#pragma once

#include "mxl_core/core.hpp"

#include <cstdint>
#include <string>
#include <vector>

namespace mxl_core {

struct LiveSourceConfig {
  SourceId id;
  FlowId flow_id;
  std::string label;
};

struct LiveConfig {
  std::string domain;
  std::vector<LiveSourceConfig> sources;
  std::string program_definition_path;
  FlowId program_id;
  std::string preview_definition_path;
  FlowId preview_id;
  SourceId initial_program;
  SourceId initial_preview;
  VideoFormat format{1920, 1080, 30000, 1001, "video/v210"};
  std::uint64_t frames{0};
  std::uint64_t take_every{0};
  std::uint64_t input_delay_frames{0};
  std::string control_host{"127.0.0.1"};
  std::uint16_t control_port{8080};
};

LiveConfig load_live_config(const std::string& path);
void validate_live_config(const LiveConfig& config);

}  // namespace mxl_core
