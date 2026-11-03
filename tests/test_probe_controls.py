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
        self.safety.enter_context(patch.dict("os.environ", {"MR_TTS_TEST": "offline-fixture-key"}, clear=True))
        for name in ("load_config", "load_secrets", "stub_config"):
            self.safety.enter_context(patch.object(config, name, side_effect=AssertionError("个人配置读取已禁止")))
        self.safety.enter_context(patch.object(webserver.store, "load_overlay", side_effect=AssertionError("个人配置层读取已禁止")))
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
        overlay = webserver.store.Overlay(base={"audio": {"models": [{
            "id": "tts", "provider": "dashscope", "model": "configured-tts", "api_key_env": "MR_TTS_TEST",
        }]}})
        for payload in ({"id": "tts"}, {"id": "tts", "deep": False}, {"id": "tts", "deep": True}):
            with self.subTest(payload=payload):
                with self.model_api() as (api, light, adapter), ExitStack() as stack:
                    if payload.get("deep") is not True:
                        stack.enter_context(patch.object(webserver.store, "load_overlay", return_value=overlay))
                    result = api.model_test({}, payload)
                self.assertTrue(result["ok"])
                self.assertEqual(light.call_count, 1)
                self.assertEqual(adapter.call_count, 1 if payload.get("deep") is True else 0)
                self.assertEqual("real_call" in result, payload.get("deep") is True)

    def test_deep_unsaved_form_and_unknown_id_rejected_before_preflight(self):
        payloads = [
            {"deep": True, "model": "not-saved", "vendor": "fixture", "kind": "audio"},
            {"id": "not-saved", "deep": True},
        ]
        for payload in payloads:
            with self.subTest(payload=payload), self.model_api() as (api, light, adapter), \
                 patch.object(self.cfg, "resolve_api_key") as key, \
                 patch.object(webserver.store, "save_with") as save:
                with self.assertRaises((webserver.ApiError, config.ConfigError)):
                    api.model_test({}, payload)
                light.assert_not_called()
                adapter.assert_not_called()
                key.assert_not_called()
                save.assert_not_called()

    def test_deep_id_must_be_nonempty_string_before_config_access(self):
        for value in (None, "", "  ", 1, True, [], {}):
            with self.subTest(value=value), self.model_api() as (api, light, adapter), \
                 patch.object(api, "_config", return_value=self.cfg) as load:
                with self.assertRaises(webserver.ApiError) as caught:
                    api.model_test({}, {"id": value, "deep": True})
                self.assertEqual(caught.exception.status, 400)
                load.assert_not_called()
                light.assert_not_called()
                adapter.assert_not_called()

    def test_deep_disabled_saved_model_rejected_before_preflight(self):
        self.spec.enabled = False
        with self.model_api() as (api, light, adapter), \
             patch.object(self.cfg, "resolve_api_key") as key:
            with self.assertRaises((webserver.ApiError, config.ConfigError)):
                api.model_test({}, {"id": "tts", "deep": True})
            light.assert_not_called()
            adapter.assert_not_called()
            key.assert_not_called()

    def test_deep_rejects_non_control_fields_before_config_or_key_access(self):
        fields = ("model", "model_name", "model_id", "vendor", "vendor_id", "provider", "kind", "params", "options",
                  "endpoint", "endpoints", "api_key", "api_key_env", "supports", "priority", "weight", "enabled",
                  "images", "extra", "count", "output_dir", "resource_id", "saved_model_id", "timeout")
        for name in fields:
            with self.subTest(field=name), self.model_api() as (api, light, adapter), \
                 patch.object(api, "_config", return_value=self.cfg) as load, \
                 patch.object(self.cfg, "resolve_api_key") as key:
                with self.assertRaises(webserver.ApiError) as caught:
                    api.model_test({}, {"id": "tts", "deep": True, name: "private-fixture-value"})
                self.assertEqual(caught.exception.status, 400)
                self.assertNotIn("private-fixture-value", str(caught.exception))
                load.assert_not_called()
                key.assert_not_called()
                light.assert_not_called()
                adapter.assert_not_called()

    def test_deep_saved_id_uses_canonical_spec_and_matching_preflight(self):
        vendor = config.Vendor("fixture", "openai", catalog="siliconflow",
            endpoints={"audio": "https://vendor.invalid/v1/audio/speech", "image": "https://other-account.invalid/images"},
            options={"voice": "vendor-voice"}, api_key_env="VENDOR_KEY")
        self.cfg.vendors[vendor.id] = vendor
        self.spec.vendor = vendor.id
        self.spec.provider = "openai"
        self.spec.endpoint = "https://model.invalid/v1/audio/speech"
        self.spec.options = {"voice": "saved-voice"}
        self.cfg.defaults.update(timeout_seconds=17, poll_interval_seconds=2, max_poll_seconds=7)
        observed = []
        def light(cfg, target, kind, model_name):
            observed.append((cfg, target, kind, model_name, probe.resolve_key(cfg, target)))
            return probe.ProbeResult(True)
        with self.model_api() as (api, _, adapter), \
             patch.object(api, "_resolve_model_for_test", side_effect=AssertionError("deep 不得读取表单")), \
             patch.object(webserver.store, "load_overlay", side_effect=AssertionError("deep 不得重新读配置层")), \
             patch.object(probe, "probe_target", side_effect=light), \
             patch.object(webserver.config, "build_spec_from_entry", side_effect=AssertionError("deep 不得重建spec")):
            result = api.model_test({}, {"id": "tts", "deep": True, "prompt": "合成测试短文本"})
        self.assertTrue(result["ok"])
        self.assertEqual(len(observed), 1)
        cfg, target, kind, name, key = observed[0]
        self.assertIs(cfg, self.cfg)
        self.assertEqual((kind, name), (self.spec.kind, self.spec.model))
        self.assertEqual(target.provider, self.spec.provider)
        self.assertEqual(target.catalog_key, vendor.catalog)
        self.assertEqual(target.endpoints, {"audio": self.spec.endpoint})
        self.assertEqual(target.options, self.spec.options)
        self.assertEqual(target.saved_model_id, self.spec.id)
        self.assertEqual(key, self.cfg.resolve_api_key(self.spec))
        sent_spec, req, sent_cfg, ctx = adapter.call_args[0]
        self.assertIs(sent_spec, self.spec)
        self.assertIs(sent_cfg, self.cfg)
        self.assertEqual((ctx.api_key, ctx.key_source), key)
        self.assertEqual((ctx.timeout, ctx.poll_interval, ctx.max_poll), (17, 2, 7))
        self.assertEqual((req.prompt, req.count, req.images, req.extra), ("合成测试短文本", 1, [], {}))
        self.assertEqual(req.output_dir, self.cfg.output_dir / "_connectivity_test")

    def test_direct_deep_probe_rejects_unregistered_or_disabled_spec(self):
        unknown = ModelSpec("unknown", "openai", "audio")
        disabled = ModelSpec("disabled", "openai", "audio", enabled=False)
        self.cfg.pool("audio").models.append(disabled)
        for spec in (unknown, disabled):
            with self.subTest(model_id=spec.id), \
                 patch.object(probe, "run_adapter", return_value=GenResult("ok", spec.id, spec.provider)) as adapter, \
                 patch.object(self.cfg, "resolve_api_key", wraps=self.cfg.resolve_api_key) as key:
                result = probe.deep_probe(self.cfg, spec)
                self.assertFalse(result["ok"])
                self.assertGreaterEqual(result["elapsed_ms"], 0)
                self.assertEqual(result["diagnostic"]["kind"], "config")
                adapter.assert_not_called()
                key.assert_not_called()

    def test_direct_deep_probe_replaces_same_id_forged_spec_before_native_check(self):
        for provider in ("native", "openai"):
            forged = ModelSpec("tts", provider, "image", model="not-approved",
                               endpoint="https://not-approved.invalid", options={"api_key": "not-approved"},
                               params={"model": "not-approved"})
            with self.subTest(provider=provider), \
                 patch.object(probe, "run_adapter", return_value=GenResult("ok", "tts", "dashscope")) as adapter:
                result = probe.deep_probe(self.cfg, forged, fail_fast=True)
                self.assertTrue(result["ok"])
                self.assertIs(adapter.call_args[0][0], self.spec)
                self.assertEqual(adapter.call_args[0][1].kind, "audio")
                self.assertEqual(adapter.call_args[0][3].api_key, "offline-fixture-key")

    def test_saved_target_key_resolution_matches_generation_for_all_sources(self):
        self.assertTrue(callable(getattr(probe.ProbeTarget, "from_saved_model", None)), "保存模型需有独立可信target投影")
        cases = [
            ("environment", "dashscope", "MR_TTS_TEST", {}, {}, {}),
            ("model-id", "openai", "", {}, {}, {"tts": "model-id-key"}),
            ("inline", "openai", "", {"api_key": "inline-key"}, {}, {}),
            ("raw-inline", "openai", "", {}, {"api_key": "raw-inline-key"}, {}),
            ("kling-pair", "kling", "", {"access_key": "fixture-ak", "secret_key": "fixture-sk"}, {}, {}),
            ("empty", "openai", "", {}, {}, {"fixture": "vendor-key-must-not-fallback"}),
        ]
        for name, provider, env, options, raw, keys in cases:
            with self.subTest(source=name):
                self.spec.provider, self.spec.api_key_env = provider, env
                self.spec.options, self.spec.raw, self.spec.vendor = options, raw, "fixture"
                self.cfg.secrets = {"keys": keys}
                target = probe.ProbeTarget.from_saved_model(self.cfg, self.spec)
                # 即使同进程修改了临时target，内部保存ID分支仍只认cfg凭据。
                target.api_key = "page-input-must-not-win"
                target.api_key_env = "MR_TTS_TEST"
                self.assertEqual(probe.resolve_key(self.cfg, target), self.cfg.resolve_api_key(self.spec))
                if name == "empty":
                    self.assertEqual(probe.resolve_key(self.cfg, target)[0], "")

    def test_saved_target_uses_model_provider_when_vendor_protocol_differs(self):
        self.cfg.vendors["fixture"] = config.Vendor("fixture", "dashscope", catalog="dashscope")
        self.spec.vendor, self.spec.provider = "fixture", "kling"
        self.spec.endpoint = "https://model.invalid/v1/audio/tts"
        target = probe.ProbeTarget.from_saved_model(self.cfg, self.spec)
        self.assertEqual(target.provider, "kling")
        self.assertEqual(target.catalog_key, "kling")
        self.assertEqual(target.endpoints, {"audio": self.spec.endpoint})

    def test_saved_target_factory_replaces_forged_spec_and_rejects_disabled(self):
        self.assertTrue(callable(getattr(probe.ProbeTarget, "from_saved_model", None)), "保存模型需有独立可信target投影")
        forged = ModelSpec("tts", "native", "image", endpoint="https://wrong.invalid")
        target = probe.ProbeTarget.from_saved_model(self.cfg, forged)
        self.assertEqual((target.provider, target.endpoints, target.saved_model_id),
                         ("dashscope", {}, "tts"))
        self.spec.enabled = False
        with self.assertRaises(config.ConfigError):
            probe.ProbeTarget.from_saved_model(self.cfg, forged)
        with self.assertRaises(config.ConfigError):
            probe.resolve_key(self.cfg, target)

    def test_payload_cannot_select_internal_saved_target_key_mode(self):
        api = webserver.ConfigApi("127.0.0.1")
        target = api._target_from_payload({"provider": "openai", "saved_model_id": "tts", "api_key": "page-key"})
        self.assertEqual(getattr(target, "saved_model_id", ""), "")
        self.assertEqual(probe.resolve_key(self.cfg, target), ("page-key", "页面输入"))

    def test_light_unsaved_form_still_supported_without_generation(self):
        overlay = webserver.store.Overlay({"vendors": [{"id": "fixture", "provider": "openai", "catalog": "openai"}]}, base={})
        for deep in (None, False):
            payload = {"model": "not-saved", "vendor": "fixture", "kind": "audio"}
            if deep is not None:
                payload["deep"] = deep
            with self.subTest(deep=deep), self.model_api() as (api, light, adapter), \
                 patch.object(webserver.store, "load_overlay", return_value=overlay), \
                 patch.object(webserver.store, "save_with") as save:
                result = api.model_test({}, payload)
                self.assertTrue(result["ok"])
                self.assertIn("checks", result)
                self.assertEqual(light.call_args.kwargs["model_name"], "not-saved")
                adapter.assert_not_called()
                save.assert_not_called()

    def test_failed_result_has_elapsed_time(self):
        with patch.object(probe, "run_adapter", return_value=GenResult("error", "tts", "dashscope", error="no output")):
            result = probe.deep_probe(self.cfg, self.spec)
        self.assertFalse(result["ok"])
        self.assertGreaterEqual(result["elapsed_ms"], 0)

    def test_strict_direct_probe_native_does_not_delegate(self):
        native = ModelSpec("native", "native", "image")
        self.cfg.pools["image"] = Pool("image", "priority_then_weight", [native])
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
             patch.object(probe, "probe_target", return_value=probe.ProbeResult(False, summary="unreachable")), \
             patch.object(probe, "deep_probe") as deep:
            result = api.model_test({}, {"id": "tts", "deep": True})
        self.assertFalse(result["ok"])
        self.assertFalse(result["real_call"]["ok"])
        deep.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
