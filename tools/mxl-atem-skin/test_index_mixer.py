import ctypes
import json
import pathlib
import os
import sys
import tempfile
import threading
import unittest
from array import array

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import mxl_direct as mxl  # noqa: E402
import mxl_index_mixer as mixer  # noqa: E402

BLOCK = mixer.BLOCK
START = 480 * 100_000  # an arbitrary block-aligned absolute sample index


def f32(value):
    return array("f", [value])[0]


class Clock:
    """The MXL sample-index clock the mixer runs on."""

    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class PlanarRing:
    """Planar stereo Float32 ring indexed like the SDK: ``count`` samples up to head index ``end``."""

    def __init__(self, length):
        self.length = length
        self.channels = [(ctypes.c_float * length)(), (ctypes.c_float * length)()]
        self.head = 0

    def slices(self, end, count, slices):
        start = end - count
        first = start % self.length
        first_count = min(count, self.length - first)
        base = ctypes.addressof(self.channels[0])
        slices._obj.fragments[0].pointer = base + 4 * first
        slices._obj.fragments[0].size = 4 * first_count
        slices._obj.fragments[1].pointer = base
        slices._obj.fragments[1].size = 4 * (count - first_count)
        slices._obj.stride = ctypes.addressof(self.channels[1]) - base
        slices._obj.count = 2

    def value(self, channel, index):
        return self.channels[channel][index % self.length]

    def mxlFlowReaderGetRuntimeInfo(self, handle, info):
        info._obj.headIndex = self.head
        return mxl.MXL_STATUS_OK


class FakeInput(PlanarRing):
    """An input ring whose content at sample ``s`` is ``left(s)``, ``right(s)``.

    ``on_wait(ring, end, timeout_ns)`` runs when the mixer blocks for samples
    that have not arrived, standing in for the SDK's bounded wait.
    """

    def __init__(self, left, right, length=48_000, on_wait=None):
        super().__init__(length)
        self.left, self.right = left, right
        self.on_wait = on_wait
        self.lap_during_copy = 0
        self.waits = 0
        self.invalid = False  # the writer recreated the flow
        self.released = 0

    def write_through(self, end):
        for index in range(max(self.head, end - self.length), end):
            self.channels[0][index % self.length] = self.left(index)
            self.channels[1][index % self.length] = self.right(index)
        self.head = max(self.head, end)

    def mxlCreateFlowReader(self, instance, flow, options, handle):
        handle._obj.value = 1
        return mxl.MXL_STATUS_OK

    def mxlReleaseFlowReader(self, instance, handle):
        self.released += 1
        return mxl.MXL_STATUS_OK

    def mxlFlowReaderGetSamples(self, handle, end, count, timeout, slices):
        if self.invalid:
            return mxl.MXL_ERR_FLOW_INVALID
        if end > self.head and timeout and self.on_wait:
            self.waits += 1
            self.on_wait(self, end, timeout)
        if end > self.head:
            return mxl.MXL_ERR_OUT_OF_RANGE_TOO_EARLY
        if self.head - (end - count) > self.length:
            return mxl.MXL_ERR_OUT_OF_RANGE_TOO_LATE
        self.slices(end, count, slices)
        if self.lap_during_copy:
            # The writer advances after the slice is handed out; the copy may be torn.
            self.write_through(self.head + self.lap_during_copy)
            self.lap_during_copy = 0
        return mxl.MXL_STATUS_OK


class FakeOutput(PlanarRing):
    def __init__(self, length=48_000):
        super().__init__(length)
        self.written = []
        self.open = None

    def mxlCreateFlowWriter(self, instance, definition, options, handle, info, created):
        self.definition = json.loads(definition)
        handle._obj.value = 1
        info._obj.common.grainRate.numerator = 48_000
        info._obj.common.grainRate.denominator = 1
        info._obj.continuous.channelCount = 2
        info._obj.continuous.bufferLength = self.length
        created._obj.value = True
        return mxl.MXL_STATUS_OK

    def mxlReleaseFlowWriter(self, instance, handle):
        return mxl.MXL_STATUS_OK

    def mxlFlowWriterOpenSamples(self, handle, end, count, slices):
        self.slices(end, count, slices)
        self.open = end
        return mxl.MXL_STATUS_OK

    def mxlFlowWriterCancelSamples(self, handle):
        self.open = None
        return mxl.MXL_STATUS_OK

    def mxlFlowWriterCommitSamples(self, handle):
        self.written.append(self.open)
        self.head = max(self.head, self.open)
        self.open = None
        return mxl.MXL_STATUS_OK


