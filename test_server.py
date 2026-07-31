"""
Unit tests for ClipTrimmer's pure helpers.

ClipTrimmer.pyw is not importable by name (".pyw" is not on the import path),
so it is loaded directly from disk. Importing it only defines module-level
constants and functions - main() is guarded behind __main__ - so nothing
starts a server or opens a window.

Run:  python test_server.py
"""

import importlib.util
import os
import sys
import unittest
from pathlib import Path

_APP = Path(__file__).resolve().parent / "ClipTrimmer.pyw"
_spec = importlib.util.spec_from_loader(
    "cliptrimmer_app",
    importlib.machinery.SourceFileLoader("cliptrimmer_app", str(_APP)),
)
ct = importlib.util.module_from_spec(_spec)
sys.modules["cliptrimmer_app"] = ct
_spec.loader.exec_module(ct)


# ---------------------------------------------------------------- Range parsing
class TestParseRangeHeader(unittest.TestCase):
    SIZE = 1000

    def test_absent_header_serves_whole_file(self):
        self.assertIsNone(ct.parse_range_header(None, self.SIZE))
        self.assertIsNone(ct.parse_range_header("", self.SIZE))

    def test_simple_closed_range(self):
        self.assertEqual(ct.parse_range_header("bytes=0-99", self.SIZE), (0, 99))
        self.assertEqual(ct.parse_range_header("bytes=100-199", self.SIZE),
                         (100, 199))

    def test_open_ended_range(self):
        self.assertEqual(ct.parse_range_header("bytes=500-", self.SIZE),
                         (500, 999))

    def test_end_is_clamped_to_last_byte(self):
        self.assertEqual(ct.parse_range_header("bytes=900-99999", self.SIZE),
                         (900, 999))

    def test_suffix_range(self):
        self.assertEqual(ct.parse_range_header("bytes=-100", self.SIZE),
                         (900, 999))

    def test_suffix_larger_than_file_clamps_to_zero(self):
        self.assertEqual(ct.parse_range_header("bytes=-99999", self.SIZE),
                         (0, 999))

    def test_multi_range_uses_first_spec(self):
        self.assertEqual(ct.parse_range_header("bytes=0-9,20-29", self.SIZE),
                         (0, 9))

    def test_whitespace_tolerated(self):
        self.assertEqual(ct.parse_range_header("bytes= 10-19 ", self.SIZE),
                         (10, 19))

    # -- malformed input must degrade to "serve whole file", never raise -------
    def test_malformed_headers_return_none(self):
        for bad in ("bytes=abc-def", "bytes=", "bytes=-", "bytes=1-2-3",
                    "items=0-10", "0-10", "bytes=xyz", "bytes=10-5"):
            with self.subTest(bad=bad):
                self.assertIsNone(ct.parse_range_header(bad, self.SIZE),
                                  f"{bad!r} should be ignored, not fatal")

    # -- valid but unsatisfiable must raise so the handler can send 416 -------
    def test_start_past_eof_is_unsatisfiable(self):
        with self.assertRaises(ValueError):
            ct.parse_range_header("bytes=1000-1100", self.SIZE)
        with self.assertRaises(ValueError):
            ct.parse_range_header("bytes=5000-", self.SIZE)

    def test_zero_length_suffix_is_unsatisfiable(self):
        with self.assertRaises(ValueError):
            ct.parse_range_header("bytes=-0", self.SIZE)


