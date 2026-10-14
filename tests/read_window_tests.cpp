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

void test_classifies_input_cadence() {
  require(classify_input_index_step(std::nullopt, 100).kind ==
              InputIndexStepKind::First,
          "first selection was misclassified");
  require(classify_input_index_step(100, 101).kind ==
              InputIndexStepKind::Sequential,
          "sequential selection was misclassified");
  require(classify_input_index_step(100, 100).kind ==
              InputIndexStepKind::Repeat,
          "repeated selection was misclassified");
  const auto skip = classify_input_index_step(100, 104);
  require(skip.kind == InputIndexStepKind::Skip && skip.magnitude == 3,
          "skipped grains were counted incorrectly");
  const auto regression = classify_input_index_step(104, 100);
  require(regression.kind == InputIndexStepKind::Regression &&
              regression.magnitude == 4,
          "input regression was counted incorrectly");
}

void test_independent_selection_uses_one_ring() {
  const auto selected = select_independent_read_index({100, 6}, 0);
  require(selected.oldest_index == 96 && selected.newest_index == 100 &&
              selected.selected_index == 100,
          "independent selection escaped its source ring");
}

void test_repeat_hold_requires_matching_source() {
  require(!can_hold_repeated_source(std::nullopt, "source-a"),
          "repeat hold accepted an empty payload history");
  require(can_hold_repeated_source(std::string{"source-a"}, "source-a"),
          "repeat hold rejected the selected source's cached picture");
  require(!can_hold_repeated_source(std::string{"source-a"}, "source-b"),
          "repeat hold could leak the prior source across a cut");
}

void test_valid_cadence_hold_breaks_unavailable_run() {
  SustainedUnavailableAlarm alarm;
  for (int i = 0; i < 4; ++i) {
    require(!alarm.note_unavailable(5),
            "unavailable alarm fired before its threshold");
  }
  require(!alarm.note_available(),
          "a pre-threshold cadence hold reported a cleared alarm");
  require(alarm.consecutive_frames() == 0,
          "a valid cadence hold did not break the unavailable run");
  for (int i = 0; i < 4; ++i) {
    require(!alarm.note_unavailable(5),
            "separate unavailable runs were incorrectly combined");
  }
}

void test_cut_resets_unrelated_input_timeline() {
  OutputReadTimelines timelines;
  const auto a1 = timelines.select("pgm", "source-a", {900'000, 6}, 0);
  const auto b1 = timelines.select("pgm", "source-b", {12'000, 6}, 0);
  const auto a2 = timelines.select("pgm", "source-a", {900'001, 6}, 0);
  const auto b2 = timelines.select("pgm", "source-b", {12'001, 6}, 0);
  require(a1 && b1 && a2 && b2, "cut timeline selection failed");
  require(a1->timeline_reset && b1->timeline_reset && a2->timeline_reset &&
              b2->timeline_reset,
          "source changes did not reset the output timeline");
  require(a1->cadence.kind == InputIndexStepKind::First &&
              b1->cadence.kind == InputIndexStepKind::First &&
              a2->cadence.kind == InputIndexStepKind::First &&
              b2->cadence.kind == InputIndexStepKind::First,
          "a cut was misclassified as a skip or regression");
  require(b1->window.selected_index == 12'000 &&
              a2->window.selected_index == 900'001,
          "a cut inherited the prior source's input floor");
}

void test_recovery_clamps_forward_within_same_timeline() {
  OutputReadTimelines timelines;
  const auto first = timelines.select("pgm", "source-a", {105, 10}, 5);
  const auto recovered = timelines.select("pgm", "source-a", {104, 10}, 5);
  require(first && recovered, "recoverable source regression was rejected");
  require(recovered->forward_clamped &&
              recovered->window.selected_index == 100 &&
              recovered->cadence.kind == InputIndexStepKind::Repeat,
          "recovery was not clamped to its forward-only floor");
}

void test_recovery_holds_when_floor_left_ring() {
  OutputReadTimelines timelines;
  require(timelines.select("pgm", "source-a", {100, 6}, 0).has_value(),
          "initial timeline selection failed");
  require(!timelines.select("pgm", "source-a", {10, 6}, 0).has_value(),
          "an out-of-ring recovery floor was accepted");
}

void test_sustained_unavailable_alarm_is_one_shot() {
  mxl_core::SustainedUnavailableAlarm alarm;
  require(!alarm.note_unavailable(3), "first unavailable frame is not sustained");
  require(!alarm.note_unavailable(3), "second unavailable frame is not sustained");
  require(alarm.note_unavailable(3), "threshold crossing emits the alarm");
  require(!alarm.note_unavailable(3), "latched alarm does not repeat");
  require(alarm.latched(), "alarm stays latched after threshold crossing");
  require(alarm.note_available(), "recovery reports the cleared alarm episode");
  require(alarm.consecutive_frames() == 0,
          "availability resets the consecutive-frame counter");
  require(!alarm.note_unavailable(3),
          "a new episode starts below threshold after recovery");
  require(!alarm.note_unavailable(3),
          "new episode remains below threshold on frame two");
  require(alarm.note_unavailable(3),
          "a later sustained episode emits one new alarm");
}

}  // namespace

int main() {
  const std::vector<std::pair<std::string, std::function<void()>>> tests = {
      {"delayed overlap selection", test_selects_delayed_index_from_overlap},
      {"narrow overlap clamp", test_clamps_delay_to_narrow_overlap},
      {"no-overlap detection", test_reports_no_overlap},
      {"startup underflow clamp", test_handles_startup_without_underflow},
      {"invalid input rejection", test_rejects_invalid_input},
      {"input cadence classification", test_classifies_input_cadence},
      {"independent one-ring selection", test_independent_selection_uses_one_ring},
      {"repeat hold source identity", test_repeat_hold_requires_matching_source},
      {"cadence hold availability",
       test_valid_cadence_hold_breaks_unavailable_run},
      {"cut timeline reset", test_cut_resets_unrelated_input_timeline},
      {"forward-only recovery clamp",
       test_recovery_clamps_forward_within_same_timeline},
      {"out-of-ring recovery hold", test_recovery_holds_when_floor_left_ring},
      {"sustained unavailable one-shot",
       test_sustained_unavailable_alarm_is_one_shot},
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
