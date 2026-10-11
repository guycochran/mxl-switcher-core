#include "mxl_core/control_api.hpp"

#include <cctype>
#include <optional>
#include <sstream>
#include <stdexcept>

namespace mxl_core {
namespace {

std::string json_string(std::string_view value) {
  std::string result{"\""};
  for (const unsigned char character : value) {
    switch (character) {
      case '\"':
        result += "\\\"";
        break;
      case '\\':
        result += "\\\\";
        break;
      case '\b':
        result += "\\b";
        break;
      case '\f':
        result += "\\f";
        break;
      case '\n':
        result += "\\n";
        break;
      case '\r':
        result += "\\r";
        break;
      case '\t':
        result += "\\t";
        break;
      default:
        if (character < 0x20) {
          constexpr char digits[] = "0123456789abcdef";
          result += "\\u00";
          result += digits[character >> 4];
          result += digits[character & 0x0f];
        } else {
          result += static_cast<char>(character);
        }
    }
  }
  result += '\"';
  return result;
}

void skip_space(std::string_view input, std::size_t& position) {
  while (position < input.size() &&
         std::isspace(static_cast<unsigned char>(input[position])) != 0) {
    ++position;
  }
}

std::optional<std::string> source_from_json(std::string_view input) {
  const auto key = input.find("\"source\"");
  if (key == std::string_view::npos) {
    return std::nullopt;
  }
  auto position = key + 8;
  skip_space(input, position);
  if (position >= input.size() || input[position++] != ':') {
    return std::nullopt;
  }
  skip_space(input, position);
  if (position >= input.size() || input[position++] != '\"') {
    return std::nullopt;
  }

  std::string value;
  while (position < input.size()) {
    const auto character = input[position++];
    if (character == '\"') {
      return value.empty() ? std::nullopt
                           : std::optional<std::string>{std::move(value)};
    }
    if (character == '\\') {
      if (position >= input.size()) {
        return std::nullopt;
      }
      const auto escaped = input[position++];
      if (escaped != '\"' && escaped != '\\' && escaped != '/') {
        return std::nullopt;
      }
      value += escaped;
    } else if (static_cast<unsigned char>(character) < 0x20) {
      return std::nullopt;
    } else {
      value += character;
    }
  }
  return std::nullopt;
}

ApiResponse json_response(int status, std::string body) {
  return {status, "application/json", std::move(body)};
}

ApiResponse error_response(int status, std::string_view message) {
  return json_response(status, "{\"error\":" + json_string(message) + "}");
}

std::string state_json(const State& state) {
  std::ostringstream output;
  output << "{\"revision\":" << state.revision
         << ",\"program\":" << json_string(state.program)
         << ",\"preview\":" << json_string(state.preview)
         << ",\"program_flow\":" << json_string(state.program_flow)
         << ",\"preview_flow\":" << json_string(state.preview_flow)
         << ",\"last_output_index\":";
  if (state.last_output_index) {
    output << *state.last_output_index;
  } else {
    output << "null";
  }
  output << '}';
  return output.str();
}

std::string sources_json(const std::vector<Source>& sources) {
  std::ostringstream output;
  output << '[';
  bool first = true;
  for (const auto& source : sources) {
    if (!first) {
      output << ',';
    }
    first = false;
    output << "{\"id\":" << json_string(source.id)
           << ",\"flow_id\":" << json_string(source.flow_id)
           << ",\"label\":" << json_string(source.label)
           << ",\"format\":{\"width\":" << source.format.width
           << ",\"height\":" << source.format.height
           << ",\"rate_numerator\":" << source.format.rate_numerator
           << ",\"rate_denominator\":" << source.format.rate_denominator
           << ",\"encoding\":" << json_string(source.format.encoding)
           << "}}";
  }
  output << ']';
  return output.str();
}

ApiResponse receipt_response(const CommandReceipt& receipt) {
  return json_response(202, "{\"command_id\":" +
                                std::to_string(receipt.command_id) +
                                ",\"accepted_revision\":" +
                                std::to_string(receipt.accepted_revision) + "}");
}

}  // namespace

ControlApi::ControlApi(SwitcherCore& core, HealthProvider health_provider)
    : core_(core), health_provider_(std::move(health_provider)) {}

ApiResponse ControlApi::handle(std::string_view method, std::string_view path,
                               std::string_view body) {
  try {
    if (method == "GET" && path == "/api/mxl/v1/meta") {
      return json_response(
          200,
          "{\"name\":\"MXL Switcher Core\",\"api_version\":\"v1\","
          "\"media_in_control_process\":false}");
    }
    if (method == "GET" && path == "/api/mxl/v1/state") {
      return json_response(200, state_json(core_.state()));
    }
    if (method == "GET" && path == "/api/mxl/v1/sources") {
      return json_response(200, sources_json(core_.sources()));
    }
    if (method == "GET" && path == "/api/mxl/v1/health") {
      return json_response(200, health_provider_
                                    ? health_provider_()
                                    : "{\"status\":\"running\"}");
    }
    if (method == "POST" && path == "/api/mxl/v1/take") {
      return receipt_response(core_.take());
    }
    if (method == "POST" &&
        (path == "/api/mxl/v1/preview" || path == "/api/mxl/v1/cut")) {
      const auto source = source_from_json(body);
      if (!source) {
        return error_response(400, "body must contain a non-empty source");
      }
      return receipt_response(path == "/api/mxl/v1/preview"
                                  ? core_.set_preview(*source)
                                  : core_.cut(*source));
    }
    if (path.starts_with("/api/mxl/v1/")) {
      return error_response(405, "method or endpoint is not supported");
    }
    return error_response(404, "not found");
  } catch (const std::invalid_argument& error) {
    return error_response(400, error.what());
  }
}

ApiEvent ControlApi::current_event() const {
  const auto state = core_.state();
  return {state.revision, "state", state_json(state)};
}

std::optional<ApiEvent> ControlApi::wait_for_event(
    std::uint64_t after_revision,
    std::chrono::milliseconds timeout) const {
  const auto state = core_.wait_for_revision(after_revision, timeout);
  if (!state) {
    return std::nullopt;
  }
  return ApiEvent{state->revision, "state", state_json(*state)};
}

}  // namespace mxl_core
