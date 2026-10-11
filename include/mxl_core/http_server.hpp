#pragma once

#include "mxl_core/control_api.hpp"

#include <atomic>
#include <cstdint>
#include <string>
#include <thread>

namespace mxl_core {

// A deliberately small HTTP/1.1 transport for the ControlApi. It handles one
// bounded request per connection, closes the connection after each response,
// and has no access to the media backend.
class HttpServer {
 public:
  HttpServer(ControlApi& api, std::string bind_address = "127.0.0.1",
             std::uint16_t port = 8080);
  ~HttpServer();

  HttpServer(const HttpServer&) = delete;
  HttpServer& operator=(const HttpServer&) = delete;

  std::uint16_t port() const;
  const std::string& bind_address() const;

 private:
  void run();
  void handle_connection(int client_socket) noexcept;

  ControlApi& api_;
  std::string bind_address_;
  std::uint16_t port_;
  int listen_socket_{-1};
  std::atomic_bool running_{true};
  std::thread thread_;
};

}  // namespace mxl_core
