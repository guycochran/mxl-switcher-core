#include "mxl_core/core.hpp"

#include <algorithm>
#include <stdexcept>

namespace mxl_core {
namespace {

void validate_format(const VideoFormat& format) {
  if (format.width == 0 || format.height == 0 ||
      format.rate_numerator == 0 || format.rate_denominator == 0) {
    throw std::invalid_argument("video dimensions and frame rate must be non-zero");
  }
  if (format.encoding != "video/v210") {
    throw std::invalid_argument("the v1 core operating point is video/v210");
  }
}

}  // namespace

SwitcherCore::SwitcherCore(MediaBackend& backend, std::vector<Source> sources,
                           OutputSpec program_output,
                           OutputSpec preview_output,
                           SourceId initial_program,
                           SourceId initial_preview)
    : backend_(backend),
      sources_(std::move(sources)),
      program_output_(std::move(program_output)),
      preview_output_(std::move(preview_output)) {
  if (sources_.size() < 2) {
    throw std::invalid_argument("at least two sources are required");
  }
  validate_format(program_output_.format);
  validate_format(preview_output_.format);
  if (!(program_output_.format == preview_output_.format)) {
    throw std::invalid_argument("program and preview formats must match");
  }
  if (program_output_.flow_id.empty() || preview_output_.flow_id.empty() ||
      program_output_.flow_id == preview_output_.flow_id) {
    throw std::invalid_argument("program and preview require distinct flow ids");
  }

  for (std::size_t i = 0; i < sources_.size(); ++i) {
    const auto& source = sources_[i];
    validate_format(source.format);
    if (!(source.format == program_output_.format)) {
      throw std::invalid_argument("all sources must match the core operating point");
    }
    if (source.id.empty() || source.flow_id.empty() ||
        !source_index_.emplace(source.id, i).second) {
      throw std::invalid_argument("source ids and flow ids must be non-empty and ids unique");
    }
  }

  source_by_id_unlocked(initial_program);
  source_by_id_unlocked(initial_preview);
  state_.program = std::move(initial_program);
  state_.preview = std::move(initial_preview);
  state_.program_flow = program_output_.flow_id;
  state_.preview_flow = preview_output_.flow_id;

  // Output identity is established once for the life of the process. Cuts only
  // change routing state; they never delete or recreate an MXL flow.
  backend_.create_output_once(program_output_);
  backend_.create_output_once(preview_output_);
}

CommandReceipt SwitcherCore::set_preview(SourceId source) {
  return enqueue(CommandKind::SetPreview, std::move(source));
}

CommandReceipt SwitcherCore::cut(SourceId source) {
  return enqueue(CommandKind::Cut, std::move(source));
}

CommandReceipt SwitcherCore::take() { return enqueue(CommandKind::Take); }

CommandReceipt SwitcherCore::enqueue(CommandKind kind, SourceId source) {
  std::lock_guard lock(mutex_);
  if (kind != CommandKind::Take) {
    source_by_id_unlocked(source);
  }
  const auto id = next_command_id_++;
  pending_.push_back(Command{id, kind, std::move(source)});
  return CommandReceipt{id, state_.revision};
}

void SwitcherCore::process(std::uint64_t output_index) {
  std::optional<Source> program_source;
  std::optional<Source> preview_source;
  bool revision_changed = false;
  {
    std::lock_guard lock(mutex_);
    if (state_.last_output_index && output_index <= *state_.last_output_index) {
      throw std::invalid_argument("output grain indices must increase monotonically");
    }
    const auto prior_revision = state_.revision;
    apply_pending_at_frame_boundary();
    revision_changed = state_.revision != prior_revision;
    state_.last_output_index = output_index;
    program_source = source_by_id_unlocked(state_.program);
    preview_source = source_by_id_unlocked(state_.preview);
  }
  if (revision_changed) {
    state_changed_.notify_all();
  }

  backend_.prepare_frame(
      {program_source->flow_id, preview_source->flow_id}, output_index);
  backend_.route_grain(program_source->flow_id, program_output_.flow_id,
                       output_index);
  backend_.route_grain(preview_source->flow_id, preview_output_.flow_id,
                       output_index);
}

SourceRegistrationReceipt SwitcherCore::register_source(Source source) {
  validate_format(source.format);
  if (!(source.format == program_output_.format)) {
    throw std::invalid_argument("source must match the core operating point");
  }
  if (source.id.empty() || source.flow_id.empty() || source.label.empty()) {
    throw std::invalid_argument("source id, flow id, and label must be non-empty");
  }
  if (source.capabilities.empty()) {
    source.capabilities.push_back("video");
  }

  std::uint64_t revision = 0;
  {
    std::lock_guard lock(mutex_);
    const auto existing = source_index_.find(source.id);
    if (existing != source_index_.end()) {
      if (sources_[existing->second] == source) {
        return {false, state_.revision};
      }
      throw std::invalid_argument("source id is already registered: " + source.id);
    }
    if (std::any_of(sources_.begin(), sources_.end(), [&](const Source& item) {
          return item.flow_id == source.flow_id;
        })) {
      throw std::invalid_argument("source flow is already registered: " +
                                  source.flow_id);
    }
    sources_.push_back(std::move(source));
    source_index_.emplace(sources_.back().id, sources_.size() - 1);
    revision = ++state_.revision;
  }
  state_changed_.notify_all();
  return {true, revision};
}

SourceRegistrationReceipt SwitcherCore::remove_source(
    const SourceId& source) {
  std::uint64_t revision = 0;
  {
    std::lock_guard lock(mutex_);
    const auto existing = source_index_.find(source);
    if (existing == source_index_.end()) {
      return {false, state_.revision};
    }
    if (state_.program == source || state_.preview == source) {
      throw std::invalid_argument("cannot remove a source on program or preview");
    }
    if (std::any_of(pending_.begin(), pending_.end(), [&](const Command& command) {
          return command.kind != CommandKind::Take && command.source == source;
        })) {
      throw std::invalid_argument("cannot remove a source with pending commands");
    }
    sources_.erase(sources_.begin() +
                   static_cast<std::ptrdiff_t>(existing->second));
    rebuild_source_index_unlocked();
    revision = ++state_.revision;
  }
  state_changed_.notify_all();
  return {true, revision};
}

VideoFormat SwitcherCore::operating_format() const {
  return program_output_.format;
}

void SwitcherCore::apply_pending_at_frame_boundary() {
  if (pending_.empty()) {
    return;
  }
  while (!pending_.empty()) {
    auto command = std::move(pending_.front());
    pending_.pop_front();
    switch (command.kind) {
      case CommandKind::SetPreview:
        state_.preview = std::move(command.source);
        break;
      case CommandKind::Cut:
        state_.program = std::move(command.source);
        break;
      case CommandKind::Take:
        std::swap(state_.program, state_.preview);
        break;
    }
    ++state_.revision;
  }
}

const Source& SwitcherCore::source_by_id_unlocked(const SourceId& id) const {
  const auto found = source_index_.find(id);
  if (found == source_index_.end()) {
    throw std::invalid_argument("unknown source: " + id);
  }
  return sources_[found->second];
}

void SwitcherCore::rebuild_source_index_unlocked() {
  source_index_.clear();
  for (std::size_t index = 0; index < sources_.size(); ++index) {
    source_index_.emplace(sources_[index].id, index);
  }
}

State SwitcherCore::state() const {
  std::lock_guard lock(mutex_);
  return state_;
}

std::optional<State> SwitcherCore::wait_for_revision(
    std::uint64_t after_revision,
    std::chrono::milliseconds timeout) const {
  std::unique_lock lock(mutex_);
  if (!state_changed_.wait_for(
          lock, timeout,
          [&] { return state_.revision > after_revision; })) {
    return std::nullopt;
  }
  return state_;
}

std::vector<Source> SwitcherCore::sources() const {
  std::lock_guard lock(mutex_);
  return sources_;
}

void MemoryBackend::add_source(FlowId flow_id, VideoFormat format,
                               std::uint8_t marker,
                               std::uint64_t source_index_base) {
  validate_format(format);
  if (!synthetic_sources_
           .emplace(std::move(flow_id),
                    SyntheticSource{std::move(format), marker,
                                    source_index_base})
           .second) {
    throw std::invalid_argument("source flow already exists");
  }
}

void MemoryBackend::create_output_once(const OutputSpec& output) {
  validate_format(output.format);
  ++create_counts_[output.flow_id];
  if (!outputs_.emplace(output.flow_id, output).second) {
    throw std::logic_error("output flow must only be created once");
  }
}

void MemoryBackend::prepare_frame(const std::vector<FlowId>& source_flows,
                                  std::uint64_t output_index) {
  (void)source_flows;
  (void)output_index;
}

void MemoryBackend::route_grain(const FlowId& source_flow,
                                const FlowId& output_flow,
                                std::uint64_t output_index) {
  write_grain(output_flow, output_index,
              read_grain(source_flow, output_index));
}

Grain MemoryBackend::read_grain(const FlowId& source_flow,
                                std::uint64_t output_index) {
  const auto found = synthetic_sources_.find(source_flow);
  if (found == synthetic_sources_.end()) {
    throw std::invalid_argument("unknown source flow: " + source_flow);
  }
  auto bytes = std::make_shared<ByteBuffer>(64, found->second.marker);
  // A tiny deterministic signature makes accidental transforms/cross-routing
  // visible without allocating full 1080p v210 frames in unit tests.
  (*bytes)[1] = static_cast<std::uint8_t>(output_index & 0xffU);
  (*bytes)[2] = static_cast<std::uint8_t>((output_index >> 8U) & 0xffU);
  const auto source_index = found->second.source_index_base + output_index;
  return Grain{source_index,
               static_cast<std::int64_t>(source_index) * 33'366'667,
               true, std::move(bytes)};
}

void MemoryBackend::write_grain(const FlowId& output_flow,
                                std::uint64_t output_index,
                                const Grain& grain) {
  if (!outputs_.contains(output_flow)) {
    throw std::invalid_argument("unknown output flow: " + output_flow);
  }
  auto& history = writes_[output_flow];
  history.push_back(Write{output_index, grain});
  ++write_counts_[output_flow];
  if (history.size() > kHistoryLimit) {
    history.pop_front();
  }
}

const std::deque<MemoryBackend::Write>& MemoryBackend::writes(
    const FlowId& output_flow) const {
  const auto found = writes_.find(output_flow);
  if (found == writes_.end()) {
    static const std::deque<Write> empty;
    return empty;
  }
  return found->second;
}

std::uint64_t MemoryBackend::write_count(const FlowId& output_flow) const {
  const auto found = write_counts_.find(output_flow);
  return found == write_counts_.end() ? 0 : found->second;
}

std::size_t MemoryBackend::output_create_count(const FlowId& output_flow) const {
  const auto found = create_counts_.find(output_flow);
  return found == create_counts_.end() ? 0 : found->second;
}

}  // namespace mxl_core
