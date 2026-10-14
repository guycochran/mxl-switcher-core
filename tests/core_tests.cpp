#include "mxl_core/core.hpp"

#include <cstdlib>
#include <functional>
#include <iostream>
#include <stdexcept>
#include <string>
#include <vector>

using namespace mxl_core;

namespace {

constexpr const char* kSourceAFlow = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa";
constexpr const char* kSourceBFlow = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb";
constexpr const char* kProgramFlow = "11111111-1111-4111-8111-111111111111";
constexpr const char* kPreviewFlow = "22222222-2222-4222-8222-222222222222";

const VideoFormat kV210{1920, 1080, 30000, 1001, "video/v210"};

struct Fixture {
  MemoryBackend backend;
  SwitcherCore core;

  Fixture()
      : backend(),
        core(add_sources(backend),
             {{"src_a", kSourceAFlow, "Camera A", kV210},
              {"src_b", kSourceBFlow, "Camera B", kV210}},
             {kProgramFlow, "Program", kV210},
             {kPreviewFlow, "Preview", kV210}, "src_a", "src_b") {}

 private:
  static MemoryBackend& add_sources(MemoryBackend& backend) {
    backend.add_source(kSourceAFlow, kV210, 0xA1);
    backend.add_source(kSourceBFlow, kV210, 0xB2);
    return backend;
  }
};

void require(bool condition, const std::string& message) {
  if (!condition) {
    throw std::runtime_error(message);
  }
}

void test_stable_output_identity() {
  Fixture f;
  for (std::uint64_t i = 1; i <= 20; ++i) {
    if ((i % 2) == 0) {
      f.core.take();
    }
    f.core.process(i);
  }
  require(f.backend.output_create_count(kProgramFlow) == 1,
          "PGM output was recreated");
  require(f.backend.output_create_count(kPreviewFlow) == 1,
          "PVW output was recreated");
  require(f.core.state().program_flow == kProgramFlow, "PGM identity changed");
  require(f.core.state().preview_flow == kPreviewFlow, "PVW identity changed");
}

void test_commands_land_on_frame_boundary() {
  Fixture f;
  f.core.process(40);
  const auto receipt = f.core.take();
  require(f.core.state().program == "src_a", "take applied between frames");
  require(receipt.accepted_revision == 0, "wrong accepted revision");
  f.core.process(41);
  require(f.core.state().program == "src_b", "take missed next boundary");
  require(f.core.state().preview == "src_a", "take did not swap buses");
  require(f.core.state().revision == 1, "revision did not advance");
}

void test_native_payload_is_not_transformed() {
  Fixture f;
  f.core.process(77);
  const auto& pgm = f.backend.writes(kProgramFlow).back().grain;
  const auto& pvw = f.backend.writes(kPreviewFlow).back().grain;
  require(pgm.payload && (*pgm.payload)[0] == 0xA1,
          "program v210 marker changed");
  require(pvw.payload && (*pvw.payload)[0] == 0xB2,
          "preview v210 marker changed");
  require((*pgm.payload)[1] == 77, "program payload signature changed");
  require(pgm.capture_timestamp_ns == 77LL * 33'366'667,
          "capture timestamp was not preserved");
}

void test_direct_cut_changes_only_program() {
  Fixture f;
  f.core.cut("src_b");
  f.core.process(1);
  const auto state = f.core.state();
  require(state.program == "src_b", "direct cut did not change PGM");
  require(state.preview == "src_b", "direct cut unexpectedly changed PVW");
}

void test_preview_then_take_is_atomic() {
  Fixture f;
  f.core.set_preview("src_a");
  f.core.set_preview("src_b");
  f.core.take();
  require(f.core.state().revision == 0, "commands leaked before boundary");
  f.core.process(1);
  const auto state = f.core.state();
  require(state.program == "src_b" && state.preview == "src_a",
          "command batch was not ordered and atomic at boundary");
  require(state.revision == 3, "each accepted command needs a revision");
}

void test_far_apart_source_timelines_cut_cleanly() {
  MemoryBackend backend;
  backend.add_source(kSourceAFlow, kV210, 0xA1, 900'000);
  backend.add_source(kSourceBFlow, kV210, 0xB2, 12'000);
  SwitcherCore core(
      backend,
      {{"src_a", kSourceAFlow, "Camera A", kV210},
       {"src_b", kSourceBFlow, "Camera B", kV210}},
      {kProgramFlow, "Program", kV210},
      {kPreviewFlow, "Preview", kV210}, "src_a", "src_b");

  core.process(1);
  core.take();
  core.process(2);
  core.take();
  core.process(3);
  core.take();
  core.process(4);

  const auto& pgm = backend.writes(kProgramFlow);
  require(pgm.size() == 4, "A/B/A/B sequence lost an output frame");
  require((*pgm[0].grain.payload)[0] == 0xA1 &&
              (*pgm[1].grain.payload)[0] == 0xB2 &&
              (*pgm[2].grain.payload)[0] == 0xA1 &&
              (*pgm[3].grain.payload)[0] == 0xB2,
          "A/B/A/B sequence emitted a stale or wrong-source frame");
  require(pgm[0].grain.source_index == 900'001 &&
              pgm[1].grain.source_index == 12'002 &&
              pgm[2].grain.source_index == 900'003 &&
              pgm[3].grain.source_index == 12'004,
          "a cut incorrectly carried the prior source's input timeline");
  for (std::size_t i = 0; i < pgm.size(); ++i) {
    require(pgm[i].output_index == i + 1,
            "program output indices regressed across a cut");
  }
}

void test_bad_control_and_clock_inputs_are_rejected() {
  Fixture f;
  bool unknown_rejected = false;
  try {
    f.core.cut("does_not_exist");
  } catch (const std::invalid_argument&) {
    unknown_rejected = true;
  }
  require(unknown_rejected, "unknown source accepted");
  f.core.process(10);
  bool duplicate_rejected = false;
  try {
    f.core.process(10);
  } catch (const std::invalid_argument&) {
    duplicate_rejected = true;
  }
  require(duplicate_rejected, "duplicate grain index accepted");
}

void test_fast_soak(std::uint64_t frames) {
  Fixture f;
  for (std::uint64_t i = 1; i <= frames; ++i) {
    if ((i % 997) == 0) {
      f.core.take();
    }
    f.core.process(i);
  }
  require(f.backend.write_count(kProgramFlow) == frames,
          "soak lost program grains");
  require(f.backend.write_count(kPreviewFlow) == frames,
          "soak lost preview grains");
  require(f.backend.writes(kProgramFlow).size() <= 256,
          "mock output history grew without a bound");
  require(f.backend.writes(kPreviewFlow).size() <= 256,
          "mock preview history grew without a bound");
  require(f.backend.output_create_count(kProgramFlow) == 1,
          "soak recreated program flow");
}

}  // namespace

int main(int argc, char** argv) {
  std::uint64_t soak_frames = 100'000;
  if (argc == 3 && std::string(argv[1]) == "--soak") {
    soak_frames = std::strtoull(argv[2], nullptr, 10);
  }

  const std::vector<std::pair<std::string, std::function<void()>>> tests = {
      {"stable output identity", test_stable_output_identity},
      {"frame-boundary commands", test_commands_land_on_frame_boundary},
      {"native payload preservation", test_native_payload_is_not_transformed},
      {"direct cut semantics", test_direct_cut_changes_only_program},
      {"atomic ordered command batch", test_preview_then_take_is_atomic},
      {"far-apart source timeline cuts",
       test_far_apart_source_timelines_cut_cleanly},
      {"invalid input rejection", test_bad_control_and_clock_inputs_are_rejected},
      {"fast soak", [=] { test_fast_soak(soak_frames); }},
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
  std::cout << "All " << tests.size() << " tests passed (soak=" << soak_frames
            << " frames).\n";
  return 0;
}