# ------------------------------------------------------- Export JSON validation
class TestValidateExportRequest(unittest.TestCase):
    BASE = {"start": 1.0, "end": 5.0, "target_mb": 10, "resolution": 1080,
            "keep_audio": True}

    def test_valid_payload(self):
        params, err = ct.validate_export_request(dict(self.BASE))
        self.assertIsNone(err)
        self.assertEqual(params["start"], 1.0)
        self.assertEqual(params["end"], 5.0)
        self.assertEqual(params["target_mb"], 10.0)
        self.assertEqual(params["resolution"], 1080)
        self.assertIs(params["keep_audio"], True)

    def test_resolution_and_audio_default(self):
        params, err = ct.validate_export_request(
            {"start": 0, "end": 2, "target_mb": 8})
        self.assertIsNone(err)
        self.assertEqual(params["resolution"], 0)
        self.assertIs(params["keep_audio"], True)

    def test_string_numbers_are_coerced(self):
        params, err = ct.validate_export_request(
            {"start": "0", "end": "3.5", "target_mb": "25"})
        self.assertIsNone(err)
        self.assertEqual(params["end"], 3.5)

    def test_non_object_body_rejected(self):
        for bad in ([], "hello", 42, None):
            with self.subTest(bad=bad):
                params, err = ct.validate_export_request(bad)
                self.assertIsNone(params)
                self.assertTrue(err)

    def test_missing_fields_rejected(self):
        params, err = ct.validate_export_request({"start": 0})
        self.assertIsNone(params)
        self.assertIn("numbers", err)

    def test_non_numeric_fields_rejected(self):
        params, err = ct.validate_export_request(
            {"start": "abc", "end": 5, "target_mb": 10})
        self.assertIsNone(params)
        self.assertIn("numbers", err)

    def test_nan_and_inf_rejected(self):
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(bad=bad):
                p = dict(self.BASE, end=bad)
                params, err = ct.validate_export_request(p)
                self.assertIsNone(params)
                self.assertIn("finite", err)

    def test_negative_start_rejected(self):
        params, err = ct.validate_export_request(dict(self.BASE, start=-1))
        self.assertIsNone(params)
        self.assertIn("negative", err)

    def test_too_short_selection_rejected(self):
        params, err = ct.validate_export_request(
            dict(self.BASE, start=2.0, end=2.05))
        self.assertIsNone(params)
        self.assertIn("too short", err)

    def test_reversed_selection_rejected(self):
        params, err = ct.validate_export_request(
            dict(self.BASE, start=9.0, end=1.0))
        self.assertIsNone(params)
        self.assertIn("too short", err)

    def test_target_size_bounds(self):
        for bad in (0, 0.5, -3, 4097, 100000):
            with self.subTest(bad=bad):
                params, err = ct.validate_export_request(
                    dict(self.BASE, target_mb=bad))
                self.assertIsNone(params)
                self.assertIn("between 1 and 4096", err)
        # boundaries are inclusive
        for ok in (1, 4096):
            params, err = ct.validate_export_request(
                dict(self.BASE, target_mb=ok))
            self.assertIsNone(err)

    def test_bad_resolution_rejected(self):
        params, err = ct.validate_export_request(
            dict(self.BASE, resolution="tall"))
        self.assertIsNone(params)
        self.assertIn("resolution", err)
        params, err = ct.validate_export_request(
            dict(self.BASE, resolution=99999))
        self.assertIsNone(params)
        self.assertIn("resolution", err)


# --------------------------------------------------------------- Bitrate + cap
class TestComputeBitrates(unittest.TestCase):
    def test_typical_clip(self):
        v, a, capped = ct.compute_bitrates(10, 30, True)
        self.assertFalse(capped)
        self.assertEqual(a, 160)
        # 10 MB over 30 s ~= 2730 kbit/s total, minus audio, times 0.97
        self.assertAlmostEqual(v, int((10 * 8 * 1024 / 30 - 160) * 0.97))

    def test_audio_tiers(self):
        self.assertEqual(ct.compute_bitrates(4, 10, True)[1], 96)
        self.assertEqual(ct.compute_bitrates(8, 10, True)[1], 128)
        self.assertEqual(ct.compute_bitrates(9, 10, True)[1], 160)

    def test_no_audio(self):
        v, a, _ = ct.compute_bitrates(10, 30, False)
        self.assertEqual(a, 0)
        self.assertGreater(v, 0)

    def test_cap_applied_for_huge_target_short_clip(self):
        # The audit case: 512 MB target on a 10 s clip -> ~400,000 kbps.
        v, a, capped = ct.compute_bitrates(512, 10, True)
        self.assertTrue(capped)
        self.assertEqual(v, ct.MAX_VIDEO_KBPS)

    def test_cap_never_exceeded(self):
        for mb, dur in ((4096, 1), (1000, 5), (512, 10), (100, 2)):
            v, _, _ = ct.compute_bitrates(mb, dur, True)
            self.assertLessEqual(v, ct.MAX_VIDEO_KBPS)

    def test_zero_duration_does_not_divide_by_zero(self):
        v, _, capped = ct.compute_bitrates(10, 0, True)
        self.assertTrue(capped)
        self.assertEqual(v, ct.MAX_VIDEO_KBPS)

    def test_cap_matches_frontend_mirror(self):
        # The UI hardcodes the same ceiling; keep them in sync.
        src = _APP.read_text(encoding="utf-8")
        self.assertIn(f"const MAX_KBPS = {ct.MAX_VIDEO_KBPS}", src)


