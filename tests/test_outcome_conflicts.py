#!/usr/bin/env python3
"""实际适配器的保守尺寸核验；仅替换 HTTP 边界，不联网、不读写媒体。"""

import copy
import json
import socket
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from mrouter import adapters, transport  # noqa: E402
from mrouter.adapters import Ctx, GenRequest  # noqa: E402
from mrouter.config import ModelSpec  # noqa: E402
from mrouter.outcomes import assess_result  # noqa: E402


class HttpResponse:
    status = 200
    headers = {"Content-Type": "application/json"}

    def __init__(self, payload):
        self.body = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.body


class OutcomeConflictTests(unittest.TestCase):
    def setUp(self):
        self.guards = ExitStack()
        self.addCleanup(self.guards.close)
        for name in ("socket", "create_connection", "getaddrinfo"):
            self.guards.enter_context(patch.object(
                socket, name, side_effect=AssertionError("socket access forbidden")))

    def assess_adapter(self, provider="fal", *, kind="image", params=None,
                       options=None, extra=None, size="", actual=(1280, 720),
                       duration=5, actual_duration=5, watermark=False):
        spec = ModelSpec(id="offline", provider=provider, kind=kind,
                         model="offline-model", endpoint="https://stub.invalid/submit",
                         params=copy.deepcopy(params or {}),
                         options=copy.deepcopy(options or {}))
        req = GenRequest(kind=kind, prompt="offline", size=size,
                         duration=duration if kind == "video" else 0,
                         extra=copy.deepcopy(extra or {}), output_dir=None)
        ctx = Ctx(api_key="", key_source="offline-fixture", timeout=1,
                  poll_interval=0, max_poll=1)
        url = "https://stub.invalid/result" + ("_watermark" if watermark else "")
        url += ".mp4" if kind == "video" else ".png"
        payload = ({"data": [{"url": url}]} if provider == "generic_http" else
                   {"video" if kind == "video" else "images": [{"url": url}]})
        requests = []

        def respond(request, timeout):
            self.assertEqual(request.full_url, "https://stub.invalid/submit")
            self.assertEqual(request.get_method(), "POST")
            requests.append(json.loads(request.data.decode("utf-8")))
            return HttpResponse(payload)

        before = copy.deepcopy((spec, req))
        with ExitStack() as guards:
            guards.enter_context(patch.object(transport, "open_request", side_effect=respond))
            for name in ("open", "read_bytes", "read_text", "write_bytes", "write_text",
                         "mkdir", "replace", "unlink"):
                guards.enter_context(patch.object(
                    Path, name, side_effect=AssertionError("file I/O forbidden")))
            result = adapters.run_adapter(spec, req, None, ctx)
            self.assertEqual((spec, req), before, "adapter must not mutate specification/request")
            self.assertEqual(result.status, "ok")
            self.assertEqual(result.files, [], "no-download fixture must not write files")
            self.assertEqual(result.urls, [url])
            self.assertEqual(len(requests), 1, "only one mocked HTTP submission is expected")
            # 合成同路径、正 bytes 档案，避免“没有文件”的 unverified 掩盖规格错误。
            path = "synthetic.mp4" if kind == "video" else "synthetic.png"
            result.files = [path]
            profiles = [{"path": path, "bytes": 100, "width": actual[0],
                         "height": actual[1], "duration_seconds": actual_duration,
                         "format": "mp4" if kind == "video" else "png"}]
            before_assessment = copy.deepcopy((spec, req, result, profiles))
            assessment, notes = assess_result(spec, req, result, profiles)
            self.assertEqual((spec, req, result, profiles), before_assessment,
                             "assessment must not mutate its inputs")
        self.assertTrue(assessment["local_files_checked"])
        return assessment, notes, requests[0], result

    def test_generic_nested_size_is_unverified_not_platform_default(self):
        assessment, notes, body, result = self.assess_adapter(
            "generic_http", size="640x480", options={
                "submit": {"body": {"input": {"size": "{{size}}"}}},
                "result_path": "data"})
        self.assertEqual(body, {"input": {"size": "640x480"}})
        self.assertEqual(assessment["status"], "unverified")
        self.assertEqual(result.meta["effective_spec"]["size_status"], "unverified")
        self.assertNotIn("平台默认", " ".join(notes))
        self.assertNotIn("未指定", " ".join(notes))
        self.assertIn("未核验", " ".join(notes))
        self.assertNotIn("input", result.meta["effective_spec"])

    def test_fal_width_height_and_image_size_conflict_is_unverified(self):
        params = {"width": 1280, "height": 720,
                  "image_size": {"width": 640, "height": 480}}
        for actual in ((1280, 720), (640, 480), (320, 240)):
            with self.subTest(actual=actual):
                assessment, notes, body, _ = self.assess_adapter(params=params, actual=actual)
                for field, value in params.items():
                    self.assertEqual(body[field], value)
                self.assertEqual(assessment["status"], "unverified")
                self.assertIn("冲突", " ".join(notes))
                self.assertNotIn("尺寸不一致", " ".join(notes))

    def test_fal_size_and_image_size_conflict_is_unverified(self):
        params = {"size": "1280x720", "image_size": "640x480"}
        for actual in ((1280, 720), (640, 480), (320, 240)):
            with self.subTest(actual=actual):
                assessment, notes, body, _ = self.assess_adapter(params=params, actual=actual)
                for field, value in params.items():
                    self.assertEqual(body[field], value)
                self.assertEqual(assessment["status"], "unverified")
                self.assertIn("冲突", " ".join(notes))
                self.assertNotIn("尺寸不一致", " ".join(notes))

    def test_fal_width_height_and_size_conflict_is_unverified(self):
        assessment, notes, body, _ = self.assess_adapter(
            params={"width": 1280, "height": 720}, extra={"size": "640x480"})
        self.assertEqual(body["width"], 1280)
        self.assertEqual(body["height"], 720)
        self.assertEqual(body["size"], "640x480")
        self.assertEqual(assessment["status"], "unverified")
        self.assertIn("冲突", " ".join(notes))

    def test_fal_three_exact_groups_conflict_without_authoritative_override(self):
        for params in (
                {"width": 1280, "height": 720, "size": "1280x720", "image_size": "640x480"},
                {"width": 1280, "height": 720, "size": "640x480", "image_size": "1280x720"},
                {"width": 640, "height": 480, "size": "1280x720", "image_size": "1280x720"}):
            with self.subTest(params=params):
                assessment, notes, _, _ = self.assess_adapter(params=params)
                self.assertEqual(assessment["status"], "unverified")
                self.assertIn("冲突", " ".join(notes))
                self.assertNotIn("尺寸不一致", " ".join(notes))

    def test_fal_matching_exact_groups_can_be_checked(self):
        for params in (
                {"width": 1280, "height": 720, "image_size": "1280x720"},
                {"size": "1280X720", "image_size": {"width": 1280, "height": 720}},
                {"width": 1280, "height": 720, "size": "1280*720", "image_size": "1280x720"}):
            with self.subTest(params=params):
                assessment, notes, _, _ = self.assess_adapter(params=params)
                self.assertEqual(assessment["status"], "checked")
                self.assertNotIn("冲突", " ".join(notes))
                self.assertNotIn("未核验", " ".join(notes))

    def test_fal_single_or_consistent_exact_groups_still_mismatch(self):
        for params in ({"width": 1280, "height": 720}, {"size": "1280x720"},
                       {"image_size": {"width": 1280, "height": 720}},
                       {"width": 1280, "height": 720, "size": "1280x720", "image_size": "1280x720"}):
            with self.subTest(params=params):
                assessment, notes, _, _ = self.assess_adapter(params=params, actual=(1279, 720))
                self.assertEqual(assessment["status"], "mismatch")
                self.assertIn("尺寸不一致", " ".join(notes))
                self.assertNotIn("冲突", " ".join(notes))

    def test_fal_matching_exact_groups_do_not_verify_unknown_constraint(self):
        for unknown in ({"ratio": "16:9"}, {"resolution": "720p"},
                        {"image_size": "landscape_16_9"}):
            with self.subTest(unknown=unknown):
                assessment, notes, _, _ = self.assess_adapter(
                    params={"width": 1280, "height": 720, "size": "1280x720", **unknown})
                self.assertEqual(assessment["status"], "unverified")
                self.assertIn("未核验", " ".join(notes))
                self.assertNotIn("冲突", " ".join(notes))

    def test_duration_and_watermark_mismatch_take_priority_over_size_conflict(self):
        for actual_duration, watermark in ((5.19, False), (5, True), (5.19, True)):
            with self.subTest(duration=actual_duration, watermark=watermark):
                assessment, notes, _, _ = self.assess_adapter(
                    kind="video", params={"size": "1280x720", "image_size": "640x480"},
                    actual_duration=actual_duration, watermark=watermark)
                self.assertEqual(assessment["status"], "mismatch")
                self.assertIn("冲突", " ".join(notes))
                self.assertIn("未核验", " ".join(notes))
                self.assertNotIn("尺寸不一致", " ".join(notes))
                if actual_duration != 5:
                    self.assertEqual(assessment["duration"][0]["status"], "mismatch")
                    self.assertIn("时长不一致", " ".join(notes))
                if watermark:
                    self.assertEqual(assessment["watermark"], "present")
                    self.assertIn("带水印", " ".join(notes))

    def test_generic_nested_fixed_or_arbitrary_structure_is_not_inferred(self):
        for template in ({"input": {"size": "640x480"}},
                         {"payload": [{"custom_sizing": "{{size}}"}]},
                         {"size": "1280x720", "input": {"size": "{{size}}"}}):
            with self.subTest(template=template):
                assessment, notes, _, result = self.assess_adapter(
                    "generic_http", size="640x480", options={
                        "submit": {"body": template}, "result_path": "data"})
                self.assertEqual(assessment["status"], "unverified")
                self.assertEqual(result.meta["effective_spec"]["size_status"], "unverified")
                self.assertNotIn("平台默认", " ".join(notes))
                self.assertNotIn("input", result.meta["effective_spec"])
                self.assertNotIn("payload", result.meta["effective_spec"])

    def test_generic_requested_size_without_effective_evidence_is_unverified(self):
        assessment, notes, body, _ = self.assess_adapter("generic_http", size="640x480")
        self.assertNotIn("size", body)
        self.assertEqual(assessment["status"], "unverified")
        self.assertNotIn("平台默认", " ".join(notes))

    def test_generic_known_direct_size_and_unconstrained_default_remain_checked(self):
        for template, size, expected_size in (
                ({"size": "{{size}}"}, "1280x720", "explicit"),
                ({"image_size": {"width": 1280, "height": 720}}, "", "explicit"),
                ({"prompt": "{{prompt}}"}, "", "platform_default")):
            with self.subTest(template=template):
                assessment, notes, _, result = self.assess_adapter(
                    "generic_http", size=size, options={
                        "submit": {"body": template}, "result_path": "data"})
                self.assertEqual(assessment["status"], "checked")
                self.assertEqual(result.meta["effective_spec"]["size_status"], expected_size)
                if expected_size == "explicit":
                    self.assertNotIn("平台默认", " ".join(notes))
                else:
                    self.assertIn("平台默认", " ".join(notes))

    def test_generic_nested_body_and_credentials_are_not_copied_to_evidence(self):
        assessment, notes, body, result = self.assess_adapter(
            "generic_http", size="640x480", options={
                "submit": {"body": {"input": {"size": "{{size}}"},
                                    "credential": "fixture-must-not-copy",
                                    "callback": "https://private.invalid/?token=fixture-only"}},
                "result_path": "data"})
        self.assertEqual(body["credential"], "fixture-must-not-copy")
        evidence = json.dumps([result.meta["effective_spec"], assessment, notes])
        for forbidden in ("fixture-must-not-copy", "private.invalid", "fixture-only",
                          "callback", "credential", "input", "prompt", "body"):
            self.assertNotIn(forbidden, evidence)
        self.assertEqual(assessment["status"], "unverified")


if __name__ == "__main__":
    unittest.main(verbosity=2)
