#include "mxl_core/live_config.hpp"

#include <filesystem>
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>
#include <unistd.h>

using namespace mxl_core;

namespace {

void require(bool condition, const std::string& message) {
  if (!condition) {
    throw std::runtime_error(message);
  }
}

class TempConfig {
 public:
  explicit TempConfig(std::string contents) {
    path_ = std::filesystem::temp_directory_path() /
            ("mxl-core-config-" + std::to_string(::getpid()) + "-" +
             std::to_string(next_id_++) + ".conf");
    std::ofstream output(path_);
    output << contents;
    if (!output) {
      throw std::runtime_error("could not write temporary config");
    }
  }

  ~TempConfig() { std::filesystem::remove(path_); }
  std::string path() const { return path_.string(); }

 private:
  inline static std::uint64_t next_id_{0};
  std::filesystem::path path_;
};

const std::string kValidConfig = R"CONFIG(
# Facility values stay outside the binary.
domain=/dev/shm/mxl/domain_1
source=cam_a|aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa|Camera A
source=clip|bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb|Clip Player
source=gfx|cccccccc-cccc-4ccc-8ccc-cccccccccccc|Graphics
program_definition=/etc/mxl-switcher/program.json
program_id=11111111-1111-4111-8111-111111111111
preview_definition=/etc/mxl-switcher/preview.json
preview_id=22222222-2222-4222-8222-222222222222
initial_program=cam_a
initial_preview=clip
width=1920
height=1080
rate_numerator=30
rate_denominator=1
frames=0
take_every=0
control_host=127.0.0.1
control_port=8080
)CONFIG";

void test_valid_config() {
  TempConfig file(kValidConfig);
  const auto config = load_live_config(file.path());
  require(config.sources.size() == 3, "all sources were not loaded");
  require(config.sources[2].id == "gfx" &&
              config.sources[2].label == "Graphics",
          "source fields were parsed incorrectly");
  require(config.format.rate_numerator == 30 &&
              config.format.rate_denominator == 1,
          "operating rate was parsed incorrectly");
  require(config.frames == 0 && config.control_port == 8080,
          "runtime settings were parsed incorrectly");
}

void require_rejected(const std::string& contents,
                      const std::string& message) {
  TempConfig file(contents);
  try {
    (void)load_live_config(file.path());
  } catch (const std::invalid_argument&) {
    return;
  }
  throw std::runtime_error(message);
}

void test_invalid_configs() {
  require_rejected(kValidConfig + "mystery=value\n",
                   "unknown key was accepted");
  require_rejected(kValidConfig + "control_port=9000\n",
                   "duplicate scalar key was accepted");

  auto missing_source = kValidConfig;
  const auto source = missing_source.find(
      "source=cam_a|aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa|Camera A\n");
  const auto source_end = missing_source.find('\n', source);
  missing_source.erase(source, source_end - source + 1);
  require_rejected(missing_source, "missing initial source was accepted");

  auto duplicate_flow = kValidConfig;
  duplicate_flow +=
      "source=duplicate|aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa|Duplicate\n";
  require_rejected(duplicate_flow, "duplicate source flow was accepted");

  auto output_collision = kValidConfig;
  const auto output_id = output_collision.find(
      "program_id=11111111-1111-4111-8111-111111111111");
  output_collision.replace(
      output_id, 47,
      "program_id=aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa");
  require_rejected(output_collision, "source/output UUID collision was accepted");
}

}  // namespace

int main() {
  try {
    test_valid_config();
    test_invalid_configs();
    std::cout << "All live config tests passed.\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "Live config test failed: " << error.what() << '\n';
    return 1;
  }
}