# ------------------------------------------------------------------ Job pruning
class TestPruneJobs(unittest.TestCase):
    @staticmethod
    def _jobs(n, status="done"):
        return {str(i): {"status": status} for i in range(1, n + 1)}

    def test_under_limit_untouched(self):
        jobs = self._jobs(5)
        ct.prune_jobs(jobs, keep=8)
        self.assertEqual(len(jobs), 5)

    def test_oldest_finished_dropped(self):
        jobs = self._jobs(12)
        ct.prune_jobs(jobs, keep=8)
        self.assertEqual(len(jobs), 8)
        self.assertEqual(sorted(jobs, key=int),
                         [str(i) for i in range(5, 13)])

    def test_numeric_not_lexicographic_ordering(self):
        jobs = self._jobs(11)
        ct.prune_jobs(jobs, keep=2)
        # "10","11" survive - a string sort would have kept "9" and dropped "10"
        self.assertEqual(sorted(jobs, key=int), ["10", "11"])

    def test_running_jobs_never_pruned(self):
        jobs = self._jobs(12)
        jobs["1"] = {"status": "encoding"}
        jobs["2"] = {"status": "queued"}
        ct.prune_jobs(jobs, keep=1)
        self.assertIn("1", jobs)
        self.assertIn("2", jobs)
        self.assertIn("12", jobs)
        self.assertEqual(len(jobs), 3)

    def test_all_terminal_statuses_are_prunable(self):
        jobs = {"1": {"status": "done"}, "2": {"status": "error"},
                "3": {"status": "cancelled"}}
        ct.prune_jobs(jobs, keep=0)
        self.assertEqual(jobs, {})

    def test_non_job_keys_survive(self):
        jobs = dict(self._jobs(12))
        jobs["counter"] = 12
        ct.prune_jobs(jobs, keep=1)
        self.assertEqual(jobs["counter"], 12)


# --------------------------------------------------------------- misc. helpers
class TestExportActive(unittest.TestCase):
    def test_detects_running_job(self):
        self.assertTrue(ct.export_active({"1": {"status": "encoding"}}))
        self.assertTrue(ct.export_active({"1": {"status": "queued"}}))

    def test_ignores_finished_and_non_dicts(self):
        self.assertFalse(ct.export_active({"1": {"status": "done"},
                                           "2": {"status": "error"},
                                           "3": {"status": "cancelled"},
                                           "counter": 3}))
        self.assertFalse(ct.export_active({}))


class TestParseFpsFraction(unittest.TestCase):
    def test_common_rates(self):
        self.assertAlmostEqual(ct.parse_fps_fraction("60/1"), 60.0)
        self.assertAlmostEqual(ct.parse_fps_fraction("144/1"), 144.0)
        self.assertAlmostEqual(ct.parse_fps_fraction("30000/1001"),
                               30000 / 1001)
        self.assertAlmostEqual(ct.parse_fps_fraction("59.94"), 59.94)

    def test_junk_falls_back_to_default(self):
        for bad in ("0/0", "N/A", "", None, "abc", "0/1", "-60/1", "5000/1"):
            with self.subTest(bad=bad):
                self.assertEqual(ct.parse_fps_fraction(bad), 30.0)

    def test_custom_default(self):
        self.assertEqual(ct.parse_fps_fraction("junk", default=60.0), 60.0)


class TestGuessVideoMime(unittest.TestCase):
    def test_known_extensions(self):
        cases = {
            "a.mp4": "video/mp4", "a.M4V": "video/mp4",
            "a.mov": "video/quicktime", "a.webm": "video/webm",
            "a.mkv": "video/x-matroska", "a.avi": "video/x-msvideo",
            "a.ts": "video/mp2t",
        }
        for name, mime in cases.items():
            with self.subTest(name=name):
                self.assertEqual(ct.guess_video_mime(name), mime)

    def test_unknown_extension_is_octet_stream(self):
        self.assertEqual(ct.guess_video_mime("a.xyz"),
                         "application/octet-stream")
        self.assertEqual(ct.guess_video_mime("noext"),
                         "application/octet-stream")

    def test_accepts_path_objects(self):
        self.assertEqual(ct.guess_video_mime(Path("c:/clips/x.mkv")),
                         "video/x-matroska")


if __name__ == "__main__":
    unittest.main(verbosity=2)
