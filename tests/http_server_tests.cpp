#include "mxl_core/http_server.hpp"

#include <arpa/inet.h>
#include <sys/socket.h>
#include <unistd.h>

#include <iostream>
#include <stdexcept>
#include <string>
#include <string_view>

using namespace mxl_core;

namespace {

const VideoFormat kV210{1920, 1080, 30, 1, "video/v210"};

void require(bool condition, const std::string& message) {
  if (!condition) {
    throw std::runtime_error(message);
  }
}

std::string request(std::uint16_t port, std::string_view bytes) {
  const auto socket = ::socket(AF_INET, SOCK_STREAM, 0);
  if (socket < 0) {
    throw std::runtime_error("could not create client socket");
  }
  sockaddr_in address{};
  address.sin_family = AF_INET;
  address.sin_port = htons(port);
  ::inet_pton(AF_INET, "127.0.0.1", &address.sin_addr);
  if (::connect(socket, reinterpret_cast<sockaddr*>(&address),
                sizeof(address)) != 0) {
    ::close(socket);
    throw std::runtime_error("could not connect to test server");
  }
  const auto sent = ::send(socket, bytes.data(), bytes.size(), 0);
  if (sent != static_cast<ssize_t>(bytes.size())) {
    ::close(socket);
    throw std::runtime_error("could not send complete test request");
  }
  std::string response;
  char buffer[4096];
  ssize_t received = 0;
  while ((received = ::recv(socket, buffer, sizeof(buffer), 0)) > 0) {
    response.append(buffer, static_cast<std::size_t>(received));
  }
  ::close(socket);
  return response;
}

int open_client(std::uint16_t port) {
  const auto socket = ::socket(AF_INET, SOCK_STREAM, 0);
  if (socket < 0) {
    throw std::runtime_error("could not create event client socket");
  }
  sockaddr_in address{};
  address.sin_family = AF_INET;
  address.sin_port = htons(port);
  ::inet_pton(AF_INET, "127.0.0.1", &address.sin_addr);
  if (::connect(socket, reinterpret_cast<sockaddr*>(&address),
                sizeof(address)) != 0) {
    ::close(socket);
    throw std::runtime_error("could not connect event client");
  }
  timeval timeout{2, 0};
  (void)::setsockopt(socket, SOL_SOCKET, SO_RCVTIMEO, &timeout,
                     sizeof(timeout));
  return socket;
}

std::string receive_until(int socket, std::string_view expected) {
  std::string response;
  char buffer[4096];
  while (response.find(expected) == std::string::npos) {
    const auto received = ::recv(socket, buffer, sizeof(buffer), 0);
    if (received <= 0) {
      throw std::runtime_error("event stream ended before expected data");
    }
    response.append(buffer, static_cast<std::size_t>(received));
  }
  return response;
}

struct Fixture {
  MemoryBackend backend;
  SwitcherCore core;
  ControlApi api;
  HttpServer server;

  Fixture()
      : core(add_sources(backend),
             {{"src_a", "flow-a", "Camera A", kV210},
              {"src_b", "flow-b", "Camera B", kV210}},
             {"program-flow", "Program", kV210},
             {"preview-flow", "Preview", kV210}, "src_a", "src_b"),
        api(core),
        server(api, "127.0.0.1", 0) {}

 private:
  static MemoryBackend& add_sources(MemoryBackend& backend) {
    backend.add_source("flow-a", kV210, 0xa1);
    backend.add_source("flow-b", kV210, 0xb2);
    return backend;
  }
};

void test_http_reads_and_writes() {
  Fixture fixture;
  const auto state = request(
      fixture.server.port(),
      "GET /api/mxl/v1/state HTTP/1.1\r\nHost: localhost\r\n\r\n");
  require(state.find("HTTP/1.1 200 OK") == 0 &&
              state.find("\"program\":\"src_a\"") != std::string::npos,
          "state was not served over HTTP");

  const auto cut = request(
      fixture.server.port(),
      "POST /api/mxl/v1/cut HTTP/1.1\r\nHost: localhost\r\nContent-Type: "
      "application/json\r\nContent-Length: 18\r\n\r\n{\"source\":\"src_b\"}");
  require(cut.find("HTTP/1.1 202 Accepted") == 0 &&
              cut.find("\"command_id\":1") != std::string::npos,
          "cut was not accepted over HTTP");
  require(fixture.core.state().program == "src_a",
          "HTTP command changed state between frames");
  fixture.core.process(1);
  require(fixture.core.state().program == "src_b",
          "HTTP command missed the next frame boundary");
}

void test_http_limits_and_routes() {
  Fixture fixture;
  const auto missing = request(
      fixture.server.port(),
      "GET /missing HTTP/1.1\r\nHost: localhost\r\n\r\n");
  require(missing.find("HTTP/1.1 404 Not Found") == 0,
          "missing route did not return 404");
  const auto bad = request(
      fixture.server.port(),
      "POST /api/mxl/v1/cut HTTP/1.1\r\nHost: localhost\r\nContent-Length: "
      "999999\r\n\r\n");
  require(bad.find("HTTP/1.1 413 Content Too Large") == 0,
          "oversized body was not rejected");
}

void test_event_stream() {
  Fixture fixture;
  const auto socket = open_client(fixture.server.port());
  constexpr std::string_view event_request =
      "GET /api/mxl/v1/events HTTP/1.1\r\nHost: localhost\r\n\r\n";
  if (::send(socket, event_request.data(), event_request.size(), 0) !=
      static_cast<ssize_t>(event_request.size())) {
    ::close(socket);
    throw std::runtime_error("could not request event stream");
  }
  const auto initial = receive_until(socket, "id: 0\n");
  require(initial.find("Content-Type: text/event-stream") != std::string::npos &&
              initial.find("event: state\n") != std::string::npos,
          "initial SSE state event is missing");

  const auto take = request(
      fixture.server.port(),
      "POST /api/mxl/v1/take HTTP/1.1\r\nHost: localhost\r\n"
      "Content-Length: 0\r\n\r\n");
  require(take.find("HTTP/1.1 202 Accepted") == 0,
          "concurrent control request failed while SSE was open");
  fixture.core.process(1);
  const auto changed = receive_until(socket, "id: 1\n");
  require(changed.find("\"revision\":1") != std::string::npos,
          "SSE did not deliver the applied revision");
  ::shutdown(socket, SHUT_RDWR);
  ::close(socket);

  const auto resumed_socket = open_client(fixture.server.port());
  constexpr std::string_view resumed_request =
      "GET /api/mxl/v1/events HTTP/1.1\r\nHost: localhost\r\n"
      "Last-Event-ID: 0\r\n\r\n";
  if (::send(resumed_socket, resumed_request.data(), resumed_request.size(), 0) !=
      static_cast<ssize_t>(resumed_request.size())) {
    ::close(resumed_socket);
    throw std::runtime_error("could not resume event stream");
  }
  const auto resumed = receive_until(resumed_socket, "id: 1\n");
  require(resumed.find("\"revision\":1") != std::string::npos,
          "SSE resume did not replay the newer current revision");
  ::shutdown(resumed_socket, SHUT_RDWR);
  ::close(resumed_socket);
}

}  // namespace

int main() {
  try {
    test_http_reads_and_writes();
    test_http_limits_and_routes();
    test_event_stream();
    std::cout << "All HTTP transport tests passed.\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "HTTP transport test failed: " << error.what() << '\n';
    return 1;
  }
}
