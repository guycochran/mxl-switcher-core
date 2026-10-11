#include "mxl_core/read_window.hpp"

#include <functional>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

using namespace mxl_core;

namespace {

void require(bool condition, const std::string& message) {
  if (!condition) {
    throw std::runtime_error(message);
  }
}

void test_selects_delayed_index_from_overlap() {
  const auto selected = select_common_read_index({{100, 29}, {96, 29}}, 5);
  require(selected.has_value(), "overlapping sources were rejected");
  require(selected->oldest_index == 73, "wrong common oldest index");
  require(selected->newest_index == 96, "wrong common newest index");
  require(selected->selected_index == 91, "safety margin was not applied");
}

void test_clamps_delay_to_narrow_overlap() {
  const auto selected = select_common_read_index({{130, 29}, {105, 29}}, 5);
  require(selected.has_value(), "narrow overlap was rejected");
  require(selected->oldest_index == 103, "wrong narrow-window lower bound");
  require(selected->newest_index == 105, "wrong narrow-window upper bound");
  require(selected->selected_index == 103,
          "selection escaped the narrow common window");
}

void test_reports_no_overlap() {
  const auto selected = select_common_read_index({{140, 29}, {105, 29}}, 5);
  require(!selected.has_value(), "disjoint source rings were accepted");
}

void test_handles_startup_without_underflow() {
  const auto selected = select_common_read_index({{3, 29}, {2, 29}}, 5);
  require(selected.has_value(), "startup window was rejected");
  require(selected->oldest_index == 0 && selected->newest_index == 2 &&
              selected->selected_index == 0,
          "startup selection did not clamp to index zero");
}

void test_rejects_invalid_input() {
  bool empty_rejected = false;
  try {
    (void)select_common_read_index({}, 0);
  } catch (const std::invalid_argument&) {
    empty_rejected = true;
  }
  require(empty_rejected, "empty source set was accepted");

  bool shallow_rejected = false;
  try {
    (void)select_common_read_index({{10, 1}}, 0);
  } catch (const std::invalid_argument&) {
    shallow_rejected = true;
  }
  require(shallow_rejected, "invalid ring depth was accepted");
}

}  // namespace

int main() {
  const std::vector<std::pair<std::string, std::function<void()>>> tests = {
      {"delayed overlap selection", test_selects_delayed_index_from_overlap},
      {"narrow overlap clamp", test_clamps_delay_to_narrow_overlap},
      {"no-overlap detection", test_reports_no_overlap},
      {"startup underflow clamp", test_handles_startup_without_underflow},
      {"invalid input rejection", test_rejects_invalid_input},
  };

  for (const auto& [name, test] : tests) {
    try {
      test();
      std::cout << "PASS  " << name << '\n';
    } catch (const std::exception& error) {
      std::cerr << "FAIL  " << name << ": " << error.what() << '\n';
      return 1;
    }
  }
  std::cout << "All " << tests.size() << " read-window tests passed.\n";
  return 0;
}
