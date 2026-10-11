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

}  // namespace mxl_core
