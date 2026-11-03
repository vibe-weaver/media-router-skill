#!/usr/bin/env python3
"""生效规格与只读验收：只截获请求和使用合成档案，零依赖、不联网。"""

import copy
import importlib
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from mrouter import adapters, config, transport  # noqa: E402
from mrouter.adapters import Ctx, GenRequest, GenResult  # noqa: E402
from mrouter.caption import profile_files  # noqa: E402
from mrouter.config import ModelSpec  # noqa: E402


class Response:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class AdapterEvidenceTests(unittest.TestCase):
    def capture(self, provider, kind="video", *, params=None, options=None,
                model="test-model", async_result=False, inline=False, **request_kw):
        spec = ModelSpec(id="test", provider=provider, kind=kind, model=model,
                         endpoint="https://stub.invalid/submit", params=params or {},
                         options=options or {})
        req = GenRequest(kind=kind, prompt="offline", **request_kw)
        ctx = Ctx(api_key="offline-test-key", key_source="test", timeout=1,
                  poll_interval=0, max_poll=1)
        calls = []
        url = "https://stub.invalid/result.mp4" if kind == "video" else "https://stub.invalid/result.png"

        def respond(method, target, **kw):
            calls.append((method, copy.deepcopy(kw)))
            if provider == "openai":
                if kind == "video":
                    return Response({"id": "task"} if method == "POST" and async_result else
                                    {"task_status": "SUCCESS", "data": [{"url": url}]})
                return Response({"data": [{"b64_json": "offline"} if inline else {"url": url}]})
            if provider == "dashscope":
                if model.startswith("qwen-image"):
                    return Response({"output": {"choices": [{"message": {"content": [{"image": url}]}}]}})
                return Response({"output": {"task_id": "task"}} if method == "POST" else
                                {"output": {"task_status": "SUCCEEDED", "results": [{"url": url}]}})
            if provider == "volcengine":
                if kind == "video":
                    return Response({"id": "task"} if method == "POST" else
                                    {"status": "succeeded", "content": {"video_url": url}})
                return Response({"data": [{"b64_json": "offline"} if inline else {"url": url}]})
            if provider == "kling":
                return Response({"code": 0, "data": {"task_id": "task"}} if method == "POST" else
                                {"code": 0, "data": {"task_status": "succeed", "task_result":
                                 {"videos" if kind == "video" else "images": [{"url": url}]}}})
            if provider == "replicate":
                return Response({"id": "task", "status": "succeeded", "output": [url]})
            if provider == "fal":
                if async_result:
                    if method == "POST":
                        return Response({"status_url": "https://stub.invalid/status",
                                         "response_url": "https://stub.invalid/response", "request_id": "task"})
                    if target.endswith("/status"):
                        return Response({"status": "COMPLETED"})
                return Response({"video" if kind == "video" else "images": [{"url": url}]})
            return Response({"data": [{"url": url}]})

        with patch.object(adapters, "request", side_effect=respond), \
             patch.object(adapters, "download", side_effect=AssertionError("must not download")), \
             patch.object(adapters, "read_upload", return_value=("in.png", b"offline", "image/png")), \
             patch.object(adapters, "_write_b64_to_local", return_value=Path("offline.png")), \
             patch.object(transport, "open_request", side_effect=AssertionError("must not access network")):
            result = adapters.ADAPTERS[provider](spec, req, None, ctx)
        body = calls[0][1].get("json_body") or calls[0][1].get("form") or {}
        evidence = result.meta.get("effective_spec")
        self.assertIsInstance(evidence, dict, "adapter must record final merged effective_spec")
        return evidence, body, result

    def test_openai_and_dashscope_duration_final_priority(self):
        for provider in ("openai", "dashscope"):
            for params, duration, extra, expected, source in (
                ({"duration": 3}, 5, {"duration": 7}, 7, "extra.duration"),
                ({"duration": 3}, 5, {}, 5, "request.duration"),
                ({"duration": 3}, 0, {}, 3, "params.duration"),
                ({}, 0, {}, None, "platform_default"),
            ):
                with self.subTest(provider=provider, expected=expected):
                    ev, body, _ = self.capture(provider, params=params, duration=duration, extra=extra)
                    sent = body.get("parameters", body).get("duration")
                    self.assertEqual(ev.get("duration_seconds"), sent)
                    self.assertEqual(ev.get("duration_seconds"), expected)
                    self.assertEqual(ev.get("duration_source"), source)

    def test_openai_async_and_fal_async_keep_evidence(self):
        for provider in ("openai", "fal"):
            with self.subTest(provider=provider):
                ev, _, _ = self.capture(provider, duration=5, async_result=True)
                self.assertEqual(ev.get("duration_seconds"), 5)

    def test_kling_fixed_params_override_cli_but_named_extra_wins(self):
        for extra, expected, source in (({}, 3, "params.duration"),
                                        ({"duration": "7"}, 7, "extra.duration")):
            with self.subTest(extra=extra):
                ev, body, _ = self.capture("kling", params={"duration": 3}, duration=5, extra=extra)
                self.assertEqual(ev.get("duration_seconds"), float(body["duration"]))
                self.assertEqual(ev.get("duration_seconds"), expected)
                self.assertEqual(ev.get("duration_source"), source)
        ev, body, _ = self.capture("kling", duration=5)
        self.assertEqual(body["duration"], "5", "existing string payload must not change")
        self.assertEqual(ev.get("duration_seconds"), 5)
        self.assertEqual(ev.get("duration_source"), "request.duration")

    def test_volcengine_records_text_flags_not_ignored_extra_duration(self):
        for duration, expected, source in ((5, 5, "request.duration"), (0, 3, "params.duration")):
            with self.subTest(duration=duration):
                ev, body, _ = self.capture("volcengine", params={"duration": 3}, duration=duration,
                                           extra={"duration": 7})
                self.assertIn(" --dur " + str(expected), body["content"][0]["text"])
                self.assertNotIn(" --dur 7", body["content"][0]["text"])
                self.assertEqual(ev.get("duration_seconds"), expected)
                self.assertEqual(ev.get("duration_source"), source)

    def test_replicate_fal_all_input_priorities_and_cli(self):
        cases = (
            ({"inputs": {"duration": 4}}, {"duration": 6, "inputs": {"duration": 7}}, 5, 7, "extra.inputs.duration"),
            ({"inputs": {"duration": 4}}, {"duration": 6}, 5, 6, "extra.duration"),
            ({"inputs": {"duration": 4}}, {}, 5, 5, "request.duration"),
            ({"inputs": {"duration": 4}}, {}, 0, 4, "options.inputs.duration"),
            ({}, {}, 0, 3, "params.duration"),
        )
        for provider in ("replicate", "fal"):
            for options, extra, duration, expected, source in cases:
                with self.subTest(provider=provider, source=source):
                    ev, body, _ = self.capture(provider, params={"duration": 3}, options=options,
                                               duration=duration, extra=extra)
                    sent = body.get("input", body)["duration"]
                    self.assertEqual(ev.get("duration_seconds"), sent)
                    self.assertEqual(ev.get("duration_seconds"), expected)
                    self.assertEqual(ev.get("duration_source"), source)

    def test_passthrough_disabled_and_unknown_duration_units(self):
        for provider in ("replicate", "fal"):
            for field in (False, "none", "num_frames", "video_length"):
                with self.subTest(provider=provider, field=field):
                    ev, body, _ = self.capture(provider, duration=5, options={"duration_field": field})
                    sent = body.get("input", body)
                    self.assertNotIn("duration", sent)
                    self.assertIsNone(ev.get("duration_seconds"))
                    self.assertIn(ev.get("duration_source"), ("disabled", "unverified"))
                    if field in ("num_frames", "video_length"):
                        self.assertEqual(sent[field], 5, "recording must not change existing mapping")
            ev, body, _ = self.capture(provider, duration=5, params={"duration": 3},
                                       options={"duration_field": False})
            self.assertEqual(body.get("input", body)["duration"], 3)
            self.assertEqual(ev.get("duration_seconds"), 3, "disabled injection must not hide actually sent params")

    def test_generic_direct_seconds_template_observes_final_overrides(self):
        options = {"submit": {"body": {"duration": "{{duration}}", "size": "{{size}}"}},
                   "allowed_params": ["duration"], "result_path": "data"}
        ev, body, _ = self.capture("generic_http", options=options, duration=5,
                                   params={"duration": 3}, extra={"duration": 7})
        self.assertEqual(body["duration"], 7)
        self.assertEqual(ev.get("duration_seconds"), body["duration"])
        self.assertEqual(ev.get("duration_source"), "extra.duration")
        ev, body, _ = self.capture("generic_http", options=options, duration=5)
        self.assertEqual(ev.get("duration_seconds"), body["duration"])
        self.assertEqual(ev.get("duration_source"), "template.duration")

    def test_generic_arbitrary_template_and_get_body_are_unverified(self):
        for template, method in (({"num_frames": "{{duration}}"}, "POST"),
                                 ({"input": {"duration": "{{duration}}"}}, "POST"),
                                 ({"duration": "{{duration}}ms"}, "POST"),
                                 ({"duration": "{{duration}}"}, "GET"),
                                 ({"duration": 5}, "POST")):
            with self.subTest(template=template, method=method):
                ev, _, _ = self.capture("generic_http", duration=5, options={"result_path": "data",
                                       "submit": {"body": template, "method": method}})
                self.assertIsNone(ev.get("duration_seconds"))
                self.assertEqual(ev.get("duration_source"), "unverified")

    def test_common_image_adapters_record_actual_extra_size_not_defaults(self):
        for provider, model in (("openai", "model"), ("dashscope", "wanx-test"),
                                ("dashscope", "qwen-image-test"), ("volcengine", "model"),
                                ("kling", "model"), ("replicate", "model"),
                                ("fal", "model"), ("generic_http", "model")):
            with self.subTest(provider=provider, model=model):
                options = {"allowed_params": ["size"]} if provider == "generic_http" else None
                ev, body, _ = self.capture(provider, "image", model=model, params={"size": "512x512"},
                                           options=options, extra={"size": "1024x768"})
                sent = body.get("parameters", body.get("input", body))["size"]
                self.assertEqual(ev.get("size"), sent)
                self.assertEqual(ev.get("size_source"), "extra.size")
                self.assertEqual(ev.get("size_status"), "explicit")

    def test_dashscope_image_cli_size_overrides_extra_while_video_does_not(self):
        for kind, model, expected, source in (("image", "qwen-image-test", "640*480", "request.size"),
                                              ("image", "wanx-test", "640*480", "request.size"),
                                              ("video", "wanx-test", "1024*768", "extra.size")):
            with self.subTest(kind=kind, model=model):
                ev, body, _ = self.capture("dashscope", kind, model=model, size="640x480",
                                           params={"size": "512x512"}, extra={"size": "1024x768"})
                self.assertEqual(body["parameters"]["size"], expected)
                self.assertEqual(ev.get("size"), expected)
                self.assertEqual(ev.get("size_source"), source)

    def test_openai_edits_and_inline_image_paths_keep_size_evidence(self):
        ev, body, _ = self.capture("openai", "image", images=["offline.png"], extra={"size": "640x480"})
        self.assertEqual(ev.get("size"), body["size"])
        for provider in ("openai", "volcengine"):
            with self.subTest(provider=provider):
                ev, body, result = self.capture(provider, "image", inline=True, output_dir=Path("unused"),
                                                extra={"size": "640x480"})
                self.assertEqual(result.files, ["offline.png"])
                self.assertEqual(ev.get("size"), body["size"])

    def test_passthrough_dimensions_and_size_objects_are_whitelisted(self):
        for provider in ("replicate", "fal"):
            with self.subTest(provider=provider):
                ev, body, _ = self.capture(provider, "image", params={"width": 512, "height": 512},
                                           extra={"width": 1024, "height": 768,
                                                  "image_size": {"width": 640, "height": 480, "note": "must-not-copy"}})
                sent = body.get("input", body)
                self.assertEqual(ev.get("width"), sent["width"])
                self.assertEqual(ev.get("height"), sent["height"])
                self.assertEqual(ev.get("image_size"), {"width": 640, "height": 480})
                self.assertNotIn("must-not-copy", json.dumps(ev))

    def test_metadata_never_copies_arbitrary_body_or_secrets(self):
        ev, _, _ = self.capture("openai", extra={"duration": 5, "size": "https://secret.invalid/?token=private",
                                               "opaque_marker": "must-not-copy", "seed": 999,
                                               "watermark": False, "custom": {"note": "must-not-copy"}})
        text = json.dumps(ev)
        for value in ("must-not-copy", "secret.invalid", "private", "opaque_marker", "seed", "custom", "prompt"):
            self.assertNotIn(value, text)
        self.assertEqual(ev.get("duration_seconds"), 5)
        self.assertEqual(ev.get("size_status"), "unverified")

    def test_identity_and_credential_overrides_are_rejected_before_submission(self):
        for extra in ({"model": "unapproved"}, {"model_name": "unapproved"},
                      {"api_key": "must-not-copy"}, {"endpoint": "https://other.invalid"},
                      {"inputs": {"model_id": "unapproved"}},
                      {"custom": [{"resource_id": "unapproved"}]}):
            with self.subTest(keys=sorted(extra)):
                spec = ModelSpec(id="test", provider="openai", kind="image", model="test-model",
                                 endpoint="https://stub.invalid/submit")
                req = GenRequest(kind="image", prompt="offline", extra=extra)
                ctx = Ctx(api_key="offline-test-key", key_source="test", timeout=1,
                          poll_interval=0, max_poll=1)
                with patch.object(adapters, "request") as submit:
                    with self.assertRaises(config.ConfigError) as caught:
                        adapters.gen_openai(spec, req, None, ctx)
                submit.assert_not_called()
                self.assertNotIn("unapproved", str(caught.exception))
                self.assertNotIn("must-not-copy", str(caught.exception))

    def test_volcengine_video_records_sent_size_flags_without_changing_request(self):
        for field, value in (("size", "1280x720"), ("image_size", "landscape_16_9"),
                             ("resolution", "720p"), ("width", 1280), ("height", 720),
                             ("aspect_ratio", "16:9")):
            with self.subTest(field=field):
                ev, body, _ = self.capture("volcengine", duration=5, aspect_ratio="16:9",
                    params={field: "not-sent"},
                    extra={field: value, "seed": 0, "opaque_marker": "must-not-copy"})
                self.assertEqual(body, {"model": "test-model", "content": [{"type": "text", "text":
                    f"offline --ratio 16:9 --dur 5 --{field} {value} --seed 0 --opaque_marker must-not-copy"}]})
                self.assertEqual(ev.get(field), value)
                self.assertEqual(ev.get(field + "_source"), "extra." + field)
                self.assertEqual(ev.get("size_status"), "explicit")
                self.assertEqual(ev.get("ratio_source"), "request.aspect_ratio")
                self.assertNotIn("must-not-copy", json.dumps(ev))
                self.assertNotIn("seed", ev)

    def test_volcengine_does_not_invent_unsent_fixed_or_cli_dimensions(self):
        ev, body, _ = self.capture("volcengine", duration=5, size="2048x2048",
            params={"size": "1024x1024", "image_size": "square", "resolution": "1080p",
                    "width": 1024, "height": 1024, "aspect_ratio": "1:1"})
        self.assertEqual(body, {"model": "test-model", "content": [{"type": "text", "text": "offline --dur 5"}]})
        self.assertEqual(ev.get("size_status"), "platform_default")
        for field in ("size", "image_size", "resolution", "width", "height", "aspect_ratio"):
            self.assertNotIn(field, ev)

    def test_volcengine_extra_dur_is_unverified_without_reordering_flags(self):
        for duration, params, value, prefix in (
            (5, {"duration": 3}, 7, "offline --dur 5"),
            (0, {"duration": 3}, "unknown", "offline --dur 3"),
            (0, {}, 7, "offline"),
            (5, {}, "7 --dur 9", "offline --dur 5"),
            (5, {}, 0, "offline --dur 5"),
            (5, {}, False, "offline --dur 5"),
        ):
            with self.subTest(duration=duration, params=params, value=value):
                ev, body, _ = self.capture("volcengine", duration=duration, params=params,
                    extra={"seed": 0, "dur": value, "watermark": False})
                sent = "false" if value is False else str(value)
                self.assertEqual(body, {"model": "test-model", "content": [{"type": "text", "text":
                    f"{prefix} --seed 0 --dur {sent} --watermark false"}]})
                self.assertIsNone(ev.get("duration_seconds"))
                self.assertEqual(ev.get("duration_source"), "unverified")
                self.assertEqual(ev.get("duration_field"), "--dur")

    def test_volcengine_unsent_empty_dur_does_not_hide_known_duration(self):
        for value in (None, ""):
            with self.subTest(value=value):
                ev, body, _ = self.capture("volcengine", duration=5, extra={"dur": value})
                self.assertEqual(body["content"][0]["text"], "offline --dur 5")
                self.assertEqual(ev.get("duration_seconds"), 5)
                self.assertEqual(ev.get("duration_source"), "request.duration")

    def test_passthrough_aspect_ratio_source_only_for_actual_cli_injection(self):
        cases = (
            ({}, {}, {}, "16:9", "request.aspect_ratio"),
            ({"aspect_ratio": "4:3"}, {}, {}, "4:3", "params.aspect_ratio"),
            ({"aspect_ratio": "16:9"}, {}, {}, "16:9", "params.aspect_ratio"),
            ({"aspect_ratio": "4:3"}, {"inputs": {"aspect_ratio": "1:1"}}, {}, "1:1", "options.inputs.aspect_ratio"),
            ({}, {"inputs": {"aspect_ratio": "4:3"}}, {"aspect_ratio": "16:9"}, "16:9", "extra.aspect_ratio"),
            ({}, {}, {"aspect_ratio": "4:3", "inputs": {"aspect_ratio": "16:9"}}, "16:9", "extra.inputs.aspect_ratio"),
        )
        for provider in ("replicate", "fal"):
            for kind in ("image", "video"):
                for params, options, extra, value, source in cases:
                    with self.subTest(provider=provider, kind=kind, source=source):
                        ev, body, _ = self.capture(provider, kind, params=params, options=options,
                            extra=extra, aspect_ratio="16:9")
                        self.assertEqual(body.get("input", body)["aspect_ratio"], value)
                        self.assertEqual(ev.get("aspect_ratio"), value)
                        self.assertEqual(ev.get("aspect_ratio_source"), source)

    def test_passthrough_existing_empty_ratio_does_not_claim_cli_injection(self):
        for provider in ("replicate", "fal"):
            for value in (None, ""):
                for origin in ("params", "options", "extra", "nested"):
                    with self.subTest(provider=provider, value=value, origin=origin):
                        params = {"aspect_ratio": value} if origin == "params" else {}
                        options = {"inputs": {"aspect_ratio": value}} if origin == "options" else {}
                        extra = ({"aspect_ratio": value} if origin == "extra" else
                                 {"inputs": {"aspect_ratio": value}} if origin == "nested" else {})
                        ev, body, _ = self.capture(provider, params=params, options=options,
                            extra=extra, aspect_ratio="16:9")
                        self.assertEqual(body.get("input", body)["aspect_ratio"], value)
                        self.assertNotIn("aspect_ratio", ev)
                        self.assertNotIn("aspect_ratio_source", ev)


