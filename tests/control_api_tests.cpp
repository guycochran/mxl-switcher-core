#include "mxl_core/control_api.hpp"

#include <chrono>
#include <iostream>
#include <stdexcept>
#include <string>

using namespace mxl_core;

namespace {

constexpr const char* kProgramFlow = "11111111-1111-4111-8111-111111111111";
constexpr const char* kPreviewFlow = "22222222-2222-4222-8222-222222222222";
const VideoFormat kV210{1920, 1080, 30, 1, "video/v210"};

void require(bool condition, const std::string& message) {
  if (!condition) {
    throw std::runtime_error(message);
  }
}

struct Fixture {
  MemoryBackend backend;
  SwitcherCore core;
  ControlApi api;

  Fixture()
      : core(add_sources(backend),
             {{"src_a", "flow-a", "Camera \"A\"", kV210},
              {"src_b", "flow-b", "Camera B", kV210}},
             {kProgramFlow, "Program", kV210},
             {kPreviewFlow, "Preview", kV210}, "src_a", "src_b"),
        api(core) {}

 private:
  static MemoryBackend& add_sources(MemoryBackend& backend) {
    backend.add_source("flow-a", kV210, 0xa1);
    backend.add_source("flow-b", kV210, 0xb2);
    return backend;
  }
};

void test_reads() {
  Fixture fixture;
  const auto meta = fixture.api.handle("GET", "/api/mxl/v1/meta");
  require(meta.status == 200 && meta.body.find("\"api_version\":\"v1\"") !=
                                    std::string::npos,
          "meta contract missing");
  const auto state = fixture.api.handle("GET", "/api/mxl/v1/state");
  require(state.status == 200 &&
              state.body.find("\"program\":\"src_a\"") != std::string::npos &&
              state.body.find("\"last_output_index\":null") !=
                  std::string::npos,
          "state response is wrong");
  const auto sources = fixture.api.handle("GET", "/api/mxl/v1/sources");
  require(sources.status == 200 &&
              sources.body.find("Camera \\\"A\\\"") != std::string::npos &&
              sources.body.find("\"encoding\":\"video/v210\"") !=
                  std::string::npos,
          "source response is incomplete or not escaped");
}

void test_commands_wait_for_frame_boundary() {
  Fixture fixture;
  const auto preview = fixture.api.handle(
      "POST", "/api/mxl/v1/preview", "{\"source\":\"src_a\"}");
  require(preview.status == 202 &&
              preview.body.find("\"command_id\":1") != std::string::npos,
          "preview command was not accepted");
  require(fixture.core.state().preview == "src_b",
          "control API applied a command between frames");
  fixture.core.process(1);
  require(fixture.core.state().preview == "src_a",
          "preview command missed the frame boundary");

  const auto take = fixture.api.handle("POST", "/api/mxl/v1/take");
  require(take.status == 202, "take command was not accepted");
  fixture.core.process(2);
  require(fixture.core.state().program == "src_a" &&
              fixture.core.state().preview == "src_a",
          "take command was not applied");
}

void test_errors() {
  Fixture fixture;
  require(fixture.api.handle("POST", "/api/mxl/v1/cut", "{}").status == 400,
          "missing source was accepted");
  require(fixture.api
              .handle("POST", "/api/mxl/v1/cut",
                      "{\"source\":\"not-registered\"}")
              .status == 400,
          "unknown source was accepted");
  require(fixture.api.handle("GET", "/nope").status == 404,
          "unknown route did not return 404");
  require(fixture.api.handle("DELETE", "/api/mxl/v1/state").status == 405,
          "unsupported API method did not return 405");
}

void test_events_and_health() {
  Fixture fixture;
  ControlApi api(fixture.core,
                 [] { return std::string{"{\"status\":\"test\"}"}; });
  const auto health = api.handle("GET", "/api/mxl/v1/health");
  require(health.status == 200 &&
              health.body == "{\"status\":\"test\"}",
          "health provider was not exposed");

  const auto initial = api.current_event();
  require(initial.id == 0 && initial.type == "state",
          "initial state event is wrong");
  require(!api.wait_for_event(0, std::chrono::milliseconds(1)),
          "event wait returned without a new revision");
  fixture.core.take();
  fixture.core.process(1);
  const auto changed = api.wait_for_event(0, std::chrono::milliseconds(1));
  require(changed && changed->id == 1 &&
              changed->data.find("\"program\":\"src_b\"") !=
                  std::string::npos,
          "state event did not carry the applied revision");
}

void test_runtime_source_api() {
  Fixture fixture;
  fixture.backend.add_source("flow-playout", kV210, 0xc3);
  const std::string body =
      "{\"id\":\"playout\",\"flow_id\":\"flow-playout\","
      "\"label\":\"Clip Player\",\"kind\":\"playout\","
      "\"capabilities\":[\"video\",\"playback\",\"timecode\"]}";
  const auto created =
      fixture.api.handle("POST", "/api/mxl/v1/sources", body);
  require(created.status == 201 &&
              created.body.find("\"changed\":true") != std::string::npos,
          "runtime source API did not create a source");
  const auto duplicate =
      fixture.api.handle("POST", "/api/mxl/v1/sources", body);
  require(duplicate.status == 200 &&
              duplicate.body.find("\"changed\":false") != std::string::npos,
          "runtime source API was not idempotent");
  const auto sources = fixture.api.handle("GET", "/api/mxl/v1/sources");
  require(sources.body.find("\"kind\":\"playout\"") !=
              std::string::npos &&
              sources.body.find("\"playback\"") != std::string::npos &&
              sources.body.find("\"audio_flow_id\":null") !=
                  std::string::npos,
          "runtime source metadata was not exposed");

  fixture.api.handle("POST", "/api/mxl/v1/preview",
                     "{\"source\":\"playout\"}");
  fixture.core.process(1);
  require(fixture.api
              .handle("DELETE", "/api/mxl/v1/sources/playout")
              .status == 400,
          "source API removed a source on preview");
  fixture.api.handle("POST", "/api/mxl/v1/preview",
                     "{\"source\":\"src_b\"}");
  fixture.core.process(2);
  const auto removed = fixture.api.handle(
      "DELETE", "/api/mxl/v1/sources/playout");
  require(removed.status == 200 &&
              removed.body.find("\"changed\":true") != std::string::npos,
          "source API did not remove an off-air source");
  require(fixture.api.handle("POST", "/api/mxl/v1/sources", "{}").status ==
              400,
          "source API accepted an incomplete registration");
}

void test_source_rebind_api() {
  Fixture fixture;
  fixture.backend.add_source("flow-a-restarted", kV210, 0xd4);
  const auto rebound = fixture.api.handle(
      "PUT", "/api/mxl/v1/sources/src_a",
      "{\"flow_id\":\"flow-a-restarted\","
      "\"audio_flow_id\":\"audio-a-restarted\"}");
  require(rebound.status == 200 &&
              rebound.body.find("\"changed\":true") != std::string::npos &&
              rebound.body.find("\"previous_flow_id\":\"flow-a\"") !=
                  std::string::npos &&
              rebound.body.find("\"flow_id\":\"flow-a-restarted\"") !=
                  std::string::npos,
          "source rebind API did not return the binding audit result");
  require(fixture.core.state().program == "src_a",
          "source rebind API changed the Program source identity");
  fixture.core.process(1);
  require((*fixture.backend.writes(kProgramFlow).back().grain.payload)[0] ==
              0xd4,
          "source rebind API did not affect the next Program frame");

  const auto duplicate = fixture.api.handle(
      "PUT", "/api/mxl/v1/sources/src_a",
      "{\"flow_id\":\"flow-a-restarted\","
      "\"audio_flow_id\":\"audio-a-restarted\"}");
  require(duplicate.status == 200 &&
              duplicate.body.find("\"changed\":false") != std::string::npos,
          "source rebind API was not idempotent");
  require(fixture.api
              .handle("PUT", "/api/mxl/v1/sources/src_a",
                      "{\"flow_id\":\"flow-new\"}")
              .status == 400,
          "source rebind API accepted an unspecified audio binding");
  require(fixture.api
              .handle("PUT", "/api/mxl/v1/sources/src_a",
                      "{\"flow_id\":\"flow-new\",\"audio_flow_id\":null}")
              .status == 200,
          "source rebind API could not clear an audio binding");
}

}  // namespace

int main() {
  try {
    test_reads();
    test_commands_wait_for_frame_boundary();
    test_errors();
    test_events_and_health();
    test_runtime_source_api();
    test_source_rebind_api();
    std::cout << "All control API tests passed.\n";
    return 0;
  } catch (const std::exception& error) {
    std::cerr << "Control API test failed: " << error.what() << '\n';
    return 1;
  }
}
