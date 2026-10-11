#pragma once

#include "mxl_core/core.hpp"

#include <cstdint>
#include <memory>
#include <string>
#include <vector>

namespace mxl_core {

struct MxlOutputDefinition {
  FlowId flow_id;
  std::string flow_definition_json;
};

struct MxlBackendStats {
  std::uint64_t routed{0};
  std::uint64_t invalid{0};
  std::uint64_t too_early{0};
  std::uint64_t too_late{0};
  std::uint64_t timeouts{0};
  std::uint64_t reader_reopens{0};
  std::uint64_t sync_waits{0};
  std::uint64_t sync_timeouts{0};
};

// Production backend for the official DMF MXL Flow API. The header keeps SDK
// types private so the engine and mock tests build without an installed SDK.
class MxlBackend final : public MediaBackend {
 public:
  MxlBackend(std::string domain,
             std::vector<MxlOutputDefinition> output_definitions,
             std::uint64_t read_timeout_ns = 5'000'000);
  ~MxlBackend() override;

  MxlBackend(const MxlBackend&) = delete;
  MxlBackend& operator=(const MxlBackend&) = delete;
  MxlBackend(MxlBackend&&) noexcept;
  MxlBackend& operator=(MxlBackend&&) noexcept;

  void create_output_once(const OutputSpec& output) override;
  void prepare_frame(const std::vector<FlowId>& source_flows,
                     std::uint64_t output_index) override;
  void route_grain(const FlowId& source_flow, const FlowId& output_flow,
                   std::uint64_t output_index) override;

  std::uint64_t current_index(const VideoFormat& format) const;
  void sleep_until_index(std::uint64_t index,
                         const VideoFormat& format) const;
  MxlBackendStats stats() const;

 private:
  class Impl;
  std::unique_ptr<Impl> impl_;
};

}  // namespace mxl_core
