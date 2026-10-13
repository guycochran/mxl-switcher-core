#include "mxl_core/read_window.hpp"

#include <algorithm>
#include <limits>
#include <stdexcept>

namespace mxl_core {

std::optional<ReadWindowSelection> select_common_read_index(
    const std::vector<ReadWindowSource>& sources,
    std::uint64_t safety_margin_frames) {
  if (sources.empty()) {
    throw std::invalid_argument("a common read window requires a source");
  }

  std::uint64_t common_oldest = 0;
  std::uint64_t common_newest = std::numeric_limits<std::uint64_t>::max();
  for (const auto& source : sources) {
    // MXL reserves the slot ahead of the head for the producer. Once the ring
    // has wrapped, the oldest safe discrete grain is head - count + 2.
    if (source.grain_count < 2) {
      throw std::invalid_argument(
          "a discrete source ring requires at least two grains");
    }
    const auto oldest =
        source.head_index >= source.grain_count
            ? source.head_index - source.grain_count + 2U
            : std::uint64_t{0};
    common_oldest = std::max(common_oldest, oldest);
    common_newest = std::min(common_newest, source.head_index);
  }

  if (common_oldest > common_newest) {
    return std::nullopt;
  }

  const auto delayed = common_newest > safety_margin_frames
                           ? common_newest - safety_margin_frames
                           : std::uint64_t{0};
  return ReadWindowSelection{common_oldest, common_newest,
                             std::max(common_oldest, delayed)};
}

InputIndexStep classify_input_index_step(
    std::optional<std::uint64_t> previous_index,
    std::uint64_t selected_index) {
  if (!previous_index) {
    return {InputIndexStepKind::First, 0};
  }
  if (selected_index == *previous_index) {
    return {InputIndexStepKind::Repeat, 1};
  }
  if (selected_index < *previous_index) {
    return {InputIndexStepKind::Regression,
            *previous_index - selected_index};
  }
  const auto advance = selected_index - *previous_index;
  if (advance == 1) {
    return {InputIndexStepKind::Sequential, 1};
  }
  return {InputIndexStepKind::Skip, advance - 1};
}

ReadWindowSelection select_independent_read_index(
    const ReadWindowSource& source,
    std::uint64_t safety_margin_frames) {
  if (source.grain_count < 2) {
    throw std::invalid_argument(
        "a discrete source ring requires at least two grains");
  }
  const auto oldest = source.head_index >= source.grain_count
                          ? source.head_index - source.grain_count + 2U
                          : std::uint64_t{0};
  const auto delayed = source.head_index > safety_margin_frames
                           ? source.head_index - safety_margin_frames
                           : std::uint64_t{0};
  return {oldest, source.head_index, std::max(oldest, delayed)};
}

std::optional<OutputReadSelection> OutputReadTimelines::select(
    const std::string& output_flow, const std::string& source_flow,
    const ReadWindowSource& source,
    std::uint64_t safety_margin_frames) {
  const auto candidate =
      select_independent_read_index(source, safety_margin_frames);
  auto& active_source = active_source_by_output_[output_flow];
  const auto timeline_reset = active_source != source_flow;
  if (timeline_reset) {
    active_source = source_flow;
    last_index_by_output_source_[output_flow].erase(source_flow);
  }

  auto& timelines = last_index_by_output_source_[output_flow];
  const auto prior = timelines.find(source_flow);
  const auto previous = prior == timelines.end()
                            ? std::optional<std::uint64_t>{}
                            : std::optional(prior->second);
  auto selected = candidate.selected_index;
  bool clamped = false;
  if (previous && selected < *previous) {
    // Recovery may not rewind within one source timeline. If the prior index
    // has already fallen out of this ring, holding is safer than reading stale
    // or out-of-range data; the caller will emit its last valid picture.
    if (*previous > candidate.newest_index ||
        *previous < candidate.oldest_index) {
      return std::nullopt;
    }
    selected = *previous;
    clamped = true;
  }
  const auto cadence = classify_input_index_step(previous, selected);
  timelines[source_flow] = selected;
  auto window = candidate;
  window.selected_index = selected;
  return OutputReadSelection{window, cadence, timeline_reset, clamped};
}

void OutputReadTimelines::reset_source(const std::string& source_flow) {
  for (auto& [output, timelines] : last_index_by_output_source_) {
    (void)output;
    timelines.erase(source_flow);
  }
  for (auto it = active_source_by_output_.begin();
       it != active_source_by_output_.end();) {
    if (it->second == source_flow) {
      it = active_source_by_output_.erase(it);
    } else {
      ++it;
    }
  }
}

bool SustainedUnavailableAlarm::note_unavailable(
    std::uint64_t threshold_frames) {
  if (threshold_frames == 0) {
    throw std::invalid_argument("unavailable alarm threshold must be positive");
  }
  ++consecutive_frames_;
  if (!latched_ && consecutive_frames_ >= threshold_frames) {
    latched_ = true;
    return true;
  }
  return false;
}

bool SustainedUnavailableAlarm::note_available() {
  const auto cleared = latched_;
  consecutive_frames_ = 0;
  latched_ = false;
  return cleared;
}

std::uint64_t SustainedUnavailableAlarm::consecutive_frames() const {
  return consecutive_frames_;
}

bool SustainedUnavailableAlarm::latched() const { return latched_; }

}  // namespace mxl_core
