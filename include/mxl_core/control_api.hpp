#pragma once

#include "mxl_core/core.hpp"

#include <chrono>
#include <functional>
#include <optional>
#include <string>
#include <string_view>

namespace mxl_core {

struct ApiResponse {
  int status;
  std::string content_type;
  std::string body;
};

struct ApiEvent {
  std::uint64_t id;
  std::string type;
  std::string data;
};

// Dependency-free implementation of the versioned control contract. A small
// HTTP transport can pass method/path/body into this class without gaining any
// access to media buffers or MXL handles.
class ControlApi {
 public:
  using HealthProvider = std::function<std::string()>;

  explicit ControlApi(SwitcherCore& core,
                      HealthProvider health_provider = {});

  ApiResponse handle(std::string_view method, std::string_view path,
                     std::string_view body = {});
  ApiEvent current_event() const;
  std::optional<ApiEvent> wait_for_event(
      std::uint64_t after_revision,
      std::chrono::milliseconds timeout) const;

 private:
  SwitcherCore& core_;
  HealthProvider health_provider_;
};

}  // namespace mxl_core
