#!/usr/bin/env python3
"""模型执行授权回归：只用临时配置、假凭据及离线请求桩。"""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from mrouter import adapters, cli, config, transport
from mrouter.adapters import GenResult
from mrouter.config import ConfigError, ModelSpec, Pool, RouterConfig


class ModelAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mr-model-auth-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.enabled = ModelSpec("approved", "openai", "image", model="approved-remote",
            api_key_env="MR_AUTH_FIXTURE_KEY", endpoint="https://offline.invalid/v1/images/generations")
        self.disabled = ModelSpec("disabled", "openai", "image", model="disabled-remote",
            api_key_env="MR_AUTH_FIXTURE_KEY", endpoint=self.enabled.endpoint, enabled=False)
        self.cfg = RouterConfig(self.root, self.root, self.root / "state", self.root / "unused.json",
            {"output_dir": str(self.root / "outputs"), "caption": {"mode": "none"}},
            {"image": Pool("image", "priority_then_weight", [self.enabled, self.disabled])},
            {"keys": {"MR_AUTH_FIXTURE_KEY": "synthetic-credential-only"}}, "json")
        self.guard = ExitStack()
        self.addCleanup(self.guard.close)
        self.guard.enter_context(patch.dict(os.environ, {"MR_AUTH_FIXTURE_KEY": ""}))
        for name in ("socket.create_connection", "socket.socket.connect", "socket.socket.connect_ex",
                     "socket.getaddrinfo", "urllib.request.urlopen"):
            self.guard.enter_context(patch(name, side_effect=AssertionError("禁止真实网络")))
        self.guard.enter_context(patch.object(config, "load_secrets", side_effect=AssertionError("禁止真实密钥读取")))
        self.guard.enter_context(patch.object(transport, "open_request", side_effect=AssertionError("禁止真实请求")))

    def invoke(self, flags=(), real_adapter=False):
        output = io.StringIO()
        result = GenResult("ok", "approved", "openai", urls=["https://offline.invalid/artifact.png"])
        with patch.object(cli, "load_config", return_value=self.cfg), \
             patch.object(cli, "_reconfigure_stdout"), \
             patch.object(cli, "run_adapter", wraps=adapters.run_adapter if real_adapter else None,
                          return_value=result) as adapter, \
             patch.object(self.cfg, "resolve_api_key", wraps=self.cfg.resolve_api_key) as key, \
             redirect_stdout(output):
            code = cli.main(["generate", "--kind", "image", "--prompt", "offline authorization audit",
                             "--no-download", "--max-attempts", "1"] + list(flags))
        return code, json.loads(output.getvalue()), adapter, key

    def test_disabled_forced_model_rejected_before_key_and_adapter(self):
        code, result, adapter, key = self.invoke(["--model", "disabled"])
        self.assertEqual(code, 1)
        self.assertEqual(result["kind"], "config")
        self.assertIn("停用", result["error"])
        key.assert_not_called()
        adapter.assert_not_called()
        self.assertFalse(self.disabled.enabled)

    def test_unknown_forced_id_rejected_without_key_or_adapter(self):
        code, result, adapter, key = self.invoke(["--model", "not-configured"])
        self.assertEqual(code, 1)
        self.assertIn("只能调用配置里已有", result["error"])
        key.assert_not_called()
        adapter.assert_not_called()

    def test_enabled_forced_model_preserves_normal_generation(self):
        code, result, adapter, key = self.invoke(["--model", "approved", "--param", "seed=0", "--param", "quality=high"])
        self.assertEqual(code, 0)
        self.assertEqual(result["model"]["id"], "approved")
        self.assertEqual(adapter.call_args.args[0], self.enabled)
        self.assertEqual(adapter.call_args.args[1].extra, {"seed": 0, "quality": "high"})
        self.assertTrue(key.called)

    def test_automatic_routing_never_uses_disabled_entry(self):
        code, result, adapter, _ = self.invoke()
        self.assertEqual(code, 0)
        self.assertEqual(adapter.call_args.args[0].id, "approved")
        self.assertEqual(result["model"]["id"], "approved")

    def test_sensitive_extra_is_rejected_before_any_key_or_candidate(self):
        parameters = (
            "model=not-approved", "model_name=not-approved", "model_id=not-approved",
            "MODEL=not-approved", "__model__=not-approved", "model-name=not-approved",
            "input.model=not-approved", "input[model]=not-approved", "version=not-approved",
            "resource_id=not-approved", "api_key=private-fixture-value",
            "Authorization=private-fixture-value", "endpoint=https://not-approved.invalid",
            "inputs.model=not-approved", "payload.model_name=not-approved",
        )
        for selected in ([], ["--model", "approved"]):
            for parameter in parameters:
                with self.subTest(forced=bool(selected), parameter=parameter.partition("=")[0]):
                    code, result, adapter, key = self.invoke(selected + ["--param", parameter])
                    self.assertEqual(code, 1)
                    self.assertEqual(result["kind"], "config")
                    key.assert_not_called()
                    adapter.assert_not_called()
                    text = json.dumps(result)
                    for private_value in ("private-fixture-value", "not-approved.invalid", "synthetic-credential-only"):
                        self.assertNotIn(private_value, text)

    def test_forced_model_with_mismatched_kind_is_rejected_before_key(self):
        audio = ModelSpec("audio-only", "openai", "audio", api_key_env=self.enabled.api_key_env)
        self.cfg.pools["audio"] = Pool("audio", "priority_then_weight", [audio])
        code, result, adapter, key = self.invoke(["--model", "audio-only"])
        self.assertEqual(code, 1)
        self.assertIn("不一致", result["error"])
        key.assert_not_called()
        adapter.assert_not_called()

    def test_execution_helper_returns_canonical_but_lookup_still_reads_disabled(self):
        helper = getattr(self.cfg, "require_enabled_model", None)
        self.assertTrue(callable(helper), "缺少执行授权入口")
        self.assertIs(helper("approved", kind="image"), self.enabled)
        self.assertIs(self.cfg.find_model("disabled"), self.disabled)
        with self.assertRaises(ConfigError):
            helper("disabled")
        with self.assertRaises(ConfigError):
            helper("not-configured")
        with self.assertRaises(ConfigError):
            helper("approved", kind="video")


if __name__ == "__main__":
    unittest.main()
