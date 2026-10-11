#include <mxl/dataformat.h>
#include <mxl/flow.h>
#include <mxl/time.h>

#include <array>
#include <atomic>
#include <csignal>
#include <cstdint>
#include <cstring>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <unordered_map>

namespace {
std::atomic_bool running{true};

void stop(int) { running = false; }

std::runtime_error sdk_error(const char* operation, mxlStatus status) {
  return std::runtime_error(std::string(operation) + " failed with MXL status " +
                            std::to_string(static_cast<int>(status)));
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

std::uint32_t v210_word(std::uint16_t first, std::uint16_t second,
                        std::uint16_t third) {
  return static_cast<std::uint32_t>(first) |
         (static_cast<std::uint32_t>(second) << 10U) |
         (static_cast<std::uint32_t>(third) << 20U);
}

std::array<std::uint32_t, 4> v210_black(std::uint16_t luma) {
  constexpr std::uint16_t chroma = 512;
  return {v210_word(chroma, luma, chroma),
          v210_word(luma, chroma, luma),
          v210_word(chroma, luma, chroma),
          v210_word(luma, chroma, luma)};
}

class Writer {
 public:
  Writer(mxlInstance instance, const std::string& definition,
         std::uint16_t luma)
      : instance_(instance), pattern_(v210_black(luma)) {
    bool created = false;
    const auto status = mxlCreateFlowWriter(instance_, definition.c_str(),
                                             nullptr, &handle_, &config_,
                                             &created);
    if (status != MXL_STATUS_OK) {
      throw sdk_error("mxlCreateFlowWriter", status);
    }
    if (!created || config_.common.format != MXL_DATA_FORMAT_VIDEO ||
        config_.common.payloadLocation != MXL_PAYLOAD_LOCATION_HOST_MEMORY) {
      mxlReleaseFlowWriter(instance_, handle_);
      handle_ = nullptr;
      throw std::runtime_error(
          "test source requires a new host-memory video flow");
    }
  }

  ~Writer() {
    if (handle_ != nullptr) {
      mxlReleaseFlowWriter(instance_, handle_);
    }
  }

  Writer(const Writer&) = delete;
  Writer& operator=(const Writer&) = delete;

  const mxlRational& rate() const { return config_.common.grainRate; }
  std::uint32_t grain_count() const { return config_.discrete.grainCount; }

  void write(std::uint64_t index) {
    mxlGrainInfo info{};
    std::uint8_t* payload = nullptr;
    auto status = mxlFlowWriterOpenGrain(handle_, index, &info, &payload);
    if (status != MXL_STATUS_OK) {
      throw sdk_error("mxlFlowWriterOpenGrain", status);
    }
    if (payload == nullptr || info.grainSize == 0) {
      throw std::runtime_error("MXL writer returned an empty video grain");
    }
    const auto* bytes = reinterpret_cast<const std::uint8_t*>(pattern_.data());
    constexpr auto pattern_size = sizeof(std::uint32_t) * 4U;
    for (std::size_t offset = 0; offset < info.grainSize;
         offset += pattern_size) {
      const auto remaining = static_cast<std::size_t>(info.grainSize) - offset;
      std::memcpy(payload + offset, bytes,
                  remaining < pattern_size ? remaining : pattern_size);
    }
    info.flags = 0;
    info.validSlices = info.totalSlices;
    status = mxlFlowWriterCommitGrain(handle_, &info);
    if (status != MXL_STATUS_OK) {
      throw sdk_error("mxlFlowWriterCommitGrain", status);
    }
  }

 private:
  mxlInstance instance_{nullptr};
  mxlFlowWriter handle_{nullptr};
  mxlFlowConfigInfo config_{};
  std::array<std::uint32_t, 4> pattern_{};
};
}  // namespace

int main(int argc, char** argv) {
  mxlInstance instance = nullptr;
  try {
    const auto args = parse_args(argc, argv);
    const auto& domain = required(args, "domain");
    const auto frames =
        args.contains("frames") ? std::stoull(args.at("frames")) : 0ULL;
    instance = mxlCreateInstance(domain.c_str(), nullptr);
    if (instance == nullptr) {
      throw std::runtime_error("could not open MXL domain: " + domain);
    }

    std::uint64_t processed = 0;
    {
      Writer first(instance, read_file(required(args, "flow-a-def")), 64);
      Writer second(instance, read_file(required(args, "flow-b-def")), 320);
      if (first.rate().numerator != second.rate().numerator ||
          first.rate().denominator != second.rate().denominator) {
        throw std::invalid_argument("test flows must share a grain rate");
      }
      std::cout << "test_sources=ready grain_count=" << first.grain_count()
                << ',' << second.grain_count() << '\n';

      std::signal(SIGINT, stop);
      std::signal(SIGTERM, stop);
      const auto rate = first.rate();
      auto index = mxlGetCurrentIndex(&rate) + 2;
      while (running && (frames == 0 || processed < frames)) {
        mxlSleepForNs(mxlGetNsUntilIndex(index, &rate));
        first.write(index);
        second.write(index);
        ++index;
        ++processed;
      }
    }
    std::cout << "frames=" << processed << " outputs=" << processed * 2U
              << '\n';
    mxlDestroyInstance(instance);
    instance = nullptr;
    return 0;
  } catch (const std::exception& error) {
    if (instance != nullptr) {
      mxlDestroyInstance(instance);
    }
    std::cerr << "mxl-test-source: " << error.what() << '\n';
    std::cerr << "usage: mxl-test-source --domain PATH --flow-a-def FILE "
                 "--flow-b-def FILE [--frames N]\n";
    return 2;
  }
}