class AssessmentTests(unittest.TestCase):
    def assess(self, *, kind="video", duration=5, evidence=None, profiles=None, files=None,
               meta=None, extra=None, size=""):
        try:
            assessor = getattr(importlib.import_module("mrouter.outcomes"), "assess_result", None)
        except ModuleNotFoundError as exc:
            if exc.name != "mrouter.outcomes":
                raise
            assessor = None
        self.assertTrue(callable(assessor), "shared assess_result interface must exist")
        spec = ModelSpec(id="test", provider="openai", kind=kind)
        req = GenRequest(kind=kind, duration=duration, size=size, extra=extra or {})
        profiles = profiles if profiles is not None else [
            {"path": "v.mp4", "duration_seconds": 5.19, "width": 1890,
             "height": 1080, "bytes": 100, "format": "mp4"}]
        result = GenResult(status="ok", model_id="test", provider="openai",
                           files=files if files is not None else [p["path"] for p in profiles],
                           meta={"effective_spec": evidence if evidence is not None else
                                 {"duration_seconds": 5, "duration_source": "extra.duration", "size_status": "platform_default"},
                                 **(meta or {})})
        before = copy.deepcopy((spec, req, result, profiles))
        with patch.object(transport, "open_request", side_effect=AssertionError("must not network")), \
             patch.object(Path, "write_bytes", side_effect=AssertionError("must not write")), \
             patch.object(Path, "write_text", side_effect=AssertionError("must not write")), \
             patch.object(Path, "unlink", side_effect=AssertionError("must not delete")):
            assessment, notes = assessor(spec, req, result, profiles)
        self.assertEqual((spec, req, result, profiles), before, "assessment must not mutate its inputs")
        self.assertEqual(result.status, "ok", "generation success and assessment are separate")
        return assessment, notes

    def test_duration_5_vs_5_19_is_mismatch_and_watermark_warns(self):
        assessment, notes = self.assess(meta={"watermark": True})
        self.assertEqual(assessment.get("status"), "mismatch")
        self.assertEqual(assessment.get("watermark"), "present")
        self.assertEqual(assessment.get("local_files_checked"), True)
        self.assertEqual(assessment.get("duration"), [{"path": "v.mp4", "requested": 5,
                         "actual": 5.19, "status": "mismatch"}])
        self.assertIn("5.19", " ".join(notes))
        self.assertIn("水印", " ".join(notes))

    def test_match_uses_profiles_precision_without_adding_tolerance(self):
        for actual, expected in ((5, "checked"), (5.01, "mismatch"), (5.001, "mismatch")):
            with self.subTest(actual=actual):
                assessment, _ = self.assess(profiles=[{"path": "v.mp4", "bytes": 100, "width": 1280, "height": 720,
                                                       "duration_seconds": actual, "format": "mp4"}])
                self.assertEqual(assessment.get("status"), expected)

    def test_effective_duration_overrides_raw_cli_duration(self):
        assessment, _ = self.assess(duration=5, evidence={"duration_seconds": 7, "duration_source": "extra.duration"},
                                     profiles=[{"path": "v.mp4", "duration_seconds": 7, "bytes": 100, "width": 1280, "height": 720}])
        self.assertEqual(assessment.get("status"), "checked")
        self.assertEqual((assessment.get("duration") or [{}])[0].get("requested"), 7)

    def test_unknown_actual_or_unknown_effective_duration_is_unverified(self):
        for evidence, actual in (({"duration_seconds": 5}, None),
                                 ({"duration_seconds": None, "duration_source": "unverified"}, 5),
                                 ({}, 5)):
            with self.subTest(evidence=evidence, actual=actual):
                assessment, notes = self.assess(evidence=evidence,
                    profiles=[{"path": "v.mp4", "bytes": 100, "duration_seconds": actual}])
                self.assertEqual(assessment.get("status"), "unverified")
                self.assertEqual((assessment.get("duration") or [{}])[0].get("status"), "unverified")
                self.assertIn("未核验", " ".join(notes))

    def test_multiple_media_each_have_independent_duration_and_notes(self):
        profiles = [{"path": "a.mp4", "bytes": 100, "duration_seconds": 5},
                    {"path": "b.mp4", "bytes": 100, "duration_seconds": 5.19},
                    {"path": "c.mp4", "bytes": 100}]
        assessment, notes = self.assess(profiles=profiles)
        self.assertEqual(assessment.get("status"), "mismatch")
        entries = assessment.get("duration") or []
        self.assertEqual([p.get("status") for p in entries], ["checked", "mismatch", "unverified"])
        self.assertIn("b.mp4", " ".join(notes))
        self.assertIn("c.mp4", " ".join(notes))

    def test_no_files_missing_profile_or_unreadable_profile_cannot_claim_local_check(self):
        for files, profiles in (([], []), ([], [{"path": "v.mp4", "bytes": 100, "duration_seconds": 5}]),
                                (["v.mp4"], []), (["v.mp4"], [{"path": "v.mp4", "bytes": 0}]),
                                (["v.mp4", "missing.mp4"], [{"path": "v.mp4", "bytes": 100, "duration_seconds": 5}])):
            with self.subTest(files=files, profiles=profiles):
                assessment, notes = self.assess(files=files, profiles=profiles)
                self.assertEqual(assessment.get("status"), "unverified")
                self.assertEqual(assessment.get("local_files_checked"), False)
                self.assertIn("未核验", " ".join(notes))

    def test_watermark_present_is_warning_even_when_duration_matches(self):
        assessment, notes = self.assess(meta={"watermark": True},
            profiles=[{"path": "v.mp4", "bytes": 100, "duration_seconds": 5}])
        self.assertEqual(assessment.get("status"), "mismatch")
        self.assertEqual(assessment.get("watermark"), "present")
        self.assertIn("水印", " ".join(notes))

    def test_watermark_without_positive_evidence_is_unknown_never_absent(self):
        for meta in ({}, {"watermark": False}, {"watermark": "true"}, {"watermark": 1}):
            with self.subTest(meta=meta):
                assessment, notes = self.assess(meta=meta)
                self.assertEqual(assessment.get("watermark"), "unknown")
                self.assertNotIn("无水印", " ".join(notes))

    def test_image_and_video_default_size_notes_include_actual_metadata(self):
        for kind in ("image", "video"):
            with self.subTest(kind=kind):
                assessment, notes = self.assess(kind=kind, evidence={"duration_seconds": 5, "size_status": "platform_default"},
                    profiles=[{"path": "large.dat", "bytes": 5242880, "format": "png", "width": 2048,
                               "height": 2048, "duration_seconds": 5}])
                text = " ".join(notes)
                for expected in ("平台默认", "不确定", "2048", "5242880", "png"):
                    self.assertIn(expected, text)
                self.assertNotIn("裁剪", text)
                self.assertNotIn("去水印", text)
                self.assertNotIn("重生成", text)

    def test_explicit_extra_size_is_not_described_as_platform_default(self):
        assessment, notes = self.assess(kind="image", extra={"size": "1024x768"},
            evidence={"size": "1024x768", "size_source": "extra.size", "size_status": "explicit"},
            profiles=[{"path": "a.png", "bytes": 100, "width": 1024, "height": 768, "format": "png"}])
        self.assertEqual(assessment.get("status"), "checked")
        self.assertNotIn("平台默认", " ".join(notes))
        self.assertEqual(assessment.get("duration"), [])

    def test_image_sizes_are_compared_individually(self):
        assessment, notes = self.assess(kind="image", evidence={"size": "1024x768", "size_status": "explicit"},
            profiles=[{"path": "a.png", "bytes": 100, "width": 1024, "height": 768},
                      {"path": "b.png", "bytes": 100, "width": 512, "height": 512}])
        self.assertEqual(assessment.get("status"), "mismatch")
        self.assertIn("b.png", " ".join(notes))
        self.assertIn("512", " ".join(notes))

    def test_effective_spec_is_whitelisted_even_for_synthetic_results(self):
        assessment, _ = self.assess(evidence={"duration_seconds": 5, "duration_source": "extra.duration",
            "size": "https://secret.invalid/?token=private", "api_key": "must-not-copy", "body": {"token": "must-not-copy"}})
        safe = assessment.get("effective_spec") or {}
        self.assertEqual(safe.get("duration_seconds"), 5)
        self.assertNotIn("must-not-copy", json.dumps(safe))
        self.assertNotIn("secret.invalid", json.dumps(safe))

    def test_uncompared_size_constraints_are_unverified(self):
        for kind in ("image", "video"):
            for field, value in (("aspect_ratio", "16:9"), ("ratio", "16:9"),
                                 ("size", "16:9"), ("size", "2k"), ("size", 1280),
                                 ("image_size", "landscape_16_9"), ("resolution", "720p"),
                                 ("resolution", "2k"), ("width", 1280), ("height", 720)):
                with self.subTest(kind=kind, field=field, value=value):
                    assessment, notes = self.assess(kind=kind,
                        evidence={field: value, "size_status": "explicit", "duration_seconds": 5},
                        profiles=[{"path": "media.dat", "bytes": 100, "width": 1280,
                                   "height": 720, "duration_seconds": 5}])
                    self.assertEqual(assessment.get("status"), "unverified")
                    self.assertTrue(assessment.get("local_files_checked"))
                    self.assertEqual(assessment["effective_spec"][field], value)
                    self.assertIn("未核验", " ".join(notes))
                    self.assertNotIn("平台默认", " ".join(notes))

    def test_uncompared_constraint_is_not_hidden_by_matching_exact_dimensions(self):
        for kind in ("image", "video"):
            for constraint in ({"aspect_ratio": "16:9"}, {"ratio": "16:9"},
                               {"size": "2k"}, {"image_size": "landscape_16_9"},
                               {"resolution": "720p"}):
                with self.subTest(kind=kind, constraint=constraint):
                    assessment, notes = self.assess(kind=kind,
                        evidence={"width": 1280, "height": 720, "size_status": "explicit",
                                  "duration_seconds": 5, **constraint},
                        profiles=[{"path": "media.dat", "bytes": 100, "width": 1280,
                                   "height": 720, "duration_seconds": 5}])
                    self.assertEqual(assessment.get("status"), "unverified")
                    self.assertIn("未核验", " ".join(notes))

    def test_readable_image_and_video_without_actual_dimensions_are_unverified(self):
        for kind in ("image", "video"):
            for dimensions in ({}, {"width": 1280}, {"height": 720}):
                for requested in ({"size_status": "platform_default"},
                                  {"size": "1280x720", "size_status": "explicit"}):
                    with self.subTest(kind=kind, dimensions=dimensions, requested=requested):
                        assessment, notes = self.assess(kind=kind,
                            evidence={"duration_seconds": 5, **requested},
                            profiles=[{"path": "media.dat", "bytes": 100, "format": "mp4",
                                       "duration_seconds": 5, **dimensions}])
                        self.assertEqual(assessment.get("status"), "unverified")
                        self.assertTrue(assessment.get("local_files_checked"))
                        self.assertIn("缺少实际分辨率", " ".join(notes))
                        self.assertIn("未核验", " ".join(notes))
                        if kind == "video":
                            self.assertEqual(assessment["duration"][0]["status"], "checked")

    def test_default_size_with_actual_dimensions_still_allows_checked(self):
        for kind in ("image", "video"):
            with self.subTest(kind=kind):
                assessment, notes = self.assess(kind=kind,
                    profiles=[{"path": "media.dat", "bytes": 100, "width": 1280,
                               "height": 720, "duration_seconds": 5}])
                self.assertEqual(assessment.get("status"), "checked")
                self.assertIn("平台默认", " ".join(notes))

    def test_exact_size_forms_compare_without_pixel_guessing_or_tolerance(self):
        for kind in ("image", "video"):
            for size in ({"size": "1280x720"}, {"size": "1280X720"},
                         {"size": "1280*720"}, {"image_size": "1280x720"},
                         {"image_size": {"width": 1280, "height": 720}},
                         {"width": 1280, "height": 720}):
                for width, expected in ((1280, "checked"), (1279, "mismatch")):
                    with self.subTest(kind=kind, size=size, width=width):
                        assessment, notes = self.assess(kind=kind,
                            evidence={"duration_seconds": 5, "size_status": "explicit", **size},
                            profiles=[{"path": "media.dat", "bytes": 100, "width": width,
                                       "height": 720, "duration_seconds": 5}])
                        self.assertEqual(assessment.get("status"), expected)
                        self.assertNotIn("平台默认", " ".join(notes))
                        if expected == "mismatch":
                            self.assertIn("尺寸不一致", " ".join(notes))

    def test_size_mismatch_is_not_hidden_by_unverified_ratio(self):
        assessment, notes = self.assess(kind="image",
            evidence={"size": "1280x720", "ratio": "16:9", "size_status": "explicit"},
            profiles=[{"path": "a.png", "bytes": 100, "width": 1279, "height": 720}])
        self.assertEqual(assessment.get("status"), "mismatch")
        self.assertIn("尺寸不一致", " ".join(notes))
        self.assertIn("未核验", " ".join(notes))

    def test_duration_and_watermark_mismatch_survive_unverified_dimensions(self):
        for duration, meta in ((5.19, {}), (5, {"watermark": True}),
                               (5.19, {"watermark": True})):
            with self.subTest(duration=duration, meta=meta):
                assessment, notes = self.assess(meta=meta,
                    evidence={"duration_seconds": 5, "size": "2k", "size_status": "explicit"},
                    profiles=[{"path": "v.mp4", "bytes": 100, "duration_seconds": duration}])
                self.assertEqual(assessment.get("status"), "mismatch")
                self.assertIn("缺少实际分辨率", " ".join(notes))
                self.assertIn("未核验", " ".join(notes))
                if duration != 5:
                    self.assertEqual(assessment["duration"][0]["status"], "mismatch")
                    self.assertIn("5.19", " ".join(notes))
                if meta:
                    self.assertEqual(assessment.get("watermark"), "present")
                    self.assertIn("带水印", " ".join(notes))

    def test_audio_assessment_does_not_require_image_dimensions(self):
        assessment, _ = self.assess(kind="audio", evidence={},
            profiles=[{"path": "a.wav", "bytes": 100, "duration_seconds": 5, "format": "wav"}])
        self.assertEqual(assessment.get("status"), "checked")

    def test_existing_metadata_helpers_feed_assessment_without_modifying_media(self):
        with tempfile.TemporaryDirectory(prefix="mr-outcomes-") as temp:
            path = Path(temp) / "synthetic.mp4"
            def box(kind, data):
                return struct.pack(">I", len(data) + 8) + kind + data
            mvhd = bytes(12) + struct.pack(">II", 1000, 5194) + bytes(80)
            path.write_bytes(box(b"ftyp", b"isom0000") + box(b"moov", box(b"mvhd", mvhd)))
            before = path.read_bytes()
            profiles = profile_files([str(path)], "video")
            self.assertEqual(profiles[0].get("duration_seconds"), 5.19)
            assessment, _ = self.assess(profiles=profiles)
            self.assertEqual(assessment.get("status"), "mismatch")
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
