#pragma once

#include "mxl_core/core.hpp"

#include <string>
#include <string_view>

namespace mxl_core {

struct ApiResponse {
  int status;
  std::string content_type;
  std::string body;
};

// Dependency-free implementation of the versioned control contract. A small
// HTTP transport can pass method/path/body into this class without gaining any
// access to media buffers or MXL handles.
class ControlApi {
 public:
  explicit ControlApi(SwitcherCore& core);

  ApiResponse handle(std::string_view method, std::string_view path,
                     std::string_view body = {});

 private:
  SwitcherCore& core_;
};

}  // namespace mxl_core
