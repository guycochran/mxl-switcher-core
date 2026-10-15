#include "mxl_core/mxl_backend.hpp"
#include "mxl_core/read_window.hpp"

#include <mxl/dataformat.h>
#include <mxl/flow.h>
#include <mxl/time.h>

#include <cerrno>
#include <cstring>
#include <limits>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <sys/stat.h>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

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
       std::uint64_t read_timeout_ns,
       std::uint64_t input_delay_frames)
      : domain_(std::move(domain)),
        read_timeout_ns_(read_timeout_ns),
        input_delay_frames_(input_delay_frames) {
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
    writers_.emplace(output.flow_id,
                     Writer{writer, config, created, {}, 0, 0, std::nullopt,
                            {}});
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

    refresh_replaced_readers(unique_flows);

    for (const auto& flow_id : unique_flows) {
      if (try_get_reader(flow_id) == nullptr) {
        prepared_output_index_ = output_index;
        std::lock_guard lock(stats_mutex_);
        ++stats_.observed_common_window_empty;
        stats_.last_common_window_width = 0;
        return;
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

    std::vector<ReadWindowSource> windows;
    windows.reserve(unique_flows.size());
    std::uint64_t minimum_head = std::numeric_limits<std::uint64_t>::max();
    std::uint64_t maximum_head = 0;
    for (const auto& flow_id : unique_flows) {
      auto& reader = get_reader(flow_id);
      mxlFlowRuntimeInfo runtime{};
      const auto status =
          mxlFlowReaderGetRuntimeInfo(reader.handle, &runtime);
      if (status != MXL_STATUS_OK) {
        throw sdk_error("mxlFlowReaderGetRuntimeInfo", status);
      }
      const auto grain_count = reader.config.discrete.grainCount;
      const auto oldest = runtime.headIndex >= grain_count
                              ? runtime.headIndex - grain_count + 2U
                              : std::uint64_t{0};
      windows.push_back({runtime.headIndex, grain_count});
      minimum_head = std::min(minimum_head, runtime.headIndex);
      maximum_head = std::max(maximum_head, runtime.headIndex);
      std::lock_guard lock(stats_mutex_);
      auto& source = stats_.sources[flow_id];
      source.latest_head_index = runtime.headIndex;
      source.latest_oldest_index = oldest;
    }

    const auto selection =
        select_common_read_index(windows, input_delay_frames_);
    prepared_output_index_ = output_index;
    if (!selection) {
      std::lock_guard lock(stats_mutex_);
      ++stats_.observed_common_window_empty;
      stats_.last_common_window_width = 0;
      stats_.last_source_skew_frames = maximum_head - minimum_head;
      stats_.max_source_skew_frames = std::max(
          stats_.max_source_skew_frames, stats_.last_source_skew_frames);
      return;
    }

    {
      std::lock_guard lock(stats_mutex_);
      const auto first_selection = stats_.adaptive_selections == 0;
      const auto window_width =
          selection->newest_index - selection->oldest_index + 1U;
      const auto source_skew = maximum_head - minimum_head;
      ++stats_.adaptive_selections;
      stats_.last_common_oldest_index = selection->oldest_index;
      stats_.last_common_newest_index = selection->newest_index;
      stats_.last_common_window_width = window_width;
      stats_.last_source_skew_frames = source_skew;
      if (first_selection || window_width < stats_.min_common_window_width) {
        stats_.min_common_window_width = window_width;
      }
      stats_.max_common_window_width =
          std::max(stats_.max_common_window_width, window_width);
      if (first_selection || source_skew < stats_.min_source_skew_frames) {
        stats_.min_source_skew_frames = source_skew;
      }
      stats_.max_source_skew_frames =
          std::max(stats_.max_source_skew_frames, source_skew);
    }
  }

  void route_grain(const FlowId& source_flow, const FlowId& output_flow,
                   std::uint64_t output_index) {
    const auto writer_it = writers_.find(output_flow);
    if (writer_it == writers_.end()) {
      throw std::invalid_argument("output flow is not open: " + output_flow);
    }
    if (!prepared_output_index_ || *prepared_output_index_ != output_index) {
      throw std::logic_error("route requested without a prepared output frame");
    }
    auto* reader = try_get_reader(source_flow);
    if (reader == nullptr) {
      commit_unavailable(writer_it->second, output_index);
      return;
    }

    mxlFlowRuntimeInfo runtime{};
    const auto runtime_status =
        mxlFlowReaderGetRuntimeInfo(reader->handle, &runtime);
    if (runtime_status == MXL_ERR_FLOW_INVALID) {
      discard_reader(source_flow);
      commit_unavailable(writer_it->second, output_index);
      return;
    }
    if (runtime_status != MXL_STATUS_OK) {
      throw sdk_error("mxlFlowReaderGetRuntimeInfo", runtime_status);
    }
    const auto grain_count = reader->config.discrete.grainCount;
    const auto oldest = runtime.headIndex >= grain_count
                            ? runtime.headIndex - grain_count + 2U
                            : std::uint64_t{0};
    {
      std::lock_guard lock(stats_mutex_);
      auto& source = stats_.sources[source_flow];
      source.latest_head_index = runtime.headIndex;
      source.latest_oldest_index = oldest;
    }
    const auto selection = timelines_.select(
        output_flow, source_flow, {runtime.headIndex, grain_count},
        input_delay_frames_, output_index);
    if (!selection) {
      commit_unavailable(writer_it->second, output_index);
      return;
    }

    bool repeat_selection = false;
    {
      std::lock_guard lock(stats_mutex_);
      ++stats_.independent_selections;
      stats_.last_selected_input_index = selection->window.selected_index;
      const auto input_lag =
          selection->window.newest_index - selection->window.selected_index;
      stats_.last_selected_input_lag_frames = input_lag;
      stats_.max_selected_input_lag_frames =
          std::max(stats_.max_selected_input_lag_frames, input_lag);
      auto& source = stats_.sources[source_flow];
      source.latest_selected_index = selection->window.selected_index;
      source.latest_input_lag_frames = input_lag;
      source.max_input_lag_frames =
          std::max(source.max_input_lag_frames, input_lag);
      if (selection->presentation_fallback) {
        ++stats_.presentation_fallbacks;
        ++source.presentation_fallbacks;
      }
      if (selection->presentation_fallback_entry) {
        ++stats_.presentation_fallback_entries;
        ++source.presentation_fallback_entries;
      }
      if (selection->timeline_reset) {
        ++stats_.input_timeline_resets;
      }
      if (selection->forward_clamped) {
        ++stats_.recovery_index_clamps;
      }
      switch (selection->cadence.kind) {
        case InputIndexStepKind::Repeat:
          ++stats_.selected_input_repeats;
          repeat_selection = true;
          break;
        case InputIndexStepKind::Skip:
          ++stats_.selected_input_skip_events;
          stats_.selected_input_skipped_grains += selection->cadence.magnitude;
          break;
        case InputIndexStepKind::Regression:
          ++stats_.selected_input_regressions;
          break;
        case InputIndexStepKind::First:
        case InputIndexStepKind::Sequential:
          break;
      }
    }
    if (repeat_selection) {
      commit_cadence_hold(writer_it->second, source_flow, output_index);
      return;
    }

    mxlGrainInfo source_info{};
    std::uint8_t* source_payload = nullptr;
    const auto status = mxlFlowReaderGetGrain(
        reader->handle, selection->window.selected_index, read_timeout_ns_,
        &source_info, &source_payload);
    if (status == MXL_ERR_FLOW_INVALID) {
      discard_reader(source_flow);
      commit_unavailable(writer_it->second, output_index);
      return;
    }
    if (status == MXL_ERR_OUT_OF_RANGE_TOO_EARLY) {
      {
        std::lock_guard lock(stats_mutex_);
        ++stats_.too_early;
        ++stats_.sources[source_flow].too_early;
      }
      commit_unavailable(writer_it->second, output_index);
      return;
    }
    if (status == MXL_ERR_OUT_OF_RANGE_TOO_LATE) {
      {
        std::lock_guard lock(stats_mutex_);
        ++stats_.too_late;
        ++stats_.sources[source_flow].too_late;
      }
      commit_unavailable(writer_it->second, output_index);
      return;
    }
    if (status == MXL_ERR_TIMEOUT) {
      {
        std::lock_guard lock(stats_mutex_);
        ++stats_.timeouts;
        ++stats_.sources[source_flow].timeouts;
      }
      commit_unavailable(writer_it->second, output_index);
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
      {
        std::lock_guard lock(stats_mutex_);
        ++stats_.sources[source_flow].invalid;
      }
      commit_unavailable(writer_it->second, output_index, &output_info,
                         output_payload);
      return;
    } else {
      if (source_info.grainSize != output_info.grainSize ||
          source_info.totalSlices != output_info.totalSlices ||
          source_payload == nullptr || output_payload == nullptr) {
        {
          std::lock_guard lock(stats_mutex_);
          ++stats_.sources[source_flow].invalid;
        }
        commit_unavailable(writer_it->second, output_index, &output_info,
                           output_payload);
        return;
      } else {
        std::memcpy(output_payload, source_payload, source_info.grainSize);
        output_info.flags = source_info.flags;
        output_info.validSlices = source_info.validSlices;
      }
    }
    // The payload was copied straight from the source ring. If the writer may
    // have lapped that slot during the copy, the picture can belong to a newer
    // grain; publish the hold/unavailable grain instead of a mislabeled one.
    mxlFlowRuntimeInfo after_copy{};
    if (mxlFlowReaderGetRuntimeInfo(reader->handle, &after_copy) ==
            MXL_STATUS_OK &&
        copy_may_have_been_overwritten(selection->window.selected_index,
                                       after_copy.headIndex, grain_count)) {
      {
        std::lock_guard lock(stats_mutex_);
        ++stats_.copy_overwrite_suspects;
      }
      commit_unavailable(writer_it->second, output_index, &output_info,
                         output_payload);
      return;
    }
    write_status =
        mxlFlowWriterCommitGrain(writer_it->second.handle, &output_info);
    if (write_status != MXL_STATUS_OK) {
      throw sdk_error("mxlFlowWriterCommitGrain", write_status);
    }
    writer_it->second.last_payload.assign(
        source_payload, source_payload + source_info.grainSize);
    writer_it->second.last_flags = source_info.flags;
    writer_it->second.last_valid_slices = source_info.validSlices;
    writer_it->second.last_source_flow = source_flow;
    const auto cleared_unavailable_alarm =
        writer_it->second.unavailable_alarm.note_available();
    {
      std::lock_guard lock(stats_mutex_);
      if (cleared_unavailable_alarm &&
          stats_.sustained_unavailable_active > 0) {
        --stats_.sustained_unavailable_active;
      }
      ++stats_.routed;
      ++stats_.sources[source_flow].routed;
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

  MxlBackendStats stats() const {
    std::lock_guard lock(stats_mutex_);
    return stats_;
  }

 private:
  struct FlowIdentity {
    dev_t device;
    ino_t inode;

    bool operator==(const FlowIdentity&) const = default;
  };

  struct Reader {
    mxlFlowReader handle;
    mxlFlowConfigInfo config;
    std::optional<FlowIdentity> identity;
  };
  struct Writer {
    mxlFlowWriter handle;
    mxlFlowConfigInfo config;
    bool created;
    std::vector<std::uint8_t> last_payload;
    std::uint32_t last_flags{0};
    std::uint32_t last_valid_slices{0};
    std::optional<FlowId> last_source_flow;
    SustainedUnavailableAlarm unavailable_alarm;
  };

  Reader& get_reader(const FlowId& flow_id) {
    auto* reader = try_get_reader(flow_id);
    if (reader == nullptr) {
      throw std::runtime_error("MXL source flow is temporarily unavailable: " +
                               flow_id);
    }
    return *reader;
  }

  Reader* try_get_reader(const FlowId& flow_id) {
    if (auto found = readers_.find(flow_id); found != readers_.end()) {
      return &found->second;
    }
    mxlFlowReader handle = nullptr;
    auto status =
        mxlCreateFlowReader(instance_, flow_id.c_str(), nullptr, &handle);
    if (status != MXL_STATUS_OK) {
      return nullptr;
    }
    mxlFlowConfigInfo config{};
    status = mxlFlowReaderGetConfigInfo(handle, &config);
    if (status != MXL_STATUS_OK) {
      mxlReleaseFlowReader(instance_, handle);
      return nullptr;
    }
    if (config.common.format != MXL_DATA_FORMAT_VIDEO ||
        config.common.payloadLocation != MXL_PAYLOAD_LOCATION_HOST_MEMORY) {
      mxlReleaseFlowReader(instance_, handle);
      throw std::invalid_argument(
          "source is not a host-memory MXL video flow: " + flow_id);
    }
    return &readers_
                .emplace(flow_id,
                         Reader{handle, config, flow_identity(flow_id)})
                .first->second;
  }

  std::optional<FlowIdentity> flow_identity(const FlowId& flow_id) const {
    const auto path = domain_ + "/" + flow_id + ".mxl-flow";
    struct stat metadata {};
    if (::stat(path.c_str(), &metadata) == 0) {
      return FlowIdentity{metadata.st_dev, metadata.st_ino};
    }
    if (errno == ENOENT) {
      return std::nullopt;
    }
    throw std::runtime_error("could not inspect MXL flow object: " + path +
                             ": " + std::strerror(errno));
  }

  void reset_sync_group() {
    if (sync_group_ != nullptr) {
      mxlReleaseFlowSynchronizationGroup(instance_, sync_group_);
      sync_group_ = nullptr;
    }
    const auto status =
        mxlCreateFlowSynchronizationGroup(instance_, &sync_group_);
    if (status != MXL_STATUS_OK) {
      throw sdk_error("mxlCreateFlowSynchronizationGroup", status);
    }
    grouped_flows_.clear();
  }

  void discard_reader(const FlowId& flow_id) {
    const auto found = readers_.find(flow_id);
    if (found == readers_.end()) {
      return;
    }
    if (grouped_flows_.contains(flow_id)) {
      reset_sync_group();
    }
    mxlReleaseFlowReader(instance_, found->second.handle);
    readers_.erase(found);
    timelines_.reset_source(flow_id);
    std::lock_guard lock(stats_mutex_);
    ++stats_.reader_reopens;
    ++stats_.sources[flow_id].reader_reopens;
  }

  void refresh_replaced_readers(const std::vector<FlowId>& flow_ids) {
    std::vector<FlowId> replaced;
    for (const auto& flow_id : flow_ids) {
      const auto found = readers_.find(flow_id);
      if (found == readers_.end()) {
        continue;
      }
      const auto current = flow_identity(flow_id);
      if (!current) {
        continue;
      }
      if (!found->second.identity) {
        found->second.identity = current;
      } else if (*found->second.identity != *current) {
        replaced.push_back(flow_id);
      }
    }
    if (replaced.empty()) {
      return;
    }
    reset_sync_group();
    for (const auto& flow_id : replaced) {
      const auto found = readers_.find(flow_id);
      if (found != readers_.end()) {
        mxlReleaseFlowReader(instance_, found->second.handle);
        readers_.erase(found);
      }
      timelines_.reset_source(flow_id);
      std::lock_guard lock(stats_mutex_);
      ++stats_.reader_reopens;
      ++stats_.sources[flow_id].reader_reopens;
    }
  }

  void commit_unavailable(Writer& writer, std::uint64_t output_index,
                          mxlGrainInfo* opened_info = nullptr,
                          std::uint8_t* opened_payload = nullptr) {
    mxlGrainInfo local_info{};
    auto* info = opened_info == nullptr ? &local_info : opened_info;
    std::uint8_t* payload = opened_payload;
    auto status = opened_info == nullptr
                      ? mxlFlowWriterOpenGrain(writer.handle, output_index,
                                               info, &payload)
                      : MXL_STATUS_OK;
    if (status != MXL_STATUS_OK) {
      throw sdk_error("mxlFlowWriterOpenGrain(unavailable)", status);
    }
    const auto can_hold = payload != nullptr && !writer.last_payload.empty() &&
                          writer.last_payload.size() == info->grainSize;
    if (can_hold) {
      std::memcpy(payload, writer.last_payload.data(), writer.last_payload.size());
      info->flags = writer.last_flags;
      info->validSlices = writer.last_valid_slices;
    } else {
      info->flags = MXL_GRAIN_FLAG_INVALID;
      info->validSlices = 0;
    }
    status = mxlFlowWriterCommitGrain(writer.handle, info);
    if (status != MXL_STATUS_OK) {
      throw sdk_error("mxlFlowWriterCommitGrain(unavailable)", status);
    }
    std::lock_guard lock(stats_mutex_);
    if (writer.unavailable_alarm.note_unavailable(
            kSustainedUnavailableFrames)) {
      ++stats_.sustained_unavailable_alarms;
      ++stats_.sustained_unavailable_active;
    }
    if (can_hold) {
      ++stats_.held;
    } else {
      ++stats_.invalid;
    }
  }

  // A repeated input index is a normal producer/output cadence relationship,
  // not evidence that the source is unavailable. Re-emit the last valid
  // picture at the new output index without advancing the unavailable alarm.
  // The source identity guard is essential across CUTs: if the first read from
  // a newly selected source failed, the cached picture still belongs to the
  // prior source and must never be placed on air as a valid cadence hold.
  void commit_cadence_hold(Writer& writer, const FlowId& source_flow,
                           std::uint64_t output_index) {
    mxlGrainInfo info{};
    std::uint8_t* payload = nullptr;
    auto status = mxlFlowWriterOpenGrain(writer.handle, output_index, &info,
                                         &payload);
    if (status != MXL_STATUS_OK) {
      throw sdk_error("mxlFlowWriterOpenGrain(cadence hold)", status);
    }
    const auto can_hold = can_hold_repeated_source(writer.last_source_flow,
                                                   source_flow) &&
                          payload != nullptr && !writer.last_payload.empty() &&
                          writer.last_payload.size() == info.grainSize;
    if (!can_hold) {
      commit_unavailable(writer, output_index, &info, payload);
      return;
    }

    std::memcpy(payload, writer.last_payload.data(), writer.last_payload.size());
    info.flags = writer.last_flags;
    info.validSlices = writer.last_valid_slices;
    status = mxlFlowWriterCommitGrain(writer.handle, &info);
    if (status != MXL_STATUS_OK) {
      throw sdk_error("mxlFlowWriterCommitGrain(cadence hold)", status);
    }

    const auto cleared_unavailable_alarm =
        writer.unavailable_alarm.note_available();
    std::lock_guard lock(stats_mutex_);
    if (cleared_unavailable_alarm &&
        stats_.sustained_unavailable_active > 0) {
      --stats_.sustained_unavailable_active;
    }
    ++stats_.held;
  }

  std::string domain_;
  std::uint64_t read_timeout_ns_;
  std::uint64_t input_delay_frames_;
  mxlInstance instance_{nullptr};
  mxlFlowSynchronizationGroup sync_group_{nullptr};
  std::unordered_map<FlowId, std::string> output_definitions_;
  std::unordered_map<FlowId, Reader> readers_;
  std::unordered_map<FlowId, Writer> writers_;
  std::unordered_set<FlowId> grouped_flows_;
  std::optional<std::uint64_t> prepared_output_index_;
  OutputReadTimelines timelines_;
  static constexpr std::uint64_t kSustainedUnavailableFrames = 5;
  mutable std::mutex stats_mutex_;
  MxlBackendStats stats_;
};

MxlBackend::MxlBackend(
    std::string domain,
    std::vector<MxlOutputDefinition> output_definitions,
    std::uint64_t read_timeout_ns,
    std::uint64_t input_delay_frames)
    : impl_(std::make_unique<Impl>(std::move(domain),
                                  std::move(output_definitions),
                                  read_timeout_ns,
                                  input_delay_frames)) {}

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
