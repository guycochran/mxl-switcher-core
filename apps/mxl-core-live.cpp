#include "mxl_core/core.hpp"
#include "mxl_core/mxl_backend.hpp"

#include <atomic>
#include <csignal>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <unordered_map>

using namespace mxl_core;

namespace {
std::atomic_bool running{true};

void stop(int) { running = false; }

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
}  // namespace

int main(int argc, char** argv) {
  try {
    const auto args = parse_args(argc, argv);
    const auto& domain = required(args, "domain");
    const auto& source_a = required(args, "source-a");
    const auto& source_b = required(args, "source-b");
    const auto& program_def_path = required(args, "program-def");
    const auto& preview_def_path = required(args, "preview-def");
    const auto& program_id = required(args, "program-id");
    const auto& preview_id = required(args, "preview-id");
    const auto frames = args.contains("frames")
                            ? std::stoull(args.at("frames"))
                            : 300ULL;
    const auto take_every = args.contains("take-every")
                                ? std::stoull(args.at("take-every"))
                                : 90ULL;
    const auto rate_numerator = args.contains("rate-numerator")
                                    ? std::stoul(args.at("rate-numerator"))
                                    : 30000UL;
    const auto rate_denominator = args.contains("rate-denominator")
                                      ? std::stoul(args.at("rate-denominator"))
                                      : 1001UL;

    const VideoFormat v210{1920, 1080,
                           static_cast<std::uint32_t>(rate_numerator),
                           static_cast<std::uint32_t>(rate_denominator),
                           "video/v210"};
    MxlBackend media(
        domain,
        {{program_id, read_file(program_def_path)},
         {preview_id, read_file(preview_def_path)}});
    SwitcherCore core(
        media,
        {{"src_a", source_a, "Source A", v210},
         {"src_b", source_b, "Source B", v210}},
        {program_id, "MXL Switcher Program", v210},
        {preview_id, "MXL Switcher Preview", v210}, "src_a", "src_b");

    std::signal(SIGINT, stop);
    std::signal(SIGTERM, stop);
    auto index = media.current_index(v210) + 2;
    std::uint64_t processed = 0;
    while (running && (frames == 0 || processed < frames)) {
      media.sleep_until_index(index, v210);
      if (take_every != 0 && processed != 0 &&
          (processed % take_every) == 0) {
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
        << "usage: mxl-core-live --domain PATH --source-a UUID --source-b UUID "
           "--program-def FILE --program-id UUID --preview-def FILE "
           "--preview-id UUID [--frames N] [--take-every N] "
           "[--rate-numerator N] [--rate-denominator N]\n";
    return 2;
  }
}