def make(inputs, clock, max_wait_ms=500.0, output_length=48_000):
    readers = {str(n): mxl.SampleReader(ring, None, f"in{n}", ring.length) for n, ring in enumerate(inputs, 1)}
    out = FakeOutput(output_length)
    writer = mxl.SampleWriter(out, None, mixer.flow_definition("out", "Group", "Mix", "test"))
    return mixer.IndexMixer(readers, writer, "out", clock=clock, max_wait_ms=max_wait_ms), out


class FakeDomain:
    """``DomainInputs`` stand-in: ``flows`` maps flow ID to a ring, or to an error message."""

    def __init__(self, **flows):
        self.flows = flows
        self.opened = []

    def exists(self, flow_id):
        return flow_id in self.flows

    def open(self, flow_id):
        if flow_id not in self.flows:
            raise mixer.InputUnavailable(f"flow {flow_id} is not in the domain")
        ring = self.flows[flow_id]
        if isinstance(ring, str):
            raise mixer.InputUnavailable(ring)
        self.opened.append(flow_id)
        return mxl.SampleReader(ring, None, flow_id, ring.length)


def make_live(domain, flows, clock, max_wait_ms=500.0, reopen_interval_ms=1000.0):
    """A mixer that opens its inputs through ``domain``, as ``main`` does."""

    readers = {}
    for number, flow in flows.items():
        try:
            readers[number] = domain.open(flow)
        except mixer.InputUnavailable:
            readers[number] = None
    out = FakeOutput()
    writer = mxl.SampleWriter(out, None, mixer.flow_definition("out", "Group", "Mix", "test"))
    mix = mixer.IndexMixer(readers, writer, "out", clock=clock, max_wait_ms=max_wait_ms, inputs=domain,
                           flows=flows, reopen_interval_ms=reopen_interval_ms)
    return mix, out


def counter(index):
    return float(index % 1_000_000)


def negative(index):
    return -float(index % 1_000_000)


class PlacementTest(unittest.TestCase):
    def test_single_unity_input_is_bit_exact_at_the_same_index(self):
        clock = Clock(START + 10 * BLOCK)
        source = FakeInput(counter, negative)
        source.write_through(clock.now)
        mix, out = make([source], clock)
        mix.control({"mode": "manual", "channel": 1, "on": True})
        mix.applied["1"] = 1.0  # already faded up
        for end in range(START + BLOCK, START + 6 * BLOCK, BLOCK):
            self.assertEqual(mix.mix_block(end), mxl.MXL_STATUS_OK)
        self.assertEqual(out.written, list(range(START + BLOCK, START + 6 * BLOCK, BLOCK)))
        for index in range(START, START + 5 * BLOCK):
            self.assertEqual(out.value(0, index), source.value(0, index))
            self.assertEqual(out.value(1, index), source.value(1, index))

    def test_sum_with_gain_and_master(self):
        clock = Clock(START + 10 * BLOCK)
        a = FakeInput(lambda s: float(s % 1000), lambda s: 1.0)
        b = FakeInput(lambda s: -float(s % 7), lambda s: 2.0)
        for ring in (a, b):
            ring.write_through(clock.now)
        mix, out = make([a, b], clock)
        for command in ({"mode": "manual"}, {"master_gain": 0.5},
                        {"channel": 1, "on": True, "gain": 1.5}, {"channel": 2, "on": True, "gain": 0.5}):
            mix.control(command)
        mix.mix_block(START + BLOCK)  # ramps up from silence
        mix.mix_block(START + 2 * BLOCK)
        for index in range(START + BLOCK, START + 2 * BLOCK):
            left = 0.75 * (index % 1000) + 0.25 * -(index % 7)
            self.assertEqual(out.value(0, index), f32(left))
            self.assertEqual(out.value(1, index), f32(0.75 + 0.5))

    def test_gain_change_ramps_across_one_block(self):
        clock = Clock(START + 10 * BLOCK)
        source = FakeInput(lambda s: 1.0, lambda s: 1.0)
        source.write_through(clock.now)
        mix, out = make([source], clock)
        mix.set_program("zoomiso-1")  # AFV takes input 1 to air
        mix.mix_block(START + BLOCK)
        ramp = [out.value(0, START + i) for i in range(BLOCK)]
        self.assertEqual(ramp[-1], 1.0)
        self.assertAlmostEqual(ramp[0], 1 / BLOCK, places=6)
        self.assertEqual(ramp, sorted(ramp))
        mix.set_program("zoomiso-2")  # cut away: ramps out, then input 1 is no longer read
        mix.mix_block(START + 2 * BLOCK)
        self.assertEqual(out.value(0, START + 2 * BLOCK - 1), 0.0)
        before = mix.stats["1"].on_time
        mix.mix_block(START + 3 * BLOCK)
        self.assertEqual(mix.stats["1"].on_time, before)


class GapAccountingTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock(START + BLOCK)

    def test_late_input_is_waited_for_and_placed_exactly(self):
        def arrive(ring, end, timeout):
            self.assertLessEqual(timeout, 500_000_000)
            self.clock.now += 4_800  # 100 ms later the input catches up
            ring.write_through(end + BLOCK)

        source = FakeInput(counter, negative, on_wait=arrive)
        source.write_through(START)  # one block behind the due block
        mix, out = make([source], self.clock)
        mix.control({"mode": "manual", "channel": 1, "on": True})
        mix.applied["1"] = 1.0
        mix.mix_block(START + BLOCK)
        stats = mix.stats["1"]
        self.assertEqual((stats.late, stats.on_time, stats.skipped), (1, 0, 0))
        self.assertEqual(stats.held_ms, 100.0)
        self.assertEqual(mix.blocks_degraded, 0)
        for index in range(START, START + BLOCK):
            self.assertEqual(out.value(0, index), counter(index))

    def test_missing_input_is_skipped_marked_stalled_then_recovers(self):
        def never(ring, end, timeout):
            self.clock.now += timeout * 48_000 // 1_000_000_000

        dead = FakeInput(counter, negative, on_wait=never)
        live = FakeInput(lambda s: 0.25, lambda s: 0.25)
        dead.write_through(START)
        live.write_through(START + 10 * BLOCK)
        mix, out = make([dead, live], self.clock, max_wait_ms=200.0)
        mix.control({"mode": "manual"})
        mix.control({"channel": 1, "on": True})
        mix.control({"channel": 2, "on": True})
        mix.applied.update({"1": 1.0, "2": 1.0})
        mix.mix_block(START + BLOCK)
        stats = mix.stats["1"]
        self.assertEqual((stats.skipped, stats.skipped_reasons["missing"], stats.stalled), (1, 1, True))
        self.assertEqual(stats.held_ms, 200.0)
        self.assertEqual(mix.blocks_degraded, 1)
        self.assertEqual(out.value(0, START + 7), 0.25)  # the live input still airs
        mix.mix_block(START + 2 * BLOCK)  # stalled: polled without waiting
        self.assertEqual(dead.waits, 1)
        self.assertEqual(stats.skipped, 2)
        dead.write_through(START + 4 * BLOCK)
        mix.mix_block(START + 3 * BLOCK)
        self.assertEqual((stats.stalled, stats.recoveries, stats.on_time), (False, 1, 1))
        self.assertEqual(out.value(0, START + 2 * BLOCK + 5), f32(counter(START + 2 * BLOCK + 5) + 0.25))

    def test_overrun_is_counted_when_the_ring_has_moved_on(self):
        source = FakeInput(counter, negative, length=4_800)
        source.write_through(START + 20 * BLOCK)
        mix, _ = make([source], self.clock)
        mix.set_program("zoomiso-1")
        mix.mix_block(START + BLOCK)
        self.assertEqual(mix.stats["1"].skipped_reasons["overrun"], 1)
        self.assertFalse(mix.stats["1"].stalled)

    def test_torn_copy_is_never_mixed(self):
        source = FakeInput(counter, negative, length=4_800)
        source.write_through(START + 2 * BLOCK)
        mix, out = make([source], self.clock)
        mix.set_program("zoomiso-1")
        mix.applied["1"] = 1.0
        source.lap_during_copy = 4_800  # the writer laps the ring mid-copy
        mix.mix_block(START + BLOCK)
        stats = mix.stats["1"]
        self.assertEqual((stats.skipped_reasons["torn"], stats.on_time), (1, 0))
        self.assertEqual(out.value(0, START + 3), 0.0)
        self.assertEqual(mix.blocks_degraded, 1)

    def test_wait_budget_is_capped_by_the_shortest_input_ring(self):
        mix, _ = make([FakeInput(counter, negative, length=9_600)], self.clock, max_wait_ms=1000.0)
        self.assertEqual(mix.max_wait_samples, 4_800)
        self.assertEqual(mix.state()["max_wait_ms"], 100.0)


