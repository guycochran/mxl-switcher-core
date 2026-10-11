#include "mxl_core/mxl_backend.hpp"

#include <mxl/dataformat.h>
#include <mxl/flow.h>
#include <mxl/time.h>

#include <cstring>
#include <stdexcept>
#include <unordered_map>
#include <unordered_set>
#include <utility>

namespace mxl_core {
namespace {

mxlRational rational(const VideoFormat& format) {
  return mxlRational{format.rate_numerator, format.rate_denominator};
}

std::runtime_error sdk_error(const char* operation, mxlStatus status) {
  return std::runtime_error(std::string(operation) + " failed with MXL status " +
                            std::to_string(static_cast<int>(status)));
}

}  // namespace

class MxlBackend::Impl {
 public:
  Impl(std::string domain,
       std::vector<MxlOutputDefinition> output_definitions,
       std::uint64_t read_timeout_ns)
      : domain_(std::move(domain)), read_timeout_ns_(read_timeout_ns) {
    if (domain_.empty() || read_timeout_ns_ == 0) {
      throw std::invalid_argument("MXL domain and read timeout are required");
    }
    for (auto& definition : output_definitions) {
      if (definition.flow_id.empty() || definition.flow_definition_json.empty() ||
          !output_definitions_
               .emplace(std::move(definition.flow_id),
                        std::move(definition.flow_definition_json))
               .second) {
        throw std::invalid_argument(
            "output flow definitions must be non-empty and unique");
      }
    }
    instance_ = mxlCreateInstance(domain_.c_str(), nullptr);
    if (instance_ == nullptr) {
      throw std::runtime_error("could not open MXL domain: " + domain_);
    }
    const auto status =
        mxlCreateFlowSynchronizationGroup(instance_, &sync_group_);
    if (status != MXL_STATUS_OK) {
      mxlDestroyInstance(instance_);
      instance_ = nullptr;
      throw sdk_error("mxlCreateFlowSynchronizationGroup", status);
    }
  }

  ~Impl() {
    if (sync_group_ != nullptr) {
      mxlReleaseFlowSynchronizationGroup(instance_, sync_group_);
    }
    for (auto& [id, reader] : readers_) {
      (void)id;
      mxlReleaseFlowReader(instance_, reader.handle);
    }
    for (auto& [id, writer] : writers_) {
      (void)id;
      mxlReleaseFlowWriter(instance_, writer.handle);
    }
    if (instance_ != nullptr) {
      mxlDestroyInstance(instance_);
    }
  }

  void create_output_once(const OutputSpec& output) {
    if (writers_.contains(output.flow_id)) {
      throw std::logic_error("output flow must only be opened once");
    }
    const auto definition = output_definitions_.find(output.flow_id);
    if (definition == output_definitions_.end()) {
      throw std::invalid_argument("no flow definition for output: " +
                                  output.flow_id);
    }
    if (definition->second.find(output.flow_id) == std::string::npos ||
        definition->second.find("video/v210") == std::string::npos) {
      throw std::invalid_argument(
          "output definition must contain its UUID and video/v210 media type");
    }

    mxlFlowWriter writer = nullptr;
    mxlFlowConfigInfo config{};
    bool created = false;
    const auto status = mxlCreateFlowWriter(
        instance_, definition->second.c_str(), nullptr, &writer, &config, &created);
    if (status != MXL_STATUS_OK) {
      throw sdk_error("mxlCreateFlowWriter", status);
    }
    const auto expected_rate = rational(output.format);
    if (config.common.format != MXL_DATA_FORMAT_VIDEO ||
        config.common.payloadLocation != MXL_PAYLOAD_LOCATION_HOST_MEMORY ||
        config.common.grainRate.numerator != expected_rate.numerator ||
        config.common.grainRate.denominator != expected_rate.denominator) {
      mxlReleaseFlowWriter(instance_, writer);
      throw std::invalid_argument(
          "output MXL flow does not match host-memory v210 operating point");
    }
    writers_.emplace(output.flow_id, Writer{writer, config, created});
  }

