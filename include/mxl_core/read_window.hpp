#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <unordered_map>
#include <vector>

namespace mxl_core {

struct ReadWindowSource {
  std::uint64_t head_index;
  std::uint32_t grain_count;
};

struct ReadWindowSelection {
  std::uint64_t oldest_index;
  std::uint64_t newest_index;
  std::uint64_t selected_index;
};

enum class InputIndexStepKind { First, Sequential, Repeat, Skip, Regression };

struct InputIndexStep {
  InputIndexStepKind kind;
  std::uint64_t magnitude;
};

// Return the newest safely delayed index that is readable by every discrete
// source ring. A null result means the source rings do not currently overlap.
std::optional<ReadWindowSelection> select_common_read_index(
    const std::vector<ReadWindowSource>& sources,
    std::uint64_t safety_margin_frames);

InputIndexStep classify_input_index_step(
    std::optional<std::uint64_t> previous_index,
    std::uint64_t selected_index);

// A repeated input may reuse a cached picture only when that picture belongs
// to the source currently selected on the output. This prevents a failed first
// read after a CUT from momentarily holding the prior source on air.
bool can_hold_repeated_source(
    const std::optional<std::string>& last_valid_source,
    const std::string& selected_source);

// Select the newest safely delayed grain from one source ring. Unlike the
// common-window selector, this is the normal CUT path: PGM and PVW do not need
// to sample unrelated sources at one shared input index.
ReadWindowSelection select_independent_read_index(
    const ReadWindowSource& source, std::uint64_t safety_margin_frames);

struct OutputReadSelection {
  ReadWindowSelection window;
  InputIndexStep cadence;
  bool timeline_reset{false};
  bool forward_clamped{false};
};

// Owns the forward-only input cursor for each (output, source) timeline. A CUT
// changes timeline identity, so the new source begins at its current safe
// index and is never compared with the prior source's unrelated counter.
class OutputReadTimelines {
 public:
  std::optional<OutputReadSelection> select(
      const std::string& output_flow, const std::string& source_flow,
      const ReadWindowSource& source, std::uint64_t safety_margin_frames);
  void reset_source(const std::string& source_flow);

 private:
  std::unordered_map<std::string, std::string> active_source_by_output_;
  std::unordered_map<std::string,
                     std::unordered_map<std::string, std::uint64_t>>
      last_index_by_output_source_;
};

// Converts a run of unavailable frames into one latched alarm. Availability
// resets the run counter, but an emitted alarm stays latched so a persistent
// fault cannot spam health counters once per frame.
class SustainedUnavailableAlarm {
 public:
  bool note_unavailable(std::uint64_t threshold_frames);
  // Returns true when this recovery clears an active alarm episode.
  bool note_available();
  [[nodiscard]] std::uint64_t consecutive_frames() const;
  [[nodiscard]] bool latched() const;

 private:
  std::uint64_t consecutive_frames_{0};
  bool latched_{false};
};

}  // namespace mxl_core