class InputLifecycleTest(unittest.TestCase):
    """Inputs that are absent at startup, recreated, or removed never crash or wedge the mixer."""

    def setUp(self):
        self.clock = Clock(START + BLOCK)
        self.live = FakeInput(lambda s: 0.25, lambda s: 0.25)
        self.live.write_through(START + 10 * BLOCK)

    def on_air_both(self, mix):
        mix.control({"mode": "manual"})
        mix.control({"channel": 1, "on": True})
        mix.control({"channel": 2, "on": True})
        mix.applied.update({"1": 1.0, "2": 1.0})

    def test_absent_input_at_startup_is_offline_then_joins_after_one_interval(self):
        domain = FakeDomain(live=self.live)
        mix, out = make_live(domain, {"1": "late-flow", "2": "live"}, self.clock)
        self.on_air_both(mix)
        mix.mix_block(START + BLOCK)
        stats = mix.stats["1"]
        self.assertEqual((stats.online, stats.skipped_reasons["offline"], stats.opens), (False, 1, 0))
        self.assertIn("late-flow", stats.last_open_error)
        self.assertEqual(mix.blocks_degraded, 1)
        self.assertEqual(out.value(0, START + 3), 0.25)  # the live input still airs
        self.assertEqual(mix.state()["channels"]["1"]["flow_id"], "late-flow")

        arrived = FakeInput(counter, negative)
        arrived.write_through(START + 10 * BLOCK)
        domain.flows["late-flow"] = arrived
        self.clock.now += 48_000 - BLOCK  # just short of the one-second interval
        mix.mix_block(START + 2 * BLOCK)
        self.assertEqual((stats.online, stats.skipped_reasons["offline"]), (False, 2))
        self.clock.now += BLOCK
        mix.mix_block(START + 3 * BLOCK)
        self.assertEqual((stats.online, stats.opens, stats.on_time), (True, 1, 1))
        index = START + 2 * BLOCK + 9
        self.assertEqual(out.value(0, index), f32(counter(index) + 0.25))

    def test_wrong_format_input_is_offline_not_fatal(self):
        domain = FakeDomain(live=self.live, video="input video is not a 48 kHz audio flow")
        mix, _ = make_live(domain, {"1": "video", "2": "live"}, self.clock)
        self.on_air_both(mix)
        self.assertEqual(mix.mix_block(START + BLOCK), mxl.MXL_STATUS_OK)
        self.assertEqual(mix.stats["1"].last_open_error, "input video is not a 48 kHz audio flow")
        self.assertEqual(mix.stats["2"].on_time, 1)

    def test_offline_inputs_are_probed_even_when_not_on_air(self):
        domain = FakeDomain(live=self.live)
        mix, _ = make_live(domain, {"1": "late-flow", "2": "live"}, self.clock)
        mix.set_program("zoomiso-2")
        domain.flows["late-flow"] = FakeInput(counter, negative)
        mix.mix_block(START + BLOCK)
        self.assertTrue(mix.stats["1"].online)  # ready before it is cut to
        self.assertEqual(mix.stats["1"].skipped, 0)

    def test_recreated_flow_is_reopened_immediately(self):
        old = FakeInput(counter, negative)
        old.write_through(START + 10 * BLOCK)
        domain = FakeDomain(zoom=old, live=self.live)
        mix, out = make_live(domain, {"1": "zoom", "2": "live"}, self.clock)
        self.on_air_both(mix)
        mix.mix_block(START + BLOCK)
        old.invalid = True
        new = FakeInput(lambda s: 0.5, lambda s: 0.5)
        new.write_through(START + 10 * BLOCK)
        domain.flows["zoom"] = new
        mix.mix_block(START + 2 * BLOCK)
        self.assertTrue(mix.wait_for_retired(1.0))
        stats = mix.stats["1"]
        self.assertEqual((stats.skipped_reasons["invalid"], stats.closes, stats.opens, old.released),
                         (1, 1, 1, 1))
        self.assertTrue(stats.online)
        mix.mix_block(START + 3 * BLOCK)
        self.assertEqual(out.value(0, START + 2 * BLOCK + 1), 0.75)

    def test_invalid_flow_that_is_gone_goes_offline_and_returns_later(self):
        old = FakeInput(counter, negative)
        old.write_through(START + 10 * BLOCK)
        domain = FakeDomain(zoom=old, live=self.live)
        mix, _ = make_live(domain, {"1": "zoom", "2": "live"}, self.clock)
        self.on_air_both(mix)
        old.invalid = True
        del domain.flows["zoom"]
        mix.mix_block(START + BLOCK)
        stats = mix.stats["1"]
        self.assertEqual((stats.online, stats.skipped_reasons["invalid"]), (False, 1))
        mix.mix_block(START + 2 * BLOCK)
        self.assertEqual(stats.skipped_reasons["offline"], 1)
        back = FakeInput(counter, negative)
        back.write_through(START + 10 * BLOCK)
        domain.flows["zoom"] = back
        self.clock.now += 48_000
        mix.mix_block(START + 3 * BLOCK)
        self.assertEqual((stats.online, stats.on_time), (True, 1))

    def test_stalled_input_is_reopened_without_holding_output_again(self):
        def never(ring, end, timeout):
            self.clock.now += timeout * 48_000 // 1_000_000_000

        stuck = FakeInput(counter, negative, on_wait=never)
        stuck.write_through(START)
        domain = FakeDomain(zoom=stuck, live=self.live)
        mix, _ = make_live(domain, {"1": "zoom", "2": "live"}, self.clock, max_wait_ms=200.0)
        self.on_air_both(mix)
        mix.mix_block(START + BLOCK)  # waits once, then stalls; the stall triggers a reopen next block
        stats = mix.stats["1"]
        self.assertTrue(stats.stalled)
        replacement = FakeInput(counter, negative, on_wait=never)  # recreated under the same ID, still silent
        domain.flows["zoom"] = replacement
        mix.mix_block(START + 2 * BLOCK)
        self.assertTrue(mix.wait_for_retired(1.0))
        self.assertEqual((stats.opens, stats.closes, stats.stalled, stuck.released), (1, 1, True, 1))
        self.assertEqual(replacement.waits, 0)  # the stall survives the reopen: no second hold
        mix.mix_block(START + 3 * BLOCK)
        self.assertEqual(stats.opens, 1)  # not again until the interval passes
        replacement.write_through(START + 10 * BLOCK)
        mix.mix_block(START + 4 * BLOCK)
        self.assertEqual((stats.stalled, stats.recoveries, stats.on_time), (False, 1, 1))

    def test_stalled_input_whose_flow_left_the_domain_goes_offline(self):
        stuck = FakeInput(counter, negative)
        stuck.write_through(START)
        domain = FakeDomain(zoom=stuck, live=self.live)
        mix, _ = make_live(domain, {"1": "zoom", "2": "live"}, self.clock, max_wait_ms=0.0)
        self.on_air_both(mix)
        mix.mix_block(START + BLOCK)
        del domain.flows["zoom"]
        mix.mix_block(START + 2 * BLOCK)
        self.assertTrue(mix.wait_for_retired(1.0))
        stats = mix.stats["1"]
        self.assertEqual((stats.online, stuck.released, stats.skipped_reasons["offline"]), (False, 1, 1))

    def test_slow_reader_release_does_not_block_the_surviving_input(self):
        release_started = threading.Event()
        release_allowed = threading.Event()

        class SlowRelease(FakeInput):
            def mxlReleaseFlowReader(self, instance, handle):
                release_started.set()
                release_allowed.wait(2.0)
                return super().mxlReleaseFlowReader(instance, handle)

        old = SlowRelease(counter, negative)
        old.write_through(START + 10 * BLOCK)
        domain = FakeDomain(zoom=old, live=self.live)
        mix, out = make_live(domain, {"1": "zoom", "2": "live"}, self.clock)
        self.on_air_both(mix)
        old.invalid = True
        replacement = FakeInput(lambda s: 0.5, lambda s: 0.5)
        replacement.write_through(START + 10 * BLOCK)
        domain.flows["zoom"] = replacement

        mix.mix_block(START + BLOCK)
        self.assertFalse(release_started.is_set())
        self.assertEqual(mix.stats["2"].on_time, 1)
        self.assertEqual(out.written, [START + BLOCK])
        self.assertEqual(mix.state()["reader_releases_pending"], 1)

        release_allowed.set()
        self.assertTrue(mix.wait_for_retired(1.0))
        self.assertTrue(release_started.is_set())
        self.assertEqual(old.released, 1)
        self.assertEqual(mix.state()["reader_release_errors"], 0)

    def test_wait_cap_follows_the_open_rings(self):
        domain = FakeDomain(long=FakeInput(counter, negative, length=48_000))
        mix, _ = make_live(domain, {"1": "long", "2": "short"}, self.clock, max_wait_ms=1000.0)
        self.assertEqual(mix.max_wait_samples, 24_000)
        domain.flows["short"] = FakeInput(counter, negative, length=9_600)
        mix.maintain_inputs()
        self.assertEqual(mix.max_wait_samples, 4_800)

    def test_without_an_input_source_readers_are_used_as_given(self):
        source = FakeInput(counter, negative)
        source.write_through(START + 10 * BLOCK)
        mix, _ = make([source], self.clock)
        mix.set_program("zoomiso-1")
        source.invalid = True
        mix.mix_block(START + BLOCK)
        self.assertEqual((mix.stats["1"].skipped_reasons["invalid"], source.released), (1, 0))


