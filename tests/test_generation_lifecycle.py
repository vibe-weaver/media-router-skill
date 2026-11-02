#!/usr/bin/env python3
"""真实 CLI/适配器/传输链的离线计费安全回归；配置与媒体仅用临时目录。"""
import base64
import io
import json
import socket
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from http.client import BadStatusLine
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from mrouter import adapters, cli, transport
from mrouter.config import ModelSpec, Pool, RouterConfig
from mrouter.health import HealthStore

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 40
WAV = b"RIFF" + b"\x00" * 4 + b"WAVEfmt " + b"\x00" * 24
URL = "https://cdn.invalid/first.png?Signature=private-sign"


class Response(io.BytesIO):
    status = 200
    headers = {"Content-Type": "application/json"}


def response(payload):
    return Response(payload if isinstance(payload, bytes) else json.dumps(payload).encode())


def rejection(req, status=400):
    return urllib.error.HTTPError(req.full_url, status, "offline rejection", {},
                                  io.BytesIO(b'{"code":"InvalidParameter","message":"input.text must not be empty"}'))


class GenerationLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mr-lifecycle-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(socket.socket, "connect", side_effect=AssertionError("no external network")))
        self.stack.enter_context(patch.object(transport, "open_request", side_effect=AssertionError("missing offline fixture")))
        self.stack.enter_context(patch.object(transport.time, "sleep"))

    def invoke(self, respond, *, kind="image", provider="openai", count=1,
               strict=False, download=False, params=None, options=None, patches=()):
        self.models = [ModelSpec(mid, provider, kind, model="qwen-image-test" if provider == "dashscope" and kind == "image" else "offline-model",
                                endpoint="https://" + mid + ".invalid/submit",
                                api_key_env="MR_LIFECYCLE", params=params or {}, options=options or {})
                       for mid in ("m1", "m2")]
        self.models.append(ModelSpec("native", "native", kind))
        run_id = str(getattr(self, "run_id", 0) + 1)
        self.run_id = int(run_id)
        self.cfg = RouterConfig(self.root, self.root, self.root / "state" / run_id, self.root / "models.yaml",
                                {"output_dir": str(self.root / "outputs"), "caption": {"mode": "none"},
                                 "max_poll_seconds": 0},
                                {kind: Pool(kind, "priority_then_weight", self.models)},
                                {"keys": {"MR_LIFECYCLE": "offline-key"}}, "yaml")
        calls = []
        def open_offline(req, timeout):
            calls.append((req.get_method(), req.full_url))
            if urllib.parse.urlsplit(req.full_url).hostname == "m2.invalid":
                if kind == "audio":
                    return response(WAV if provider == "openai" else {"output": {"audio": {"url": URL}}})
                if provider == "openai" and kind == "video":
                    return response({"task_status": "SUCCESS", "data": [{"url": URL}]})
                if provider == "dashscope" and kind == "image":
                    return response({"output": {"choices": [{"message": {"content": [{"image": URL}]}}]}})
                return response({"data": [{"url": URL}]})
            if urllib.parse.urlsplit(req.full_url).hostname == "cdn.invalid":
                return response(PNG)
            return respond(req, timeout)
        argv = ["generate", "--kind", kind, "--prompt", "offline", "--count", str(count), "--max-attempts", "2"]
        if not download:
            argv.append("--no-download")
        if strict:
            argv.append("--fail-fast")
        args = cli.build_parser().parse_args(argv)
        output = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch.object(cli, "load_config", return_value=self.cfg))
            stack.enter_context(patch.object(cli, "_resolve", return_value=(self.models, {})))
            stack.enter_context(patch.object(transport, "open_request", side_effect=open_offline))
            for target in patches:
                stack.enter_context(target)
            stack.enter_context(redirect_stdout(output))
            stack.enter_context(redirect_stderr(io.StringIO()))
            rc = cli.cmd_generate(args)
        return rc, json.loads(output.getvalue()), calls

    def assert_stopped(self, result, calls):
        self.assertEqual(result["status"], "error")
        self.assertEqual([urllib.parse.urlsplit(url).hostname for method, url in calls if method == "POST"],
                         ["m1.invalid"])
        self.assertNotIn("delegate", result)
        self.assertFalse(HealthStore(self.cfg.health_path).state("m1").get("failure_count"))
        self.assertIn("不要", result["hint"])

    def test_accepted_task_query_failure_never_recreates_generation(self):
        for strict in (False, True):
            for failure in ("http", "json", "timeout", "missing-output", "protocol"):
                with self.subTest(strict=strict, failure=failure):
                    def respond(req, timeout):
                        if req.get_method() == "POST":
                            return response({"id": "accepted-task"})
                        if failure == "http":
                            raise rejection(req, 503)
                        if failure == "timeout":
                            raise TimeoutError("query timeout")
                        if failure == "protocol":
                            raise BadStatusLine("invalid query status")
                        return response(b"<html>bad query</html>" if failure == "json" else {"task_status": "SUCCESS"})
                    rc, result, calls = self.invoke(respond, kind="video", strict=strict)
                    self.assertEqual(rc, 1)
                    self.assert_stopped(result, calls)
                    self.assertTrue(result["submission_uncertain"])
                    self.assertEqual(result["tasks"][0]["task_id"], "accepted-task")

    def test_semantically_incomplete_successful_submission_stops(self):
        for strict in (False, True):
            for payload in (None, {}, [], {"request_id": "accepted", "status": "processing"}, 42):
                with self.subTest(strict=strict, payload=payload):
                    rc, result, calls = self.invoke(lambda req, timeout: response(payload), strict=strict)
                    self.assertEqual(rc, 1)
                    self.assert_stopped(result, calls)
                    self.assertTrue(result["submission_uncertain"])

    def test_final_download_rename_error_is_delivery_not_model_failure(self):
        replace = Path.replace
        def fail_part(path, target):
            if path.suffix == ".part":
                raise PermissionError("offline final rename denied")
            return replace(path, target)
        rc, result, calls = self.invoke(lambda req, timeout: response({"data": [{"url": URL}]}),
                                        download=True, patches=[patch.object(Path, "replace", fail_part)])
        self.assertEqual(rc, 1)
        self.assert_stopped(result, calls)
        self.assertEqual(result.get("urls", []), [URL])
        self.assertEqual(result["files"], [])
        self.assertEqual(list((self.root / "outputs").rglob("*.part")), [])
        self.assertNotIn("private-sign", result["error"])

    def test_binary_audio_disk_error_stops_after_submission(self):
        real = Path.write_bytes
        def denied(path, data):
            if "outputs" in path.parts:
                raise OSError("offline disk full")
            return real(path, data)
        rc, result, calls = self.invoke(lambda req, timeout: response(WAV), kind="audio", download=True,
                                        patches=[patch.object(Path, "write_bytes", denied)])
        self.assertEqual(rc, 1)
        self.assert_stopped(result, calls)
        self.assertFalse(result["submission_uncertain"])

    def test_inline_write_failure_stops_and_preserves_complete_earlier_file(self):
        real = Path.write_bytes
        writes = []
        def fail_second(path, data):
            if "outputs" in path.parts:
                writes.append(path)
                if len(writes) == 2:
                    raise OSError("offline second file full")
            return real(path, data)
        payload = {"data": [{"b64_json": base64.b64encode(PNG).decode()}] * 2}
        rc, result, calls = self.invoke(lambda req, timeout: response(payload), download=True,
                                        patches=[patch.object(Path, "write_bytes", fail_second)])
        self.assertEqual(rc, 1)
        self.assert_stopped(result, calls)
        self.assertEqual(len(result["files"]), 1)
        self.assertTrue(Path(result["files"][0]).is_file())

    def test_partial_audio_batch_keeps_urls_and_does_not_regenerate_batch(self):
        for strict in (False, True):
            for status in (400, 503):
                with self.subTest(strict=strict, status=status):
                    count = [0]
                    def respond(req, timeout):
                        count[0] += 1
                        if count[0] == 2:
                            raise rejection(req, status)
                        return response({"output": {"audio": {"url": URL}}})
                    rc, result, calls = self.invoke(respond, provider="dashscope", kind="audio", count=2, strict=strict)
                    self.assertEqual(rc, 1)
                    self.assertEqual([urllib.parse.urlsplit(url).hostname for method, url in calls if method == "POST"],
                                     ["m1.invalid", "m1.invalid"])
                    self.assertEqual(result.get("urls", []), [URL])
                    self.assertIn("不要", result["hint"])
                    self.assertFalse(HealthStore(self.cfg.health_path).state("m1").get("failure_count"))

    def test_partial_binary_audio_batch_keeps_first_file(self):
        count = [0]
        def respond(req, timeout):
            count[0] += 1
            if count[0] == 2:
                raise rejection(req)
            return response(WAV)
        rc, result, calls = self.invoke(respond, kind="audio", count=2, download=True)
        self.assertEqual(rc, 1)
        self.assertEqual(len([method for method, url in calls if method == "POST"]), 2)
        self.assertEqual(len(result["files"]), 1)
        self.assertEqual(Path(result["files"][0]).read_bytes(), WAV)

    def test_inline_no_download_does_not_fall_back(self):
        for provider in ("openai", "volcengine"):
            for strict in (False, True):
                with self.subTest(provider=provider, strict=strict):
                    payload = {"data": [{"b64_json": base64.b64encode(PNG).decode()}]}
                    rc, result, calls = self.invoke(lambda req, timeout: response(payload), provider=provider, strict=strict)
                    self.assertEqual(rc, 1)
                    self.assert_stopped(result, calls)
                    self.assertFalse(result["submission_uncertain"])
                    self.assertIn("--no-download", result["error"])
                    self.assertNotIn(base64.b64encode(PNG).decode(), json.dumps(result))

    def test_accepted_query_failure_is_safe_for_each_async_provider(self):
        cases = (
            ("openai", {"id": "accepted-task"}, {}),
            ("dashscope", {"output": {"task_id": "accepted-task"}}, {}),
            ("volcengine", {"id": "accepted-task"}, {}),
            ("kling", {"code": 0, "data": {"task_id": "accepted-task"}}, {}),
            ("replicate", {"id": "accepted-task", "status": "processing", "urls": {"get": "https://m1.invalid/query"}}, {}),
            ("fal", {"request_id": "accepted-task", "status_url": "https://m1.invalid/query", "response_url": "https://m1.invalid/result"}, {}),
            ("generic_http", {"id": "accepted-task"}, {"submit": {"task_id_path": "id"}, "poll": {"url": "https://m1.invalid/query/{{task_id}}", "status_path": "status"}}),
        )
        for provider, submitted, options in cases:
            with self.subTest(provider=provider):
                def respond(req, timeout):
                    if req.get_method() == "POST":
                        return response(submitted)
                    raise rejection(req, 503)
                rc, result, calls = self.invoke(respond, provider=provider, kind="video", options=options)
                self.assertEqual(rc, 1)
                self.assert_stopped(result, calls)
                self.assertEqual(result["tasks"][0]["task_id"], "accepted-task")

    def test_unknown_200_error_is_not_proof_of_rejection(self):
        for provider, payload in (
                ("openai", {"error": {"code": "InternalServerError", "message": "backend unavailable"}}),
                ("kling", {"code": 999999, "message": "backend unavailable"})):
            with self.subTest(provider=provider):
                rc, result, calls = self.invoke(lambda req, timeout: response(payload), provider=provider)
                self.assertEqual(rc, 1)
                self.assert_stopped(result, calls)
                self.assertTrue(result["submission_uncertain"])

    def test_download_failure_after_task_keeps_recovery_task_and_flags(self):
        def respond(req, timeout):
            return response({"id": "accepted-task"} if req.get_method() == "POST" else
                            {"task_status": "SUCCESS", "data": [{"url": URL}]})
        rc, result, calls = self.invoke(respond, kind="video", download=True,
                                        patches=[patch.object(adapters, "download", side_effect=transport.HttpError("offline CDN failure"))])
        self.assertEqual(rc, 1)
        self.assert_stopped(result, calls)
        self.assertEqual(result.get("tasks", [{}])[0].get("task_id"), "accepted-task")
        self.assertFalse(result.get("submission_uncertain", True))
        self.assertEqual(result.get("phase"), "delivery")

    def test_failed_tasks_with_partial_output_are_not_safe_to_regenerate(self):
        for provider, kind, submitted, failed, options, params in (
                ("openai", "video", {"id": "partial-task"}, {"task_status": "FAILED", "data": [{"url": URL}]}, {}, {}),
                ("dashscope", "video", {"output": {"task_id": "partial-task"}}, {"output": {"task_status": "FAILED", "results": [{"url": URL}]}}, {}, {}),
                ("volcengine", "video", {"id": "partial-task"}, {"status": "failed", "content": {"video_url": URL}}, {}, {}),
                ("kling", "video", {"code": 0, "data": {"task_id": "partial-task"}}, {"data": {"task_status": "failed", "task_result": {"videos": [{"url": URL}]}}}, {}, {}),
                ("kling", "audio", {"code": 0, "data": {"task_id": "partial-task"}}, {"data": {"task_status": "failed", "task_result": {"audios": [{"url": URL}]}}}, {}, {"voice_id": "offline"}),
                ("replicate", "video", {"id": "partial-task", "status": "processing", "urls": {"get": "https://m1.invalid/query"}}, {"status": "failed", "output": [URL]}, {}, {}),
                ("fal", "video", {"request_id": "partial-task", "status_url": "https://m1.invalid/query", "response_url": "https://m1.invalid/result"}, {"status": "FAILED", "images": [{"url": URL}]}, {}, {}),
                ("generic_http", "video", {"id": "partial-task"}, {"status": "failed", "result": [{"url": URL}]}, {"submit": {"task_id_path": "id"}, "poll": {"url": "https://m1.invalid/query/{{task_id}}", "status_path": "status", "result_path": "result"}}, {})):
            with self.subTest(provider=provider, kind=kind):
                rc, result, calls = self.invoke(lambda req, timeout: response(submitted if req.get_method() == "POST" else failed),
                                                provider=provider, kind=kind, options=options, params=params)
                self.assertEqual(rc, 1)
                self.assert_stopped(result, calls)
                self.assertEqual(result.get("urls", []), [URL])
                self.assertFalse(result["submission_uncertain"])
                self.assertEqual(result["tasks"][0]["status"], "failed")
                self.assertEqual(result["phase"], "partial")

    def test_failed_dashscope_task_keeps_existing_single_url_fields(self):
        for field in ("video_url", "url"):
            with self.subTest(field=field):
                rc, result, calls = self.invoke(lambda req, timeout: response(
                    {"output": {"task_id": "partial-task"}} if req.get_method() == "POST" else
                    {"output": {"task_status": "FAILED", field: URL}}), provider="dashscope", kind="video")
                self.assert_stopped(result, calls)
                self.assertEqual(result.get("urls", []), [URL])

    def test_failed_replicate_task_with_output_stops_and_keeps_partial_output(self):
        payload = {"id": "partial-task", "status": "failed", "error": "last item failed", "output": [URL],
                   "urls": {"get": "https://m1.invalid/tasks/partial-task"}}
        rc, result, calls = self.invoke(lambda req, timeout: response(payload), provider="replicate")
        self.assertEqual(rc, 1)
        self.assert_stopped(result, calls)
        self.assertEqual(result.get("urls", []), [URL])
        self.assertFalse(result["submission_uncertain"])

    def test_binary_speech_preserves_explicit_json_rejection_evidence(self):
        payload = {"error": {"code": "InvalidApiKey", "message": "invalid key"}}
        rc, result, calls = self.invoke(lambda req, timeout: response(payload), kind="audio", download=True, strict=True)
        self.assertEqual(rc, 1)
        self.assert_stopped(result, calls)
        self.assertFalse(result["submission_uncertain"])
        self.assertEqual(result["diagnostic"]["error_code"], "InvalidApiKey")

    def test_known_task_ids_survive_incomplete_success_responses(self):
        for provider, payload in (
                ("replicate", {"id": "accepted-task", "status": "succeeded", "urls": {"get": "https://m1.invalid/tasks/accepted-task"}}),
                ("fal", {"request_id": "accepted-task", "status_url": "https://m1.invalid/query"})):
            with self.subTest(provider=provider):
                rc, result, calls = self.invoke(lambda req, timeout: response(payload), provider=provider)
                self.assertEqual(rc, 1)
                self.assert_stopped(result, calls)
                self.assertEqual([task["task_id"] for task in result.get("tasks", [])], ["accepted-task"])

    def test_explicit_generic_generation_method_is_not_replayed(self):
        for method in ("PUT", "GET"):
            with self.subTest(method=method):
                rc, result, calls = self.invoke(lambda req, timeout: (_ for _ in ()).throw(TimeoutError("submit may be accepted")),
                                                provider="generic_http", options={"submit": {"method": method}})
                self.assertEqual(rc, 1)
                self.assertEqual(calls, [(method, "https://m1.invalid/submit")])
                self.assertTrue(result["submission_uncertain"])
                self.assertFalse(HealthStore(self.cfg.health_path).state("m1").get("failure_count"))

    def test_mixed_inline_and_remote_outputs_are_both_delivered(self):
        for provider in ("openai", "volcengine"):
            with self.subTest(provider=provider):
                payload = {"data": [{"b64_json": base64.b64encode(PNG).decode()}, {"url": URL}]}
                rc, result, calls = self.invoke(lambda req, timeout: response(payload), provider=provider, download=True)
                self.assertEqual(rc, 0)
                self.assertEqual(len(result["files"]), 2)
                self.assertEqual(result["urls"], [URL])

    def test_volcengine_existing_remote_result_shapes_remain_compatible(self):
        for items in ({"url": URL}, [URL], [{"image_url": URL}], [{"download_url": URL}]):
            with self.subTest(items=items):
                rc, result, calls = self.invoke(lambda req, timeout: response({"data": items}), provider="volcengine")
                self.assertEqual(rc, 0)
                self.assertEqual(result["urls"], [URL])
                self.assertEqual(len(calls), 1)

    def test_inline_write_failure_keeps_remote_url_elsewhere_in_response(self):
        payload = {"data": [{"b64_json": base64.b64encode(PNG).decode()}, {"url": URL}]}
        rc, result, calls = self.invoke(lambda req, timeout: response(payload), download=True,
                                        patches=[patch.object(Path, "write_bytes", side_effect=OSError("offline disk full"))])
        self.assertEqual(rc, 1)
        self.assert_stopped(result, calls)
        self.assertEqual(result.get("urls", []), [URL])

    def test_mixed_inline_no_download_is_incomplete_not_success(self):
        payload = {"data": [{"b64_json": base64.b64encode(PNG).decode()}, {"url": URL}]}
        rc, result, calls = self.invoke(lambda req, timeout: response(payload))
        self.assertEqual(rc, 1)
        self.assert_stopped(result, calls)
        self.assertEqual(result["urls"], [URL])
        self.assertIn("--no-download", result["error"])

    def test_known_business_rejection_does_not_become_uncertain(self):
        for provider in ("openai", "dashscope", "volcengine", "kling", "replicate", "fal", "generic_http"):
            with self.subTest(provider=provider):
                payload = {"code": "InvalidApiKey", "message": "invalid key", "error": {"code": "InvalidApiKey", "message": "invalid key"}}
                rc, result, calls = self.invoke(lambda req, timeout: response(payload), provider=provider, strict=True)
                self.assertEqual(rc, 1)
                self.assert_stopped(result, calls)
                self.assertFalse(result["submission_uncertain"])
                self.assertEqual(result["diagnostic"]["kind"], "config")

    def test_generic_known_task_id_is_preserved_when_poll_config_is_incomplete(self):
        options = {"sync": False, "submit": {"task_id_path": "id"}}
        rc, result, calls = self.invoke(lambda req, timeout: response({"id": "accepted-task"}),
                                        provider="generic_http", options=options)
        self.assertEqual(rc, 1)
        self.assert_stopped(result, calls)
        self.assertEqual([task["task_id"] for task in result.get("tasks", [])], ["accepted-task"])

    def test_candidate_context_does_not_leak_after_failure_or_between_threads(self):
        spec = ModelSpec("local", "openai", "image")
        req = adapters.GenRequest(kind="image", prompt="offline")
        ctx = adapters.Ctx("", "offline", 1, 1, 1)
        barrier = threading.Barrier(2)
        outcomes = []
        def accepted(spec, req, cfg, ctx):
            adapters.submission_received()
            barrier.wait(timeout=2)
            raise RuntimeError("accepted-but-unresolved")
        def untouched(spec, req, cfg, ctx):
            barrier.wait(timeout=2)
            raise RuntimeError("before-submission")
        def worker(adapter):
            try:
                local_spec = ModelSpec("local", threading.current_thread().name, "image")
                adapters.run_adapter(local_spec, req, None, ctx)
            except Exception as exc:
                outcomes.append((str(exc), bool(getattr(exc, "submission_uncertain", False))))
        threads = [threading.Thread(target=worker, args=(adapter,), name="offline-" + str(i))
                   for i, adapter in enumerate((accepted, untouched))]
        with patch.dict(adapters.ADAPTERS, {"offline-0": accepted, "offline-1": untouched}), patch.dict(
                adapters.SUPPORTED_KINDS, {"offline-0": {"image"}, "offline-1": {"image"}}):
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)
                self.assertFalse(thread.is_alive())
        self.assertEqual(len(outcomes), 2)
        self.assertTrue(any("accepted-but" in message and uncertain for message, uncertain in outcomes))
        self.assertTrue(any("before-submission" in message and not uncertain for message, uncertain in outcomes))
        with patch.dict(adapters.ADAPTERS, {"openai": lambda *args: (_ for _ in ()).throw(RuntimeError("next-clean-candidate"))}):
            with self.assertRaises(RuntimeError) as caught:
                adapters.run_adapter(spec, req, None, ctx)
        self.assertFalse(getattr(caught.exception, "submission_uncertain", False))

    def test_partial_qwen_images_keep_first_url_after_second_rejection(self):
        count = [0]
        def respond(req, timeout):
            count[0] += 1
            if count[0] == 2:
                raise rejection(req)
            return response({"output": {"choices": [{"message": {"content": [{"image": URL}]}}]}})
        rc, result, calls = self.invoke(respond, provider="dashscope", count=2)
        self.assertEqual(rc, 1)
        self.assertEqual(len([method for method, url in calls if method == "POST"]), 2)
        self.assertEqual(result.get("urls", []), [URL])
        self.assertFalse(HealthStore(self.cfg.health_path).state("m1").get("failure_count"))

    def test_partial_kling_audio_keeps_first_task_when_second_task_fails(self):
        count = [0]
        def respond(req, timeout):
            if req.get_method() == "POST":
                count[0] += 1
                return response({"code": 0, "data": {"task_id": "task-" + str(count[0])}})
            return response({"data": {"task_status": "succeed", "task_result": {"audios": [{"url": URL}]}}} if count[0] == 1 else
                            {"data": {"task_status": "failed", "task_status_msg": "failed current task"}})
        rc, result, calls = self.invoke(respond, provider="kling", kind="audio", count=2, params={"voice_id": "offline"})
        self.assertEqual(rc, 1)
        self.assertEqual(len([method for method, url in calls if method == "POST"]), 2)
        self.assertEqual(result.get("urls", []), [URL])
        self.assertEqual([task["status"] for task in result["tasks"]], ["completed", "failed"])
        self.assertFalse(result["submission_uncertain"])

    def test_partial_volcengine_audio_keeps_first_file_after_second_rejection(self):
        count = [0]
        def respond(req, timeout):
            count[0] += 1
            if count[0] == 2:
                raise rejection(req)
            return response((json.dumps({"code": 0, "data": base64.b64encode(WAV).decode()}) + "\n").encode())
        rc, result, calls = self.invoke(respond, provider="volcengine", kind="audio", count=2,
                                        download=True, params={"speaker": "offline"})
        self.assertEqual(rc, 1)
        self.assertEqual(len([method for method, url in calls if method == "POST"]), 2)
        self.assertEqual(len(result.get("files", [])), 1)
        self.assertEqual(Path(result["files"][0]).read_bytes(), WAV)

    def test_explicit_first_submission_rejection_still_allows_default_fallback(self):
        rc, result, calls = self.invoke(lambda req, timeout: (_ for _ in ()).throw(rejection(req)))
        self.assertEqual((rc, result["status"]), (0, "ok"))
        self.assertEqual([urllib.parse.urlsplit(url).hostname for method, url in calls if method == "POST"],
                         ["m1.invalid", "m2.invalid"])

    def test_explicit_failed_task_without_output_still_allows_default_fallback(self):
        def respond(req, timeout):
            return response({"id": "failed-task"} if req.get_method() == "POST" else {"task_status": "FAILED", "message": "provider failed task"})
        rc, result, calls = self.invoke(respond, kind="video")
        self.assertEqual((rc, result["status"]), (0, "ok"))
        self.assertEqual([urllib.parse.urlsplit(url).hostname for method, url in calls if method == "POST"],
                         ["m1.invalid", "m2.invalid"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
