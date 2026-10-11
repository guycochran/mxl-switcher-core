#include "mxl_core/core.hpp"
#include "mxl_core/control_api.hpp"
#include "mxl_core/http_server.hpp"
#include "mxl_core/live_config.hpp"
#include "mxl_core/mxl_backend.hpp"

#include <atomic>
#include <csignal>
#include <fstream>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <unordered_map>

using namespace mxl_core;

namespace {
std::atomic_bool running{true};

void stop(int) { running = false; }

std::string json_string(std::string_view value) {
  std::string result{"\""};
  for (const auto character : value) {
    if (character == '\"' || character == '\\') {
      result += '\\';
    }
    result += character;
  }
  result += '\"';
  return result;
}

std::string health_json(const MxlBackendStats& stats) {
  std::ostringstream output;
  output << "{\"status\":\"running\",\"routed\":" << stats.routed
         << ",\"invalid\":" << stats.invalid
         << ",\"too_early\":" << stats.too_early
         << ",\"too_late\":" << stats.too_late
         << ",\"timeouts\":" << stats.timeouts
         << ",\"reader_reopens\":" << stats.reader_reopens
         << ",\"sync_waits\":" << stats.sync_waits
         << ",\"sync_timeouts\":" << stats.sync_timeouts
         << ",\"sources\":[";
  bool first = true;
  for (const auto& [flow_id, source] : stats.sources) {
    if (!first) {
      output << ',';
    }
    first = false;
    output << "{\"flow_id\":" << json_string(flow_id)
           << ",\"routed\":" << source.routed
           << ",\"invalid\":" << source.invalid
           << ",\"too_early\":" << source.too_early
           << ",\"too_late\":" << source.too_late
           << ",\"timeouts\":" << source.timeouts
           << ",\"reader_reopens\":" << source.reader_reopens << '}';
  }
  output << "]}";
  return output.str();
}

std::string read_file(const std::string& path) {
  std::ifstream input(path);
  if (!input) {
    throw std::runtime_error("cannot open: " + path);
  }
  return {std::istreambuf_iterator<char>(input),
          std::istreambuf_iterator<char>()};
}

std::unordered_map<std::string, std::string> parse_args(int argc, char** argv) {
  std::unordered_map<std::string, std::string> args;
  for (int i = 1; i < argc; i += 2) {
    if (i + 1 >= argc || std::string(argv[i]).rfind("--", 0) != 0) {
      throw std::invalid_argument("arguments must be --name value pairs");
    }
    args.emplace(std::string(argv[i]).substr(2), argv[i + 1]);
  }
  return args;
}

const std::string& required(
    const std::unordered_map<std::string, std::string>& args,
    std::string_view name) {
  const auto found = args.find(std::string(name));
  if (found == args.end() || found->second.empty()) {
    throw std::invalid_argument("missing --" + std::string(name));
  }
  return found->second;
}

LiveConfig config_from_args(
    const std::unordered_map<std::string, std::string>& args) {
  if (args.contains("config")) {
    if (args.size() != 1) {
      throw std::invalid_argument(
          "--config cannot be combined with individual settings");
    }
    return load_live_config(args.at("config"));
  }

  LiveConfig config;
  config.domain = required(args, "domain");
  config.sources = {{"src_a", required(args, "source-a"), "Source A"},
                    {"src_b", required(args, "source-b"), "Source B"}};
  config.program_definition_path = required(args, "program-def");
  config.program_id = required(args, "program-id");
  config.preview_definition_path = required(args, "preview-def");
  config.preview_id = required(args, "preview-id");
  config.initial_program = "src_a";
  config.initial_preview = "src_b";
  config.frames =
      args.contains("frames") ? std::stoull(args.at("frames")) : 300ULL;
  config.take_every = args.contains("take-every")
                          ? std::stoull(args.at("take-every"))
                          : 90ULL;
  config.input_delay_frames =
      args.contains("input-delay-frames")
          ? std::stoull(args.at("input-delay-frames"))
          : 0ULL;
  config.format.rate_numerator =
      args.contains("rate-numerator")
          ? static_cast<std::uint32_t>(std::stoul(args.at("rate-numerator")))
          : 30000U;
  config.format.rate_denominator =
      args.contains("rate-denominator")
          ? static_cast<std::uint32_t>(std::stoul(args.at("rate-denominator")))
          : 1001U;
  config.control_host = args.contains("control-host")
                            ? args.at("control-host")
                            : "127.0.0.1";
  const auto port = args.contains("control-port")
                        ? std::stoul(args.at("control-port"))
                        : 8080UL;
  if (port > 65535) {
    throw std::invalid_argument("control port must be between 1 and 65535");
  }
  config.control_port = static_cast<std::uint16_t>(port);
  validate_live_config(config);
  return config;
}
}  // namespace

