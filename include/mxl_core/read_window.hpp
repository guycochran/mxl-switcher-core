#pragma once

#include <cstdint>
#include <optional>
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

}  // namespace mxl_core