  void prepare_frame(const std::vector<FlowId>& source_flows,
                     std::uint64_t output_index) {
    std::vector<FlowId> unique_flows;
    unique_flows.reserve(source_flows.size());
    std::unordered_set<FlowId> desired;
    for (const auto& flow_id : source_flows) {
      if (desired.emplace(flow_id).second) {
        unique_flows.push_back(flow_id);
      }
    }
    if (unique_flows.empty()) {
      throw std::invalid_argument("a frame requires at least one source flow");
    }

    if (desired != grouped_flows_) {
      for (const auto& flow_id : grouped_flows_) {
        const auto reader = readers_.find(flow_id);
        if (reader != readers_.end()) {
          const auto status = mxlFlowSynchronizationGroupRemoveReader(
              sync_group_, reader->second.handle);
          if (status != MXL_STATUS_OK) {
            throw sdk_error("mxlFlowSynchronizationGroupRemoveReader", status);
          }
        }
      }
      grouped_flows_.clear();

      for (const auto& flow_id : unique_flows) {
        auto& reader = get_reader(flow_id);
        const auto status =
            mxlFlowSynchronizationGroupAddReader(sync_group_, reader.handle);
        if (status != MXL_STATUS_OK) {
          throw sdk_error("mxlFlowSynchronizationGroupAddReader", status);
        }
        grouped_flows_.emplace(flow_id);
      }
    }

    auto& first = get_reader(unique_flows.front());
    const auto rate = first.config.common.grainRate;
    for (const auto& flow_id : unique_flows) {
      const auto& candidate = get_reader(flow_id).config.common.grainRate;
      if (candidate.numerator != rate.numerator ||
          candidate.denominator != rate.denominator) {
        throw std::invalid_argument(
            "selected MXL sources do not share a grain rate");
      }
    }

    const auto timestamp = mxlIndexToTimestamp(&rate, output_index);
    const auto status = mxlFlowSynchronizationGroupWaitForDataAt(
        sync_group_, timestamp, read_timeout_ns_);
    ++stats_.sync_waits;
    if (status == MXL_ERR_TIMEOUT || status == MXL_ERR_OUT_OF_RANGE_TOO_EARLY) {
      ++stats_.sync_timeouts;
      return;
    }
    if (status != MXL_STATUS_OK) {
      throw sdk_error("mxlFlowSynchronizationGroupWaitForDataAt", status);
    }
  }

  void route_grain(const FlowId& source_flow, const FlowId& output_flow,
                   std::uint64_t output_index) {
    auto& reader = get_reader(source_flow);
    auto& source_stats = stats_.sources[source_flow];
    const auto writer_it = writers_.find(output_flow);
    if (writer_it == writers_.end()) {
      throw std::invalid_argument("output flow is not open: " + output_flow);
    }

    mxlGrainInfo source_info{};
    std::uint8_t* source_payload = nullptr;
    const auto status = mxlFlowReaderGetGrain(reader.handle, output_index,
                                               read_timeout_ns_, &source_info,
                                               &source_payload);
    if (status == MXL_ERR_FLOW_INVALID) {
      if (grouped_flows_.contains(source_flow)) {
        const auto remove_status = mxlFlowSynchronizationGroupRemoveReader(
            sync_group_, reader.handle);
        if (remove_status != MXL_STATUS_OK) {
          throw sdk_error("mxlFlowSynchronizationGroupRemoveReader",
                          remove_status);
        }
        grouped_flows_.erase(source_flow);
      }
      mxlReleaseFlowReader(instance_, reader.handle);
      readers_.erase(source_flow);
      ++stats_.reader_reopens;
      ++source_stats.reader_reopens;
      commit_invalid(writer_it->second.handle, output_index);
      return;
    }
    if (status == MXL_ERR_OUT_OF_RANGE_TOO_EARLY) {
      ++stats_.too_early;
      ++source_stats.too_early;
      commit_invalid(writer_it->second.handle, output_index);
      return;
    }
    if (status == MXL_ERR_OUT_OF_RANGE_TOO_LATE) {
      ++stats_.too_late;
      ++source_stats.too_late;
      commit_invalid(writer_it->second.handle, output_index);
      return;
    }
    if (status == MXL_ERR_TIMEOUT) {
      ++stats_.timeouts;
      ++source_stats.timeouts;
      commit_invalid(writer_it->second.handle, output_index);
      return;
    }
    if (status != MXL_STATUS_OK) {
      throw sdk_error("mxlFlowReaderGetGrain", status);
    }

    mxlGrainInfo output_info{};
    std::uint8_t* output_payload = nullptr;
    auto write_status = mxlFlowWriterOpenGrain(
        writer_it->second.handle, output_index, &output_info, &output_payload);
    if (write_status != MXL_STATUS_OK) {
      throw sdk_error("mxlFlowWriterOpenGrain", write_status);
    }

    if ((source_info.flags & MXL_GRAIN_FLAG_INVALID) != 0) {
      output_info.flags = MXL_GRAIN_FLAG_INVALID;
      output_info.validSlices = 0;
      ++stats_.invalid;
      ++source_stats.invalid;
    } else {
      if (source_info.grainSize != output_info.grainSize ||
          source_info.totalSlices != output_info.totalSlices ||
          source_payload == nullptr || output_payload == nullptr) {
        output_info.flags = MXL_GRAIN_FLAG_INVALID;
        output_info.validSlices = 0;
        ++stats_.invalid;
        ++source_stats.invalid;
      } else {
        std::memcpy(output_payload, source_payload, source_info.grainSize);
        output_info.flags = source_info.flags;
        output_info.validSlices = source_info.validSlices;
        ++stats_.routed;
        ++source_stats.routed;
      }
    }
    write_status =
        mxlFlowWriterCommitGrain(writer_it->second.handle, &output_info);
    if (write_status != MXL_STATUS_OK) {
      throw sdk_error("mxlFlowWriterCommitGrain", write_status);
    }
  }

