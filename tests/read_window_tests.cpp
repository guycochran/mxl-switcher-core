#include "mxl_core/read_window.hpp"

#include <algorithm>
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

void test_detects_possible_ring_lap_after_copy() {
  // Six-grain ring, grain 100 selected at the oldest safe index of head 104.
  require(!copy_may_have_been_overwritten(100, 104, 6),
          "a copy within the safe window was rejected");
  require(copy_may_have_been_overwritten(100, 105, 6),
          "a copy the writer may have lapped was accepted");
  require(copy_may_have_been_overwritten(100, 160, 6),
          "a long preemption during copy was accepted");
  const auto selected = select_independent_read_index({100, 6}, 0);
  require(!copy_may_have_been_overwritten(selected.oldest_index, 100, 6),
          "the selector's own oldest index failed the post-copy check");
  require(!copy_may_have_been_overwritten(selected.selected_index, 103, 6),
          "a live selection was rejected after a short copy");
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
  const auto a1 =
      timelines.select("pgm", "source-a", {900'000, 6}, 0, 900'001);
  const auto b1 =
      timelines.select("pgm", "source-b", {12'000, 6}, 0, 12'001);
  const auto a2 =
      timelines.select("pgm", "source-a", {900'001, 6}, 0, 900'002);
  const auto b2 =
      timelines.select("pgm", "source-b", {12'001, 6}, 0, 12'002);
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

void test_buffered_timeline_absorbs_phase_wobble() {
  OutputReadTimelines timelines;
  const auto first = timelines.select("pgm", "source-a", {100, 10}, 1, 101);
  const auto second = timelines.select("pgm", "source-a", {100, 10}, 1, 102);
  const auto third = timelines.select("pgm", "source-a", {102, 10}, 1, 103);
  require(first && second && third, "phase-wobble timeline was rejected");
  require(first->window.selected_index == 99 &&
              second->window.selected_index == 100 &&
              third->window.selected_index == 101,
          "buffer did not preserve one-grain source cadence");
  require(second->cadence.kind == InputIndexStepKind::Sequential &&
              third->cadence.kind == InputIndexStepKind::Sequential,
          "phase wobble became a repeat/skip pair");
}

void test_exhausted_jitter_reservoir_holds() {
  OutputReadTimelines timelines;
  const auto first = timelines.select("pgm", "source-a", {100, 10}, 1, 101);
  const auto second = timelines.select("pgm", "source-a", {100, 10}, 1, 102);
  const auto exhausted =
      timelines.select("pgm", "source-a", {100, 10}, 1, 103);
  require(first && second && exhausted, "jitter-reservoir hold was rejected");
  require(exhausted->window.selected_index == 100 &&
              exhausted->cadence.kind == InputIndexStepKind::Repeat,
          "an actual source stall did not produce a cadence hold");
}

void test_output_relative_target_survives_head_regression() {
  OutputReadTimelines timelines;
  const auto first = timelines.select("pgm", "source-a", {105, 10}, 5, 106);
  const auto recovered =
      timelines.select("pgm", "source-a", {104, 10}, 5, 107);
  require(first && recovered, "recoverable source regression was rejected");
  require(!recovered->forward_clamped &&
              recovered->window.selected_index == 101 &&
              recovered->cadence.kind == InputIndexStepKind::Sequential,
          "head regression changed the output-relative timeline");
}

void test_output_relative_delay_is_deterministic() {
  OutputReadTimelines timelines;
  const auto early_phase =
      timelines.select("pgm", "source-a", {100, 20}, 1, 101);
  timelines.reset_source("source-a");
  const auto late_phase =
      timelines.select("pgm", "source-a", {101, 20}, 1, 101);
  require(early_phase && late_phase, "phase-independent selection failed");
  require(early_phase->window.selected_index == 99 &&
              late_phase->window.selected_index == 99,
          "source-head phase changed the output-relative delay");
  require(!early_phase->presentation_fallback &&
              !late_phase->presentation_fallback,
          "anchored source unexpectedly used the adaptive fallback");
}

void test_nonanchored_source_uses_bounded_fallback() {
  OutputReadTimelines timelines;
  const std::vector<std::uint64_t> heads{95, 96, 97, 98, 99, 100};
  for (std::size_t i = 0; i < heads.size(); ++i) {
    const auto selected = timelines.select("pgm", "source-a", {heads[i], 20},
                                           1, 101 + i);
    require(selected.has_value(), "nonanchored source became unavailable");
    require(selected->presentation_fallback,
            "nonanchored source hid its presentation fallback");
    require(selected->cadence.kind ==
                (i == 0 ? InputIndexStepKind::First
                        : InputIndexStepKind::Sequential),
            "steady nonanchored source lost sequential cadence");
  }
}

void test_fast_nonanchored_fallback_stays_bounded() {
  OutputReadTimelines timelines;
  const std::vector<std::uint64_t> heads{130, 132, 134, 136, 138, 140};
  std::uint64_t max_lag = 0;
  std::uint64_t skips = 0;
  for (std::size_t i = 0; i < heads.size(); ++i) {
    const auto selected = timelines.select("pgm", "source-a", {heads[i], 20},
                                           1, 101 + i);
    require(selected.has_value(), "fast nonanchored source became unavailable");
    require(selected->presentation_fallback,
            "nonanchored source hid its presentation fallback");
    max_lag =
        std::max(max_lag, heads[i] - selected->window.selected_index);
    if (selected->cadence.kind == InputIndexStepKind::Skip) {
      skips += selected->cadence.magnitude;
    }
  }
  require(max_lag <= 2, "fallback cursor escaped its bounded lag band");
  require(skips > 0, "fallback cursor hid fast-source drift");
}

void test_stall_recovery_restores_output_relative_delay() {
  OutputReadTimelines timelines;
  const auto first = timelines.select("pgm", "source-a", {100, 10}, 1, 101);
  const auto second = timelines.select("pgm", "source-a", {100, 10}, 1, 102);
  const auto held = timelines.select("pgm", "source-a", {100, 10}, 1, 103);
  const auto recovered =
      timelines.select("pgm", "source-a", {104, 10}, 1, 104);
  require(first && second && held && recovered, "stall recovery failed");
  require(held->window.selected_index == 100 &&
              held->cadence.kind == InputIndexStepKind::Repeat,
          "source stall did not hold its last target");
  require(recovered->window.selected_index == 102 &&
              recovered->cadence.kind == InputIndexStepKind::Skip &&
              recovered->cadence.magnitude == 1,
          "recovery did not restore fixed output-relative delay");
}

void test_core_catchup_rejoins_without_regression() {
  OutputReadTimelines timelines;
  const std::vector<std::pair<std::uint64_t, std::uint64_t>> samples{
      {101, 100}, {102, 101}, {103, 108}, {104, 108}, {105, 108},
      {106, 108}, {107, 108}, {108, 108}, {109, 108}, {110, 109},
      {111, 110}};
  std::uint64_t regressions = 0;
  std::uint64_t fallback_entries = 0;
  std::uint64_t repeats = 0;
  std::optional<OutputReadSelection> last;
  for (const auto& [output, head] : samples) {
    last = timelines.select("pgm", "source-a", {head, 6}, 1, output);
    require(last.has_value(), "Core catch-up became unavailable");
    regressions += last->cadence.kind == InputIndexStepKind::Regression;
    fallback_entries += last->presentation_fallback_entry;
    repeats += last->cadence.kind == InputIndexStepKind::Repeat;
  }
  require(regressions == 0, "fallback rejoin rewound source content");
  require(fallback_entries == 1, "one catch-up episode was not counted once");
  require(repeats > 0, "forward-only rejoin did not expose its holds");
  require(last && !last->presentation_fallback &&
              last->window.selected_index == 109,
          "catch-up did not restore the fixed two-frame delay");
}

void test_core_catchup_converges_without_late_commit() {
  OutputReadTimelines timelines;
  const std::vector<std::pair<std::uint64_t, std::uint64_t>> samples{
      {101, 100}, {102, 101}, {103, 108}, {104, 108}, {105, 109},
      {106, 109}, {107, 110}, {108, 110}, {109, 111}, {110, 112},
      {111, 113}, {112, 114}, {113, 115}};
  std::uint64_t regressions = 0;
  std::uint64_t repeats = 0;
  std::optional<OutputReadSelection> last;
  for (const auto& [output, head] : samples) {
    last = timelines.select("pgm", "source-a", {head, 6}, 1, output);
    require(last.has_value(), "steady catch-up convergence became unavailable");
    regressions += last->cadence.kind == InputIndexStepKind::Regression;
    repeats += last->cadence.kind == InputIndexStepKind::Repeat;
  }
  require(regressions == 0, "steady catch-up convergence rewound content");
  require(repeats >= 4, "convergence holds were not exposed in cadence telemetry");
  require(last && !last->presentation_fallback &&
              last->window.selected_index == 111,
          "steady catch-up did not restore the fixed two-frame delay");
}

void test_out_of_ring_recovery_resets_timeline() {
  OutputReadTimelines timelines;
  require(timelines.select("pgm", "source-a", {100, 6}, 0, 101).has_value(),
          "initial timeline selection failed");
  const auto reset =
      timelines.select("pgm", "source-a", {10, 6}, 0, 102);
  require(reset && reset->timeline_reset &&
              reset->cadence.kind == InputIndexStepKind::First,
          "an out-of-ring writer epoch was not reset");
}

void test_writer_reanchor_starts_fresh_timeline() {
  OutputReadTimelines timelines;
  const auto before =
      timelines.select("pgm", "source-a", {100, 10}, 1, 101);
  const auto reset_ring =
      timelines.select("pgm", "source-a", {10, 10}, 1, 102);
  const auto reanchored =
      timelines.select("pgm", "source-a", {1000, 60}, 1, 1001);
  require(before && reset_ring && reanchored, "writer re-anchor became unavailable");
  require(reset_ring->timeline_reset &&
              reset_ring->cadence.kind == InputIndexStepKind::First,
          "reset ring was compared with the prior writer epoch");
  require(reanchored->timeline_reset &&
              reanchored->cadence.kind == InputIndexStepKind::First &&
              !reanchored->presentation_fallback &&
              reanchored->window.selected_index == 999,
          "re-anchored writer did not start a deterministic fresh timeline");
}

void test_same_writer_ring_lap_remains_a_skip() {
  OutputReadTimelines timelines;
  const auto before =
      timelines.select("pgm", "source-a", {1002, 6}, 1, 1002);
  const auto after_stall =
      timelines.select("pgm", "source-a", {1010, 6}, 1, 1010);
  require(before && after_stall, "same-writer ring lap became unavailable");
  require(!after_stall->timeline_reset &&
              after_stall->cadence.kind == InputIndexStepKind::Skip &&
              after_stall->cadence.magnitude == 7,
          "same-writer content loss was hidden as a lifecycle reset");
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
      {"post-copy ring lap detection", test_detects_possible_ring_lap_after_copy},
      {"repeat hold source identity", test_repeat_hold_requires_matching_source},
      {"cadence hold availability",
       test_valid_cadence_hold_breaks_unavailable_run},
      {"cut timeline reset", test_cut_resets_unrelated_input_timeline},
      {"buffered phase wobble", test_buffered_timeline_absorbs_phase_wobble},
      {"exhausted jitter reservoir", test_exhausted_jitter_reservoir_holds},
      {"head-regression target",
       test_output_relative_target_survives_head_regression},
      {"output-relative delay", test_output_relative_delay_is_deterministic},
      {"nonanchored fallback", test_nonanchored_source_uses_bounded_fallback},
      {"fast nonanchored fallback",
       test_fast_nonanchored_fallback_stays_bounded},
      {"stall recovery delay", test_stall_recovery_restores_output_relative_delay},
      {"Core catch-up rejoin", test_core_catchup_rejoins_without_regression},
      {"Core catch-up convergence",
       test_core_catchup_converges_without_late_commit},
      {"out-of-ring recovery reset",
       test_out_of_ring_recovery_resets_timeline},
      {"writer re-anchor reset", test_writer_reanchor_starts_fresh_timeline},
      {"same-writer ring lap", test_same_writer_ring_lap_remains_a_skip},
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
