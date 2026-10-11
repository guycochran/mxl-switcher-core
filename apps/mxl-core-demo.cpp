#include "mxl_core/core.hpp"

#include <iostream>

using namespace mxl_core;

namespace {
constexpr const char* kProgramFlow = "11111111-1111-4111-8111-111111111111";
constexpr const char* kPreviewFlow = "22222222-2222-4222-8222-222222222222";
}

int main() {
  const VideoFormat v210{1920, 1080, 30000, 1001, "video/v210"};
  MemoryBackend media;
  media.add_source("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", v210, 0xA1);
  media.add_source("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", v210, 0xB2);

  SwitcherCore core(
      media,
      {{"src_cam_a", "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa", "Camera A", v210},
       {"src_cam_b", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "Camera B", v210}},
      {kProgramFlow, "Program", v210}, {kPreviewFlow, "Preview", v210},
      "src_cam_a", "src_cam_b");

  std::cout << "MXL Switcher Core — mock domain, 1080p29.97 v210\n";
  for (std::uint64_t index = 1000; index < 1010; ++index) {
    if (index == 1004) {
      core.take();
    }
    if (index == 1007) {
      core.cut("src_cam_a");
    }
    core.process(index);
    const auto state = core.state();
    std::cout << "grain=" << index << " revision=" << state.revision
              << " PGM=" << state.program << " PVW=" << state.preview << '\n';
  }

  std::cout << "stable PGM flow: " << core.state().program_flow << '\n';
  std::cout << "stable PVW flow: " << core.state().preview_flow << '\n';
  return 0;
}

