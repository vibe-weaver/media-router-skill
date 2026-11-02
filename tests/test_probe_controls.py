#!/usr/bin/env python3
"""网页语音测试：直接执行真实业务逻辑，不启动服务、不联网、不读个人配置。"""
import io
import json
import sys
import tempfile
import threading
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from mrouter import adapters, config, probe, transport, webserver
from mrouter.adapters import DownloadError, GenResult
from mrouter.config import ModelSpec, Pool, RouterConfig
from mrouter.lifecycle import GenerationInterrupted


class ProbeControlsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mr-probe-controls-")
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.safety = ExitStack()
        self.addCleanup(self.safety.close)
        self.safety.enter_context(patch.dict("os.environ", {"MR_TTS_TEST": "offline-fixture-key"}))
        for name in ("socket.create_connection", "socket.socket.connect", "urllib.request.urlopen"):
            self.safety.enter_context(patch(name, side_effect=AssertionError("外网/服务访问已禁止")))
        self.spec = ModelSpec("tts", "dashscope", "audio", model="configured-tts",
                              api_key_env="MR_TTS_TEST", supports=["text2audio", "audio2audio"])
        self.cfg = RouterConfig(root, root, root / "state", root / "models.yaml",
                                {"output_dir": str(root / "outputs")},
                                {"audio": Pool("audio", "priority_then_weight", [self.spec])},
                                {"keys": {"MR_TTS_TEST": "test-only-key"}}, "yaml")

    def test_audio_default_is_short_text_to_audio_without_clone(self):
        seen = []
        before = (dict(self.spec.params), dict(self.spec.options))
        def adapter(spec, req, cfg, ctx):
            seen.append(req)
            return GenResult("ok", spec.id, spec.provider, meta={"kind": "audio"})
        with patch.object(probe, "run_adapter", side_effect=adapter):
            result = probe.deep_probe(self.cfg, self.spec)
        self.assertTrue(result["ok"])
        self.assertEqual(seen[0].requires, ["text2audio"])
        self.assertEqual(seen[0].images, [])
        self.assertIn("你好", seen[0].prompt)
        self.assertLess(len(seen[0].prompt), 100)
        self.assertEqual((self.spec.params, self.spec.options), before)
        self.assertGreaterEqual(result["elapsed_ms"], 0)
        self.assertIn("assessment", result)
        self.assertIn("profiles", result)

    def test_engine_error_not_reported_as_certain_parameter_failure(self):
        exc = transport.HttpError("HTTP 400", 400, json.dumps({"code": "InvalidParameter",
                                  "message": "[cosyvoice:]Engine error [411]: TTS speak operation failed"}))
        with patch.object(probe, "run_adapter", side_effect=exc):
            result = probe.deep_probe(self.cfg, self.spec)
        self.assertFalse(result["ok"])
        self.assertIn("411", result["error"])
        self.assertIn("根因待确认", result["hint"])
        self.assertEqual(result["diagnostic"]["category"], "engine")
        self.assertGreaterEqual(result["elapsed_ms"], 0)

    def test_quota_code_is_not_called_rate_limit(self):
        exc = transport.HttpError("HTTP 429", 429, '{"error":{"code":"1113","message":"余额不足或无可用资源包"}}')
        with patch.object(probe, "run_adapter", side_effect=exc):
            result = probe.deep_probe(self.cfg, self.spec)
        self.assertFalse(result["ok"])
        self.assertIn("资源包", result["hint"])
        self.assertNotIn("429 是限流", result["hint"])

    def test_light_probe_quota_rejection_is_not_rate_limit_or_success(self):
        target = probe.ProbeTarget(provider="openai", api_key="test-only-key")
        from mrouter import catalog
        exc = transport.HttpError("HTTP 429", 429, '{"error":{"code":"1113","message":"quota unavailable"}}')
        with patch.object(probe, "request", side_effect=exc):
            check, models = probe._check_auth(target, catalog.get("openai"), "test-only-key", 1)
        self.assertEqual(check.status, probe.FAIL)
        self.assertIn("资源包", check.detail)
        self.assertIn("1113", check.detail)
        self.assertNotIn("限流", check.detail)
        self.assertEqual(models, [])

    def test_generated_media_survives_profile_or_assessment_errors(self):
        file = self.cfg.output_dir / "tts.wav"
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"RIFF-offline-fixture")
        for stage in ("profile_files", "assess_result"):
            with self.subTest(stage=stage):
                generated = GenResult("ok", self.spec.id, self.spec.provider,
                                      files=[str(file)], urls=["https://stub.invalid/tts.wav"])
                with patch.object(probe, "run_adapter", return_value=generated) as adapter, \
                     patch.object(probe, stage, side_effect=RuntimeError("metadata unavailable https://stub.invalid/?token=private")):
                    result = probe.deep_probe(self.cfg, self.spec, fail_fast=True)
                self.assertTrue(result["ok"])
                self.assertEqual(result["files"], [str(file)])
                self.assertEqual(result["urls"], generated.urls)
                self.assertEqual(result["profiles"][0]["path"], str(file))
                self.assertEqual(result["assessment"]["status"], "unverified")
                self.assertIn("未核验", " ".join(result["notes"]))
                self.assertNotIn("private", " ".join(result["notes"]))
                self.assertEqual(adapter.call_count, 1)
                self.assertFalse(transport.is_fail_fast())

    @contextmanager
    def model_api(self, error=None):
        api = webserver.ConfigApi("127.0.0.1")
        with ExitStack() as stack:
            stack.enter_context(patch.object(api, "_config", return_value=self.cfg))
            stack.enter_context(patch.object(api, "_resolve_model_for_test", return_value=(
                {"provider": "dashscope", "model_id": "tts"}, "audio", "configured-tts")))
            stack.enter_context(patch.object(api, "_target_from_payload", return_value=probe.ProbeTarget(provider="dashscope")))
            stack.enter_context(patch.object(webserver.store, "load_overlay", return_value=SimpleNamespace(vendors=[], base_vendors=lambda: [])))
            stack.enter_context(patch.object(webserver.config, "build_spec_from_entry", return_value=self.spec))
            light = stack.enter_context(patch.object(probe, "probe_target", return_value=probe.ProbeResult(True)))
            adapter = stack.enter_context(patch.object(probe, "run_adapter", side_effect=error,
                return_value=GenResult("ok", "tts", "dashscope")))
            yield api, light, adapter

    def test_model_api_download_error_keeps_all_recovery_without_regeneration(self):
        files = [str(self.cfg.output_dir / "完整子项.wav")]
        urls = ["https://stub.invalid/one.wav?Signature=recover-one", "https://stub.invalid/two.wav?token=recover-two"]
        exc = DownloadError("下载失败 https://stub.invalid/?token=error-private", urls, files, 503)
        with self.model_api(exc) as (api, light, adapter):
            result = api.model_test({}, {"id": "tts", "deep": True})
        self.assertEqual(adapter.call_count, 1)
        self.assertEqual(light.call_count, 1)
        self.assertFalse(result["ok"])
        real = result["real_call"]
        self.assertFalse(real["ok"])
        self.assertEqual(real.get("files"), files)
        self.assertEqual(real.get("urls"), urls)
        self.assertEqual(real.get("phase"), "delivery")
        self.assertIs(real.get("submission_uncertain"), False)
        self.assertIn("不要", real["hint"])
        self.assertIn("生成", real["hint"])
        self.assertNotIn("error-private", json.dumps([real["error"], real["hint"], real["diagnostic"]]))
        self.assertGreaterEqual(real["elapsed_ms"], 0)

    def test_model_api_lifecycle_error_keeps_tasks_phase_and_stop_hint(self):
        exc = GenerationInterrupted("任务查询失败 https://stub.invalid/?token=error-private",
            files=[str(self.cfg.output_dir / "earlier.wav")],
            urls=["https://stub.invalid/earlier.wav?Signature=usable-signature"],
            tasks=[{"task_id": "task-1", "query_url": "https://stub.invalid/tasks/task-1?token=task-private", "status": "pending"}],
            phase="polling", submission_uncertain=True)
        exc.hint = "保留已有任务，不要再次生成；https://stub.invalid/?token=hint-private"
        with self.model_api(exc) as (api, _, adapter):
            result = api.model_test({}, {"id": "tts", "deep": True})
        self.assertEqual(adapter.call_count, 1)
        self.assertFalse(result["ok"])
        real = result["real_call"]
        self.assertEqual(real.get("files"), exc.files)
        self.assertEqual(real.get("urls"), exc.urls)
        self.assertEqual(real.get("phase"), "polling")
        self.assertIs(real.get("submission_uncertain"), True)
        self.assertEqual(real.get("tasks", [{}])[0].get("task_id"), "task-1")
        self.assertIn("保留已有任务", real["hint"])
        recovery_text = json.dumps([real.get("tasks"), real["error"], real["hint"], real["diagnostic"]])
        for secret in ("task-private", "error-private", "hint-private"):
            self.assertNotIn(secret, recovery_text)

    def test_real_probe_adapter_transport_chain_keeps_delivery_recovery(self):
        self.spec.provider = "openai"
        self.spec.endpoint = "https://stub.invalid/v1/audio/speech"
        calls = []
        # 二进制语音在目录创建失败前已返回；网络只走真实 transport 的硬边界 fixture。
        class Response(io.BytesIO):
            status = 200
            headers = {"Content-Type": "audio/wav"}
        def respond(req, timeout):
            calls.append((req.get_method(), req.full_url))
            return Response(b"RIFF-offline-generated-audio")
        with self.model_api() as (api, _, _):
            with patch.object(probe, "run_adapter", adapters.run_adapter), \
                 patch.object(transport, "open_request", side_effect=respond), \
                 patch.object(Path, "mkdir", side_effect=OSError("offline delivery denied")):
                result = api.model_test({}, {"id": "tts", "deep": True})
        self.assertEqual(calls, [("POST", "https://stub.invalid/v1/audio/speech")])
        self.assertFalse(result["ok"])
        self.assertEqual(result["real_call"]["files"], [])
        self.assertEqual(result["real_call"]["urls"], [])
        self.assertEqual(result["real_call"]["phase"], "delivery")
        self.assertIn("不要", result["real_call"]["hint"])

    def test_real_probe_adapter_transport_chain_keeps_uncertain_task(self):
        self.spec.provider = "openai"
        self.spec.kind = "video"
        self.spec.endpoint = "https://stub.invalid/videos"
        calls = []
        class Response(io.BytesIO):
            status = 200
            headers = {"Content-Type": "application/json"}
        def respond(req, timeout):
            calls.append((req.get_method(), req.full_url))
            if req.get_method() == "POST":
                return Response(b'{"id":"offline-task"}')
            raise transport.HttpError("offline query failure", 503)
        with self.model_api() as (api, _, _):
            with patch.object(probe, "run_adapter", adapters.run_adapter), \
                 patch.object(transport, "open_request", side_effect=respond), \
                 patch.object(transport.time, "sleep"):
                result = api.model_test({}, {"id": "tts", "deep": True})
        real = result["real_call"]
        self.assertFalse(result["ok"])
        self.assertEqual(len([method for method, _ in calls if method == "POST"]), 1)
        self.assertEqual(real["tasks"][0]["task_id"], "offline-task")
        self.assertIs(real["submission_uncertain"], True)
        self.assertIn("不要", real["hint"])

    def test_recovery_field_helper_ignores_non_contract_shapes(self):
        exc = transport.HttpError("invalid recovery fixture")
        exc.stop_generation = True
        exc.files = "not-a-file-list"
        exc.urls = {"not": "a-url-list"}
        exc.tasks = "not-a-task-list"
        exc.phase = ["not-a-phase"]
        exc.hint = {"not": "a-hint"}
        with patch.object(probe, "run_adapter", side_effect=exc) as adapter:
            result = probe.deep_probe(self.cfg, self.spec)
        self.assertEqual(adapter.call_count, 1)
        self.assertFalse(result["ok"])
        self.assertNotIn("files", result)
        self.assertNotIn("urls", result)
        self.assertNotIn("tasks", result)
        self.assertNotIn("phase", result)
        self.assertIsInstance(result["hint"], str)

    def test_deep_rejects_each_non_boolean_json_value_before_preflight(self):
        for value in ("false", "0", 1, 0, [], {}, None):
            with self.subTest(value=value):
                with self.model_api() as (api, light, adapter):
                    with self.assertRaises(webserver.ApiError) as caught:
                        api.model_test({}, {"id": "tts", "deep": value})
                    self.assertEqual(caught.exception.status, 400)
                    self.assertIn("deep", str(caught.exception))
                    light.assert_not_called()
                    adapter.assert_not_called()

    def test_deep_default_and_json_false_are_free_only_json_true_generates(self):
        for payload in ({"id": "tts"}, {"id": "tts", "deep": False}, {"id": "tts", "deep": True}):
            with self.subTest(payload=payload):
                with self.model_api() as (api, light, adapter):
                    result = api.model_test({}, payload)
                self.assertTrue(result["ok"])
                self.assertEqual(light.call_count, 1)
                self.assertEqual(adapter.call_count, 1 if payload.get("deep") is True else 0)
                self.assertEqual("real_call" in result, payload.get("deep") is True)

    def test_failed_result_has_elapsed_time(self):
        with patch.object(probe, "run_adapter", return_value=GenResult("error", "tts", "dashscope", error="no output")):
            result = probe.deep_probe(self.cfg, self.spec)
        self.assertFalse(result["ok"])
        self.assertGreaterEqual(result["elapsed_ms"], 0)

    def test_strict_direct_probe_native_does_not_delegate(self):
        native = ModelSpec("native", "native", "image")
        with patch.object(probe, "run_adapter") as adapter:
            result = probe.deep_probe(self.cfg, native, fail_fast=True)
        self.assertFalse(result["ok"])
        self.assertIn("严格", result["error"])
        adapter.assert_not_called()

    def test_model_api_policy_covers_preflight_and_generation(self):
        api = webserver.ConfigApi("127.0.0.1")
        seen = []
        def light(*args, **kwargs):
            seen.append(("light", transport.is_fail_fast()))
            return probe.ProbeResult(True, summary="key reachable, model unverified")
        def deep(*args, **kwargs):
            seen.append(("deep", transport.is_fail_fast()))
            return {"ok": True, "files": [], "elapsed_ms": 1}
        with ExitStack() as stack:
            stack.enter_context(patch.object(api, "_config", return_value=self.cfg))
            stack.enter_context(patch.object(api, "_resolve_model_for_test", return_value=({"provider": "dashscope", "model_id": "tts"}, "audio", "configured-tts")))
            stack.enter_context(patch.object(api, "_target_from_payload", return_value=probe.ProbeTarget(provider="dashscope")))
            stack.enter_context(patch.object(webserver.store, "load_overlay", return_value=SimpleNamespace(vendors=[], base_vendors=lambda: [])))
            stack.enter_context(patch.object(webserver.config, "build_spec_from_entry", return_value=self.spec))
            stack.enter_context(patch.object(probe, "probe_target", side_effect=light))
            stack.enter_context(patch.object(probe, "deep_probe", side_effect=deep))
            result = api.model_test({}, {"id": "tts", "deep": True, "fail_fast": True})
        self.assertTrue(result["ok"])
        self.assertEqual(seen, [("light", True), ("deep", True)])
        self.assertGreaterEqual(result["elapsed_ms"], 0)
        self.assertFalse(transport.is_fail_fast())

    def test_two_model_api_request_threads_keep_independent_policy(self):
        api = webserver.ConfigApi("127.0.0.1")
        barrier = threading.Barrier(2, timeout=5)
        observations = {"strict": [], "default": []}
        errors = []
        def observe(*args, **kwargs):
            name = threading.current_thread().name
            observations[name].append(transport.is_fail_fast())
            barrier.wait()
        def light(*args, **kwargs):
            observe()
            return probe.ProbeResult(True)
        def deep(*args, **kwargs):
            observe()
            return {"ok": True, "files": []}
        def invoke(strict):
            try:
                result = api.model_test({}, {"id": "tts", "deep": True, "fail_fast": strict})
                self.assertTrue(result["ok"])
                self.assertFalse(transport.is_fail_fast())
            except BaseException as exc:
                errors.append(exc)
                barrier.abort()
        with ExitStack() as stack:
            stack.enter_context(patch.object(api, "_config", return_value=self.cfg))
            stack.enter_context(patch.object(api, "_resolve_model_for_test", return_value=({"provider": "dashscope", "model_id": "tts"}, "audio", "configured-tts")))
            stack.enter_context(patch.object(api, "_target_from_payload", return_value=probe.ProbeTarget(provider="dashscope")))
            stack.enter_context(patch.object(webserver.store, "load_overlay", return_value=SimpleNamespace(vendors=[], base_vendors=lambda: [])))
            stack.enter_context(patch.object(webserver.config, "build_spec_from_entry", return_value=self.spec))
            stack.enter_context(patch.object(probe, "probe_target", side_effect=light))
            stack.enter_context(patch.object(probe, "deep_probe", side_effect=deep))
            threads = [threading.Thread(target=invoke, args=(strict,), name=name)
                       for name, strict in (("strict", True), ("default", False))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
            self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(observations, {"strict": [True, True], "default": [False, False]})
        self.assertFalse(transport.is_fail_fast())

    def test_strict_light_probe_stops_after_first_failed_check(self):
        target = probe.ProbeTarget(provider="dashscope", api_key="test-only-key")
        with patch.object(probe, "_check_reachable", return_value=probe.Check("接口可达", probe.FAIL, "offline")), \
             patch.object(probe, "_check_auth", return_value=(probe.Check("密钥校验", probe.FAIL, "offline"), [])) as auth:
            result = probe.probe_target(self.cfg, target, kind="audio", model_name="configured-tts", fail_fast=True)
        self.assertFalse(result.ok)
        auth.assert_not_called()
        self.assertGreaterEqual(result.elapsed_ms, 0)

    def test_vendor_api_policy_is_local_and_not_persisted(self):
        api = webserver.ConfigApi("127.0.0.1")
        seen = []
        target = probe.ProbeTarget(provider="dashscope")
        def light(*args, **kwargs):
            seen.append(transport.is_fail_fast())
            return probe.ProbeResult(True)
        with patch.object(api, "_config", return_value=self.cfg), \
             patch.object(api, "_target_from_payload", return_value=target), \
             patch.object(probe, "probe_target", side_effect=light), \
             patch.object(webserver.store, "save_with") as save:
            api.vendor_test({}, {"kind": "audio", "fail_fast": True})
        self.assertEqual(seen, [True])
        self.assertFalse(transport.is_fail_fast())
        save.assert_not_called()

    def test_failed_preflight_never_generates(self):
        api = webserver.ConfigApi("127.0.0.1")
        with patch.object(api, "_config", return_value=self.cfg), \
             patch.object(api, "_resolve_model_for_test", return_value=({"provider": "dashscope"}, "audio", "configured-tts")), \
             patch.object(api, "_target_from_payload", return_value=probe.ProbeTarget(provider="dashscope")), \
             patch.object(probe, "probe_target", return_value=probe.ProbeResult(False, summary="unreachable")), \
             patch.object(probe, "deep_probe") as deep:
            result = api.model_test({}, {"id": "tts", "deep": True})
        self.assertFalse(result["ok"])
        self.assertFalse(result["real_call"]["ok"])
        deep.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
