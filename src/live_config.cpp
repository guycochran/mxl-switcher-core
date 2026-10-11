#include "mxl_core/live_config.hpp"

#include <charconv>
#include <fstream>
#include <limits>
#include <stdexcept>
#include <string_view>
#include <unordered_map>
#include <unordered_set>

namespace mxl_core {
namespace {

std::string_view trim(std::string_view value) {
  const auto first = value.find_first_not_of(" \t\r\n");
  if (first == std::string_view::npos) {
    return {};
  }
  const auto last = value.find_last_not_of(" \t\r\n");
  return value.substr(first, last - first + 1);
}

std::uint64_t unsigned_value(std::string_view value, std::string_view key,
                             std::size_t line) {
  value = trim(value);
  std::uint64_t parsed = 0;
  const auto [end, error] =
      std::from_chars(value.data(), value.data() + value.size(), parsed);
  if (error != std::errc{} || end != value.data() + value.size()) {
    throw std::invalid_argument("invalid unsigned value for " +
                                std::string(key) + " at line " +
                                std::to_string(line));
  }
  return parsed;
}

std::uint32_t uint32_value(std::string_view value, std::string_view key,
                           std::size_t line) {
  const auto parsed = unsigned_value(value, key, line);
  if (parsed > std::numeric_limits<std::uint32_t>::max()) {
    throw std::invalid_argument("value for " + std::string(key) +
                                " is out of range at line " +
                                std::to_string(line));
  }
  return static_cast<std::uint32_t>(parsed);
}

LiveSourceConfig source_value(std::string_view value, std::size_t line) {
  const auto first = value.find('|');
  const auto second = first == std::string_view::npos
                          ? std::string_view::npos
                          : value.find('|', first + 1);
  if (first == std::string_view::npos || second == std::string_view::npos ||
      value.find('|', second + 1) != std::string_view::npos) {
    throw std::invalid_argument(
        "source must be id|flow_uuid|label at line " +
        std::to_string(line));
  }
  LiveSourceConfig source{std::string(trim(value.substr(0, first))),
                          std::string(trim(value.substr(first + 1,
                                                       second - first - 1))),
                          std::string(trim(value.substr(second + 1)))};
  if (source.id.empty() || source.flow_id.empty() || source.label.empty()) {
    throw std::invalid_argument("source fields must not be empty at line " +
                                std::to_string(line));
  }
  return source;
}

void require_nonempty(std::string_view value, std::string_view key) {
  if (value.empty()) {
    throw std::invalid_argument("missing required config key: " +
                                std::string(key));
  }
}

}  // namespace

LiveConfig load_live_config(const std::string& path) {
  std::ifstream input(path);
  if (!input) {
    throw std::runtime_error("cannot open config: " + path);
  }

  LiveConfig config;
  std::unordered_set<std::string> seen;
  std::string line_text;
  std::size_t line_number = 0;
  while (std::getline(input, line_text)) {
    ++line_number;
    const auto line = trim(line_text);
    if (line.empty() || line.front() == '#') {
      continue;
    }
    const auto equals = line.find('=');
    if (equals == std::string_view::npos) {
      throw std::invalid_argument("expected key=value at line " +
                                  std::to_string(line_number));
    }
    const auto key_view = trim(line.substr(0, equals));
    const auto value = trim(line.substr(equals + 1));
    if (key_view.empty() || value.empty()) {
      throw std::invalid_argument("config key and value are required at line " +
                                  std::to_string(line_number));
    }
    const std::string key(key_view);
    if (key != "source" && !seen.emplace(key).second) {
      throw std::invalid_argument("duplicate config key " + key +
                                  " at line " +
                                  std::to_string(line_number));
    }

    if (key == "domain") {
      config.domain = value;
    } else if (key == "source") {
      config.sources.push_back(source_value(value, line_number));
    } else if (key == "program_definition") {
      config.program_definition_path = value;
    } else if (key == "program_id") {
      config.program_id = value;
    } else if (key == "preview_definition") {
      config.preview_definition_path = value;
    } else if (key == "preview_id") {
      config.preview_id = value;
    } else if (key == "initial_program") {
      config.initial_program = value;
    } else if (key == "initial_preview") {
      config.initial_preview = value;
    } else if (key == "width") {
      config.format.width = uint32_value(value, key, line_number);
    } else if (key == "height") {
      config.format.height = uint32_value(value, key, line_number);
    } else if (key == "rate_numerator") {
      config.format.rate_numerator = uint32_value(value, key, line_number);
    } else if (key == "rate_denominator") {
      config.format.rate_denominator = uint32_value(value, key, line_number);
    } else if (key == "frames") {
      config.frames = unsigned_value(value, key, line_number);
    } else if (key == "take_every") {
      config.take_every = unsigned_value(value, key, line_number);
    } else if (key == "input_delay_frames") {
      config.input_delay_frames = unsigned_value(value, key, line_number);
    } else if (key == "read_timeout_ns") {
      config.read_timeout_ns = unsigned_value(value, key, line_number);
    } else if (key == "control_host") {
      config.control_host = value;
    } else if (key == "control_port") {
      const auto port = unsigned_value(value, key, line_number);
      if (port > 65535) {
        throw std::invalid_argument("control_port is out of range at line " +
                                    std::to_string(line_number));
      }
      config.control_port = static_cast<std::uint16_t>(port);
    } else {
      throw std::invalid_argument("unknown config key " + key + " at line " +
                                  std::to_string(line_number));
    }
  }

  validate_live_config(config);
  return config;
}

void validate_live_config(const LiveConfig& config) {
  require_nonempty(config.domain, "domain");
  require_nonempty(config.program_definition_path, "program_definition");
  require_nonempty(config.program_id, "program_id");
  require_nonempty(config.preview_definition_path, "preview_definition");
  require_nonempty(config.preview_id, "preview_id");
  require_nonempty(config.initial_program, "initial_program");
  require_nonempty(config.initial_preview, "initial_preview");
  require_nonempty(config.control_host, "control_host");
  if (config.sources.size() < 2) {
    throw std::invalid_argument("at least two source entries are required");
  }
  if (config.program_id == config.preview_id) {
    throw std::invalid_argument("program_id and preview_id must be distinct");
  }
  if (config.format.width == 0 || config.format.height == 0 ||
      config.format.rate_numerator == 0 ||
      config.format.rate_denominator == 0 ||
      config.format.encoding != "video/v210") {
    throw std::invalid_argument("config operating point must be valid video/v210");
  }
  if (config.control_port == 0) {
    throw std::invalid_argument("control_port must be between 1 and 65535");
  }
  if (config.read_timeout_ns == 0) {
    throw std::invalid_argument("read_timeout_ns must be greater than zero");
  }

  std::unordered_set<SourceId> source_ids;
  std::unordered_set<FlowId> flow_ids;
  for (const auto& source : config.sources) {
    if (source.id.empty() || source.flow_id.empty() || source.label.empty() ||
        !source_ids.emplace(source.id).second ||
        !flow_ids.emplace(source.flow_id).second) {
      throw std::invalid_argument(
          "source ids and flow ids must be non-empty and unique");
    }
  }
  if (flow_ids.contains(config.program_id) ||
      flow_ids.contains(config.preview_id)) {
    throw std::invalid_argument(
        "program and preview flow ids must not reuse a source flow id");
  }
  if (!source_ids.contains(config.initial_program) ||
      !source_ids.contains(config.initial_preview)) {
    throw std::invalid_argument(
        "initial_program and initial_preview must name configured sources");
  }
}

}  // namespace mxl_core