int main(int argc, char** argv) {
  try {
    const auto args = parse_args(argc, argv);
    if (args.contains("check-config")) {
      if (args.size() != 1) {
        throw std::invalid_argument(
            "--check-config cannot be combined with other settings");
      }
      const auto config = load_live_config(args.at("check-config"));
      std::cout << "config valid: " << config.sources.size()
                << " sources, control " << config.control_host << ':'
                << config.control_port << '\n';
      return 0;
    }
    const auto config = config_from_args(args);
    std::vector<Source> sources;
    sources.reserve(config.sources.size());
    for (const auto& source : config.sources) {
      sources.push_back(
          {source.id, source.flow_id, source.label, config.format});
    }
    MxlBackend media(
        config.domain,
        {{config.program_id, read_file(config.program_definition_path)},
         {config.preview_id, read_file(config.preview_definition_path)}},
        5'000'000, config.input_delay_frames);
    SwitcherCore core(
        media, std::move(sources),
        {config.program_id, "MXL Switcher Program", config.format},
        {config.preview_id, "MXL Switcher Preview", config.format},
        config.initial_program, config.initial_preview);
    ControlApi control_api(core,
                           [&media] { return health_json(media.stats()); });
    HttpServer http(control_api, config.control_host, config.control_port);
    std::cout << "control=http://" << http.bind_address() << ':' << http.port()
              << "/api/mxl/v1" << '\n';

    std::signal(SIGINT, stop);
    std::signal(SIGTERM, stop);
    auto index = media.current_index(config.format) + 2;
    std::uint64_t processed = 0;
    while (running && (config.frames == 0 || processed < config.frames)) {
      media.sleep_until_index(index, config.format);
      if (config.take_every != 0 && processed != 0 &&
          (processed % config.take_every) == 0) {
        core.take();
      }
      core.process(index);
      ++index;
      ++processed;
    }

    const auto stats = media.stats();
    const auto state = core.state();
    std::cout << "frames=" << processed << " routed=" << stats.routed
              << " invalid=" << stats.invalid
              << " too_early=" << stats.too_early
              << " too_late=" << stats.too_late
              << " timeouts=" << stats.timeouts
              << " reader_reopens=" << stats.reader_reopens
              << " sync_waits=" << stats.sync_waits
              << " sync_timeouts=" << stats.sync_timeouts
              << " revision=" << state.revision << '\n';
    for (const auto& source : core.sources()) {
      const auto found = stats.sources.find(source.flow_id);
      const auto source_stats = found == stats.sources.end()
                                    ? MxlSourceStats{}
                                    : found->second;
      std::cout << "source=" << source.id << " flow=" << source.flow_id
                << " routed=" << source_stats.routed
                << " invalid=" << source_stats.invalid
                << " too_early=" << source_stats.too_early
                << " too_late=" << source_stats.too_late
                << " timeouts=" << source_stats.timeouts
                << " reader_reopens=" << source_stats.reader_reopens << '\n';
    }
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "mxl-core-live: " << error.what() << '\n';
    std::cerr
        << "usage: mxl-core-live --config FILE\n"
           "       mxl-core-live --check-config FILE\n"
           "   or: mxl-core-live --domain PATH --source-a UUID --source-b UUID "
           "--program-def FILE --program-id UUID --preview-def FILE "
           "--preview-id UUID [--frames N] [--take-every N] "
           "[--input-delay-frames N] "
           "[--rate-numerator N] [--rate-denominator N] "
           "[--control-host IPv4] [--control-port N]\n";
    return 2;
  }
}
