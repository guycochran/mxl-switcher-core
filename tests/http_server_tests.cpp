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

}  // namespace

int main() {
  try {
    test_http_reads_and_writes();
    test_http_limits_and_routes();
    std::cout << "All HTTP transport tests passed.\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "HTTP transport test failed: " << error.what() << '\n';
    return 1;
  }
}