class DomainInputsTest(unittest.TestCase):
    """The SDK must never be asked to open a flow that is not in the domain."""

    class Api:
        def __init__(self, rate=48_000):
            self.rate = rate
            self.created = []
            self.instances = []
            self.destroyed = []

        def mxlCreateInstance(self, domain, options):
            instance = len(self.instances) + 10
            self.instances.append((instance, domain, options))
            return instance

        def mxlDestroyInstance(self, instance):
            self.destroyed.append(instance)
            return mxl.MXL_STATUS_OK

        def mxlCreateFlowReader(self, instance, flow, options, handle):
            self.created.append(flow.decode())
            handle._obj.value = 1
            return mxl.MXL_STATUS_OK

        def mxlFlowReaderGetConfigInfo(self, handle, info):
            info._obj.common.grainRate.numerator = self.rate
            info._obj.common.grainRate.denominator = 1
            info._obj.continuous.channelCount = 2
            info._obj.continuous.bufferLength = 19_456
            return mxl.MXL_STATUS_OK

        def mxlReleaseFlowReader(self, instance, handle):
            return mxl.MXL_STATUS_OK

    def setUp(self):
        self.domain = tempfile.TemporaryDirectory()
        self.addCleanup(self.domain.cleanup)

    def add_flow(self, flow_id):
        os.makedirs(os.path.join(self.domain.name, f"{flow_id}.mxl-flow"))
        open(os.path.join(self.domain.name, f"{flow_id}.mxl-flow", "data"), "wb").close()

    def test_missing_flow_is_refused_before_the_sdk_sees_it(self):
        api = self.Api()
        inputs = mixer.DomainInputs(api, None, self.domain.name)
        with self.assertRaises(mixer.InputUnavailable):
            inputs.open("00000000-0000-4000-8000-000000000001")
        self.assertEqual(api.created, [])

    def test_present_flow_opens_with_its_ring_length(self):
        api = self.Api()
        self.add_flow("00000000-0000-4000-8000-000000000002")
        reader = mixer.DomainInputs(api, None, self.domain.name).open("00000000-0000-4000-8000-000000000002")
        self.assertEqual(reader.ring_length, 19_456)

    def test_isolated_reader_owns_and_releases_its_instance(self):
        api = self.Api()
        flow = "00000000-0000-4000-8000-000000000004"
        self.add_flow(flow)
        reader = mixer.DomainInputs(api, None, self.domain.name, isolated_instances=True).open(flow)
        self.assertEqual(reader.ring_length, 19_456)
        self.assertEqual(len(api.instances), 1)
        reader.close()
        self.assertEqual(api.destroyed, [api.instances[0][0]])

    def test_wrong_rate_is_unavailable_not_a_crash(self):
        self.add_flow("00000000-0000-4000-8000-000000000003")
        with self.assertRaises(mixer.InputUnavailable):
            mixer.DomainInputs(self.Api(rate=44_100), None, self.domain.name).open(
                "00000000-0000-4000-8000-000000000003")


