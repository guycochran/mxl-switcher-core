import importlib.util
import pathlib
import unittest


MODULE_PATH = pathlib.Path(__file__).with_name("mxl_program_reclock.py")
SPEC = importlib.util.spec_from_file_location("mxl_program_reclock", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)
AUDIO_PROBE_PATH = pathlib.Path(__file__).with_name("mxl_audio_timing_probe.py")
AUDIO_PROBE_SPEC = importlib.util.spec_from_file_location(
    "mxl_audio_timing_probe", AUDIO_PROBE_PATH
)
AUDIO_PROBE = importlib.util.module_from_spec(AUDIO_PROBE_SPEC)
AUDIO_PROBE_SPEC.loader.exec_module(AUDIO_PROBE)


class ProgramReclockPipelineTest(unittest.TestCase):
    def test_restart_epoch_advances_by_emitted_audio(self):
        self.assertEqual(
            MODULE.advance_output_epoch(9_000_000_000, 384_000),
            10_000_000_000,
        )

    def test_candidate_pipeline_is_clock_correct_and_isolated(self):
        description = MODULE.pipeline_description(
            "/mxl-domain",
            "00000000-0000-4000-8000-000000000001",
            "00000000-0000-4000-8000-000000000002",
            9_000_000_000,
        )

        self.assertIn("pitch name=pll", description)
        self.assertIn("audioresample quality=10", description)
        self.assertIn("queue name=output_queue", description)
        self.assertIn("datarate=384000", description)
        self.assertIn("single-segment=true", description)
        self.assertIn("ts-offset=9000000000", description)
        self.assertIn("sync=true", description)
        self.assertIn('group-hint="MXLProgramMasterCandidate:Audio"', description)
        self.assertNotIn("use-wallclock", description)
        self.assertNotIn("sync=false", description)

    def test_pipeline_accepts_public_program_group(self):
        description = MODULE.pipeline_description(
            "/mxl-domain",
            "00000000-0000-4000-8000-000000000001",
            "00000000-0000-4000-8000-000000000002",
            9_000_000_000,
            "ZoomISOMXLProgram",
        )

        self.assertIn('group-hint="ZoomISOMXLProgram:Audio"', description)
        self.assertNotIn("MXLProgramMasterCandidate:Audio", description)

    def test_read_only_probe_never_publishes_a_flow(self):
        description = AUDIO_PROBE.pipeline_description(
            "/mxl-domain", "00000000-0000-4000-8000-000000000001"
        )

        self.assertIn("mxlsrc", description)
        self.assertIn("appsink", description)
        self.assertIn("drop=true", description)
        self.assertNotIn("mxlsink", description)

    def test_probe_regression_rejects_burst_phase_at_endpoints(self):
        buffers = []
        for index in range(100):
            pts = index * 1_000_000
            burst_phase = (0, 200_000, -150_000, 100_000, -100_000)[index % 5]
            wall = pts + burst_phase
            buffers.append((pts, 1_000_000, 384, wall))

        self.assertAlmostEqual(AUDIO_PROBE.regression_rate(buffers), 1.0, delta=0.0002)


if __name__ == "__main__":
    unittest.main()
