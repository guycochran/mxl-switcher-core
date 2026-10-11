#pragma once

#include "mxl_core/control_api.hpp"

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <mutex>
#include <optional>
#include <string>
#include <thread>
#include <unordered_set>

namespace mxl_core {

// A deliberately small HTTP/1.1 transport for the ControlApi. Ordinary
// requests are bounded and closed after each response. The events endpoint is
// a persistent SSE connection. The transport has no access to the media backend.
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
  void serve_events(int client_socket,
                    std::optional<std::uint64_t> last_event_id) noexcept;
  void release_connection(int client_socket) noexcept;

  ControlApi& api_;
  std::string bind_address_;
  std::uint16_t port_;
  int listen_socket_{-1};
  std::atomic_bool running_{true};
  std::thread thread_;
  std::mutex connections_mutex_;
  std::condition_variable connections_changed_;
  std::unordered_set<int> connections_;
};

}  // namespace mxl_core