  std::uint64_t current_index(const VideoFormat& format) const {
    auto rate = rational(format);
    return mxlGetCurrentIndex(&rate);
  }

  void sleep_until_index(std::uint64_t index, const VideoFormat& format) const {
    auto rate = rational(format);
    mxlSleepForNs(mxlGetNsUntilIndex(index, &rate));
  }

  MxlBackendStats stats() const { return stats_; }

 private:
  struct Reader {
    mxlFlowReader handle;
    mxlFlowConfigInfo config;
  };
  struct Writer {
    mxlFlowWriter handle;
    mxlFlowConfigInfo config;
    bool created;
  };

  Reader& get_reader(const FlowId& flow_id) {
    if (auto found = readers_.find(flow_id); found != readers_.end()) {
      return found->second;
    }
    mxlFlowReader handle = nullptr;
    auto status =
        mxlCreateFlowReader(instance_, flow_id.c_str(), nullptr, &handle);
    if (status != MXL_STATUS_OK) {
      throw sdk_error("mxlCreateFlowReader", status);
    }
    mxlFlowConfigInfo config{};
    status = mxlFlowReaderGetConfigInfo(handle, &config);
    if (status != MXL_STATUS_OK) {
      mxlReleaseFlowReader(instance_, handle);
      throw sdk_error("mxlFlowReaderGetConfigInfo", status);
    }
    if (config.common.format != MXL_DATA_FORMAT_VIDEO ||
        config.common.payloadLocation != MXL_PAYLOAD_LOCATION_HOST_MEMORY) {
      mxlReleaseFlowReader(instance_, handle);
      throw std::invalid_argument(
          "source is not a host-memory MXL video flow: " + flow_id);
    }
    return readers_.emplace(flow_id, Reader{handle, config}).first->second;
  }

  void commit_invalid(mxlFlowWriter writer, std::uint64_t output_index) {
    mxlGrainInfo info{};
    std::uint8_t* unused = nullptr;
    auto status =
        mxlFlowWriterOpenGrain(writer, output_index, &info, &unused);
    if (status != MXL_STATUS_OK) {
      throw sdk_error("mxlFlowWriterOpenGrain(invalid)", status);
    }
    info.flags = MXL_GRAIN_FLAG_INVALID;
    info.validSlices = 0;
    status = mxlFlowWriterCommitGrain(writer, &info);
    if (status != MXL_STATUS_OK) {
      throw sdk_error("mxlFlowWriterCommitGrain(invalid)", status);
    }
    ++stats_.invalid;
  }

  std::string domain_;
  std::uint64_t read_timeout_ns_;
  mxlInstance instance_{nullptr};
  mxlFlowSynchronizationGroup sync_group_{nullptr};
  std::unordered_map<FlowId, std::string> output_definitions_;
  std::unordered_map<FlowId, Reader> readers_;
  std::unordered_map<FlowId, Writer> writers_;
  std::unordered_set<FlowId> grouped_flows_;
  MxlBackendStats stats_;
};

MxlBackend::MxlBackend(
    std::string domain,
    std::vector<MxlOutputDefinition> output_definitions,
    std::uint64_t read_timeout_ns)
    : impl_(std::make_unique<Impl>(std::move(domain),
                                  std::move(output_definitions),
                                  read_timeout_ns)) {}

MxlBackend::~MxlBackend() = default;
MxlBackend::MxlBackend(MxlBackend&&) noexcept = default;
MxlBackend& MxlBackend::operator=(MxlBackend&&) noexcept = default;

void MxlBackend::create_output_once(const OutputSpec& output) {
  impl_->create_output_once(output);
}

void MxlBackend::prepare_frame(const std::vector<FlowId>& source_flows,
                               std::uint64_t output_index) {
  impl_->prepare_frame(source_flows, output_index);
}

void MxlBackend::route_grain(const FlowId& source_flow,
                             const FlowId& output_flow,
                             std::uint64_t output_index) {
  impl_->route_grain(source_flow, output_flow, output_index);
}

std::uint64_t MxlBackend::current_index(const VideoFormat& format) const {
  return impl_->current_index(format);
}

void MxlBackend::sleep_until_index(std::uint64_t index,
                                   const VideoFormat& format) const {
  impl_->sleep_until_index(index, format);
}

MxlBackendStats MxlBackend::stats() const { return impl_->stats(); }

}  // namespace mxl_core
