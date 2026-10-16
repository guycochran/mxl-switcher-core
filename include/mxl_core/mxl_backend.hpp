#pragma once

#include "mxl_core/core.hpp"

#include <cstdint>
#include <memory>
#include <string>
#include <unordered_map>
#include <vector>

namespace mxl_core {

struct MxlOutputDefinition {
  FlowId flow_id;
  std::string flow_definition_json;
};

struct MxlSourceStats {
  std::uint64_t routed{0};
  std::uint64_t invalid{0};
  std::uint64_t too_early{0};
  std::uint64_t too_late{0};
  std::uint64_t too_late_at_ring_edge{0};
  std::uint64_t timeouts{0};
  std::uint64_t reader_reopens{0};
  std::uint64_t no_common_window{0};
  std::uint64_t latest_head_index{0};
  std::uint64_t latest_oldest_index{0};
  std::uint64_t latest_selected_index{0};
  std::uint64_t latest_input_lag_frames{0};
  std::uint64_t max_input_lag_frames{0};
  std::uint64_t presentation_fallbacks{0};
  std::uint64_t presentation_fallback_entries{0};
};

struct MxlBackendStats {
  std::uint64_t routed{0};
  std::uint64_t held{0};
  std::uint64_t invalid{0};
  std::uint64_t too_early{0};
  std::uint64_t too_late{0};
  std::uint64_t too_late_at_ring_edge{0};
  std::uint64_t timeouts{0};
  std::uint64_t reader_reopens{0};
  std::uint64_t sync_waits{0};
  std::uint64_t sync_timeouts{0};
  std::uint64_t sync_too_early{0};
  std::uint64_t sync_too_late{0};
  std::uint64_t adaptive_selections{0};
  std::uint64_t independent_selections{0};
  std::uint64_t no_common_window{0};
  std::uint64_t observed_common_window_empty{0};
  std::uint64_t input_timeline_resets{0};
  std::uint64_t recovery_index_clamps{0};
  std::uint64_t copy_overwrite_suspects{0};
  std::uint64_t sustained_unavailable_alarms{0};
  std::uint64_t sustained_unavailable_active{0};
  std::uint64_t last_common_oldest_index{0};
  std::uint64_t last_common_newest_index{0};
  std::uint64_t last_selected_input_index{0};
  std::uint64_t last_selected_input_lag_frames{0};
  std::uint64_t max_selected_input_lag_frames{0};
  std::uint64_t last_common_window_width{0};
  std::uint64_t min_common_window_width{0};
  std::uint64_t max_common_window_width{0};
  std::uint64_t last_source_skew_frames{0};
  std::uint64_t min_source_skew_frames{0};
  std::uint64_t max_source_skew_frames{0};
  std::uint64_t selected_input_repeats{0};
  std::uint64_t selected_input_skip_events{0};
  std::uint64_t selected_input_skipped_grains{0};
  std::uint64_t selected_input_regressions{0};
  std::uint64_t presentation_fallbacks{0};
  std::uint64_t presentation_fallback_entries{0};
  std::unordered_map<FlowId, MxlSourceStats> sources;
};

// Production backend for the official DMF MXL Flow API. The header keeps SDK
// types private so the engine and mock tests build without an installed SDK.
class MxlBackend final : public MediaBackend {
 public:
  MxlBackend(std::string domain,
             std::vector<MxlOutputDefinition> output_definitions,
             std::uint64_t read_timeout_ns = 5'000'000,
             std::uint64_t input_delay_frames = 0);
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