class RunLoopTest(unittest.TestCase):
    def test_output_is_contiguous_and_never_ahead_of_mxl_time(self):
        clock = Clock(START + 123)
        mix, out = make([FakeInput(counter, negative)], clock)
        stop = threading.Event()

        def sleep(seconds):
            clock.now += max(1, round(seconds * 48_000))
            if len(out.written) >= 5:
                stop.set()

        mix.sleep = sleep
        written_at = []
        original = mix.mix_block

        def recording(end):
            written_at.append((end, clock.now))
            return original(end)

        mix.mix_block = recording
        mix.run(stop)
        self.assertEqual(out.written[0], START)
        self.assertEqual(out.written, list(range(START, START + BLOCK * len(out.written), BLOCK)))
        self.assertTrue(all(end <= now for end, now in written_at))
        self.assertEqual(out.value(0, START + 1), 0.0)  # no input on air: true silence, not degraded
        self.assertEqual(mix.blocks_degraded, 0)


class ControlTest(unittest.TestCase):
    def test_state_matches_the_gstreamer_mixer_contract(self):
        mix, _ = make([FakeInput(counter, negative), FakeInput(counter, negative)], Clock(START))
        state = mix.state()
        for key in ("state", "mode", "program", "master_gain", "output_flow", "channels",
                    "output_head_index", "output_progress_age_seconds"):
            self.assertIn(key, state)
        self.assertEqual(state["mode"], "afv")
        channel = state["channels"]["1"]
        for key in ("gain", "on", "mute", "active", "on_time", "late", "skipped", "held_ms", "stalled"):
            self.assertIn(key, channel)
        mix.set_program("zoomiso-2")
        self.assertEqual([c["active"] for c in mix.state()["channels"].values()], [False, True])
        self.assertEqual(mix.control({"channel": 2, "mute": True})["channels"]["2"]["active"], False)
        self.assertEqual(mix.control({"master_gain": 9})["master_gain"], 1.5)
        with self.assertRaises(ValueError):
            mix.control({"mode": "auto"})
        with self.assertRaises(ValueError):
            mix.control({"channel": 3})

    def test_flow_definition_is_stable_float32_stereo_with_group_hint(self):
        out = FakeOutput()
        mxl.SampleWriter(out, None, mixer.flow_definition("flow-a", "ProgramGroup", "Mix", "d"))
        definition = out.definition
        self.assertEqual((definition["media_type"], definition["sample_rate"], definition["channel_count"],
                          definition["bit_depth"]), ("audio/float32", {"numerator": 48_000}, 2, 32))
        self.assertEqual(definition["tags"][mixer.GROUPHINT_TAG], ["ProgramGroup:Audio"])
        self.assertEqual(json.loads(mixer.flow_definition("flow-a", "ProgramGroup", "Mix", "d"))["source_id"],
                         definition["source_id"])


