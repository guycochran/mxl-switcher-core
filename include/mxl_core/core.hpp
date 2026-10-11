#pragma once

#include <cstdint>
#include <deque>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <unordered_map>
#include <utility>
#include <vector>

namespace mxl_core {

using FlowId = std::string;
using SourceId = std::string;
using ByteBuffer = std::vector<std::uint8_t>;

struct VideoFormat {
  std::uint32_t width;
  std::uint32_t height;
  std::uint32_t rate_numerator;
  std::uint32_t rate_denominator;
  std::string encoding;

  bool operator==(const VideoFormat&) const = default;
};

// The core does not parse or transform the media. A production MXL adapter can
// expose an SDK-owned grain here and copy it once into the stable output flow.
struct Grain {
  std::uint64_t source_index{0};
  std::int64_t capture_timestamp_ns{0};
  bool valid{false};
  std::shared_ptr<const ByteBuffer> payload;
};

struct Source {
  SourceId id;
  FlowId flow_id;
  std::string label;
  VideoFormat format;
};

struct OutputSpec {
  FlowId flow_id;
  std::string label;
  VideoFormat format;
};

struct State {
  std::uint64_t revision{0};
  SourceId program;
  SourceId preview;
  FlowId program_flow;
  FlowId preview_flow;
  std::optional<std::uint64_t> last_output_index;
};

enum class CommandKind { SetPreview, Cut, Take };

struct CommandReceipt {
  std::uint64_t command_id;
  std::uint64_t accepted_revision;
};

class MediaBackend {
 public:
  virtual ~MediaBackend() = default;
  virtual void create_output_once(const OutputSpec& output) = 0;
  // Called once per frame after control commands have been applied and before
  // either bus is routed. A live backend uses this hook to synchronize all
  // selected inputs at the output timestamp.
  virtual void prepare_frame(const std::vector<FlowId>& source_flows,
                             std::uint64_t output_index) = 0;
  // Route is one operation so a live backend can copy directly from an
  // SDK-owned input grain into an SDK-owned output grain. Splitting read and
  // write here would force an extra allocation and media copy in the core.
  virtual void route_grain(const FlowId& source_flow,
                           const FlowId& output_flow,
                           std::uint64_t output_index) = 0;
};

class SwitcherCore {
 public:
  SwitcherCore(MediaBackend& backend, std::vector<Source> sources,
               OutputSpec program_output, OutputSpec preview_output,
               SourceId initial_program, SourceId initial_preview);

  CommandReceipt set_preview(SourceId source);
  CommandReceipt cut(SourceId source);
  CommandReceipt take();

  // Called once for each domain-aligned MXL grain index. All accepted control
  // commands are applied atomically before this index is rendered.
  void process(std::uint64_t output_index);

  State state() const;
  std::vector<Source> sources() const;

 private:
  struct Command {
    std::uint64_t id;
    CommandKind kind;
    SourceId source;
  };

  CommandReceipt enqueue(CommandKind kind, SourceId source = {});
  const Source& source_by_id(const SourceId& id) const;
  void apply_pending_at_frame_boundary();
  void route_one(const SourceId& source, const FlowId& output,
                 std::uint64_t output_index);

  MediaBackend& backend_;
  std::vector<Source> sources_;
  std::unordered_map<SourceId, std::size_t> source_index_;
  OutputSpec program_output_;
  OutputSpec preview_output_;

  mutable std::mutex mutex_;
  State state_;
  std::deque<Command> pending_;
  std::uint64_t next_command_id_{1};
};

// A deterministic backend for unit tests, development, and the local demo. It
// models stable MXL flows without requiring the SDK or /dev/shm.
class MemoryBackend final : public MediaBackend {
 public:
  struct Write {
    std::uint64_t output_index;
    Grain grain;
  };

  void add_source(FlowId flow_id, VideoFormat format, std::uint8_t marker);
  void create_output_once(const OutputSpec& output) override;
  void prepare_frame(const std::vector<FlowId>& source_flows,
                     std::uint64_t output_index) override;
  void route_grain(const FlowId& source_flow, const FlowId& output_flow,
                   std::uint64_t output_index) override;

  const std::deque<Write>& writes(const FlowId& output_flow) const;
  std::uint64_t write_count(const FlowId& output_flow) const;
  std::size_t output_create_count(const FlowId& output_flow) const;

 private:
  struct SyntheticSource {
    VideoFormat format;
    std::uint8_t marker;
  };

  std::unordered_map<FlowId, SyntheticSource> synthetic_sources_;
  std::unordered_map<FlowId, OutputSpec> outputs_;
  std::unordered_map<FlowId, std::size_t> create_counts_;
  static constexpr std::size_t kHistoryLimit = 256;
  std::unordered_map<FlowId, std::deque<Write>> writes_;
  std::unordered_map<FlowId, std::uint64_t> write_counts_;

  Grain read_grain(const FlowId& source_flow, std::uint64_t output_index);
  void write_grain(const FlowId& output_flow, std::uint64_t output_index,
                   const Grain& grain);
};

}  // namespace mxl_core
