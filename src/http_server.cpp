#include "mxl_core/http_server.hpp"

#include <arpa/inet.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <cerrno>
#include <cctype>
#include <cstring>
#include <limits>
#include <stdexcept>
#include <string_view>

namespace mxl_core {
namespace {

constexpr std::size_t kMaximumHeaderBytes = 16 * 1024;
constexpr std::size_t kMaximumBodyBytes = 64 * 1024;

std::runtime_error socket_error(std::string_view operation) {
  return std::runtime_error(std::string(operation) + ": " +
                            std::strerror(errno));
}

std::string lowercase(std::string_view input) {
  std::string result;
  result.reserve(input.size());
  for (const unsigned char character : input) {
    result += static_cast<char>(std::tolower(character));
  }
  return result;
}

std::string_view trim(std::string_view input) {
  while (!input.empty() &&
         std::isspace(static_cast<unsigned char>(input.front())) != 0) {
    input.remove_prefix(1);
  }
  while (!input.empty() &&
         std::isspace(static_cast<unsigned char>(input.back())) != 0) {
    input.remove_suffix(1);
  }
  return input;
}

std::string_view reason_phrase(int status) {
  switch (status) {
    case 200:
      return "OK";
    case 202:
      return "Accepted";
    case 400:
      return "Bad Request";
    case 404:
      return "Not Found";
    case 405:
      return "Method Not Allowed";
    case 413:
      return "Content Too Large";
    default:
      return "Internal Server Error";
  }
}

void send_all(int socket, std::string_view bytes) {
  while (!bytes.empty()) {
#ifdef MSG_NOSIGNAL
    const auto sent = ::send(socket, bytes.data(), bytes.size(), MSG_NOSIGNAL);
#else
    const auto sent = ::send(socket, bytes.data(), bytes.size(), 0);
#endif
    if (sent < 0) {
      if (errno == EINTR) {
        continue;
      }
      return;
    }
    if (sent == 0) {
      return;
    }
    bytes.remove_prefix(static_cast<std::size_t>(sent));
  }
}

void send_response(int socket, const ApiResponse& response) {
  const auto header =
      "HTTP/1.1 " + std::to_string(response.status) + " " +
      std::string(reason_phrase(response.status)) + "\r\nContent-Type: " +
      response.content_type + "\r\nContent-Length: " +
      std::to_string(response.body.size()) +
      "\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n";
  send_all(socket, header);
  send_all(socket, response.body);
}

ApiResponse transport_error(int status, std::string_view message) {
  return {status, "application/json",
          "{\"error\":\"" + std::string(message) + "\"}"};
}

bool parse_size(std::string_view input, std::size_t& value) {
  input = trim(input);
  if (input.empty()) {
    return false;
  }
  std::size_t result = 0;
  for (const unsigned char character : input) {
    if (std::isdigit(character) == 0) {
      return false;
    }
    const auto digit = static_cast<std::size_t>(character - '0');
    if (result > (std::numeric_limits<std::size_t>::max() - digit) / 10) {
      return false;
    }
    result = result * 10 + digit;
  }
  value = result;
  return true;
}

}  // namespace

HttpServer::HttpServer(ControlApi& api, std::string bind_address,
                       std::uint16_t port)
    : api_(api), bind_address_(std::move(bind_address)), port_(port) {
  if (bind_address_.empty()) {
    throw std::invalid_argument("HTTP bind address must not be empty");
  }
  listen_socket_ = ::socket(AF_INET, SOCK_STREAM, 0);
  if (listen_socket_ < 0) {
    throw socket_error("socket");
  }

  const int reuse = 1;
  if (::setsockopt(listen_socket_, SOL_SOCKET, SO_REUSEADDR, &reuse,
                   sizeof(reuse)) != 0) {
    const auto error = socket_error("setsockopt");
    ::close(listen_socket_);
    listen_socket_ = -1;
    throw error;
  }

  sockaddr_in address{};
  address.sin_family = AF_INET;
  address.sin_port = htons(port_);
  if (::inet_pton(AF_INET, bind_address_.c_str(), &address.sin_addr) != 1) {
    ::close(listen_socket_);
    listen_socket_ = -1;
    throw std::invalid_argument("HTTP bind address must be an IPv4 address");
  }
  if (::bind(listen_socket_, reinterpret_cast<sockaddr*>(&address),
             sizeof(address)) != 0) {
    const auto error = socket_error("bind");
    ::close(listen_socket_);
    listen_socket_ = -1;
    throw error;
  }
  if (::listen(listen_socket_, 16) != 0) {
    const auto error = socket_error("listen");
    ::close(listen_socket_);
    listen_socket_ = -1;
    throw error;
  }

  sockaddr_in bound{};
  socklen_t bound_size = sizeof(bound);
  if (::getsockname(listen_socket_, reinterpret_cast<sockaddr*>(&bound),
                    &bound_size) != 0) {
    const auto error = socket_error("getsockname");
    ::close(listen_socket_);
    listen_socket_ = -1;
    throw error;
  }
  port_ = ntohs(bound.sin_port);
  thread_ = std::thread(&HttpServer::run, this);
}

HttpServer::~HttpServer() {
  running_ = false;
  if (listen_socket_ >= 0) {
    ::shutdown(listen_socket_, SHUT_RDWR);
    ::close(listen_socket_);
    listen_socket_ = -1;
  }
  if (thread_.joinable()) {
    thread_.join();
  }
}

std::uint16_t HttpServer::port() const { return port_; }

const std::string& HttpServer::bind_address() const { return bind_address_; }

void HttpServer::run() {
  while (running_) {
    const auto client = ::accept(listen_socket_, nullptr, nullptr);
    if (client < 0) {
      if (errno == EINTR) {
        continue;
      }
      if (!running_ || errno == EBADF || errno == EINVAL) {
        return;
      }
      continue;
    }
    handle_connection(client);
    ::close(client);
  }
}

void HttpServer::handle_connection(int client_socket) noexcept {
  try {
    std::string request;
    request.reserve(4096);
    char buffer[4096];
    std::size_t header_end = std::string::npos;
    while ((header_end = request.find("\r\n\r\n")) == std::string::npos) {
      const auto received = ::recv(client_socket, buffer, sizeof(buffer), 0);
      if (received <= 0) {
        return;
      }
      request.append(buffer, static_cast<std::size_t>(received));
      if (request.size() > kMaximumHeaderBytes) {
        send_response(client_socket,
                      transport_error(413, "request headers are too large"));
        return;
      }
    }

    const auto first_line_end = request.find("\r\n");
    if (first_line_end == std::string::npos) {
      send_response(client_socket, transport_error(400, "bad request line"));
      return;
    }
    const std::string_view first_line(request.data(), first_line_end);
    const auto method_end = first_line.find(' ');
    const auto path_end = method_end == std::string_view::npos
                              ? std::string_view::npos
                              : first_line.find(' ', method_end + 1);
    if (method_end == std::string_view::npos ||
        path_end == std::string_view::npos ||
        first_line.substr(path_end + 1) != "HTTP/1.1") {
      send_response(client_socket, transport_error(400, "bad request line"));
      return;
    }
    const auto method = first_line.substr(0, method_end);
    auto path = first_line.substr(method_end + 1, path_end - method_end - 1);
    if (const auto query = path.find('?'); query != std::string_view::npos) {
      path = path.substr(0, query);
    }

    std::size_t content_length = 0;
    auto line_start = first_line_end + 2;
    while (line_start < header_end) {
      const auto line_end = request.find("\r\n", line_start);
      if (line_end == std::string::npos || line_end > header_end) {
        break;
      }
      const std::string_view line(request.data() + line_start,
                                  line_end - line_start);
      const auto colon = line.find(':');
      if (colon == std::string_view::npos) {
        send_response(client_socket,
                      transport_error(400, "bad request header"));
        return;
      }
      if (lowercase(trim(line.substr(0, colon))) == "content-length" &&
          !parse_size(line.substr(colon + 1), content_length)) {
        send_response(client_socket,
                      transport_error(400, "bad content length"));
        return;
      }
      line_start = line_end + 2;
    }
    if (content_length > kMaximumBodyBytes) {
      send_response(client_socket,
                    transport_error(413, "request body is too large"));
      return;
    }

    const auto body_start = header_end + 4;
    const auto required_size = body_start + content_length;
    while (request.size() < required_size) {
      const auto received = ::recv(client_socket, buffer, sizeof(buffer), 0);
      if (received <= 0) {
        send_response(client_socket,
                      transport_error(400, "incomplete request body"));
        return;
      }
      request.append(buffer, static_cast<std::size_t>(received));
    }
    const std::string_view body(request.data() + body_start, content_length);
    send_response(client_socket, api_.handle(method, path, body));
  } catch (...) {
    send_response(client_socket, transport_error(500, "internal server error"));
  }
}

}  // namespace mxl_core