class SampleWriterTest(unittest.TestCase):
    def test_writes_wrapped_planar_channels_at_the_read_index(self):
        out = FakeOutput(length=16)
        writer = mxl.SampleWriter(out, None, "{}")
        left = array("f", [1, 2, 3, 4, 5, 6]).tobytes()
        right = array("f", [-1, -2, -3, -4, -5, -6]).tobytes()
        self.assertEqual(writer.write(19, [left, right]), mxl.MXL_STATUS_OK)  # samples 13..18 wrap
        self.assertEqual([out.value(0, i) for i in range(13, 19)], [1, 2, 3, 4, 5, 6])
        self.assertEqual([out.value(1, i) for i in range(13, 19)], [-1, -2, -3, -4, -5, -6])
        reader = mxl.SampleReader(_Readback(out), None, "out", out.length)
        _, channels, torn = reader.read(19, 6, 0, 2)
        self.assertFalse(torn)
        self.assertEqual((channels[0], channels[1]), (left, right))


class _Readback:
    def __init__(self, ring):
        self.ring = ring

    def mxlCreateFlowReader(self, instance, flow, options, handle):
        handle._obj.value = 1
        return mxl.MXL_STATUS_OK

    def mxlFlowReaderGetRuntimeInfo(self, handle, info):
        return self.ring.mxlFlowReaderGetRuntimeInfo(handle, info)

    def mxlFlowReaderGetSamples(self, handle, end, count, timeout, slices):
        self.ring.slices(end, count, slices)
        return mxl.MXL_STATUS_OK


if __name__ == "__main__":
    unittest.main()
