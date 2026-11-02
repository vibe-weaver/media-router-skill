#!/usr/bin/env python3
"""可靠性修复：运行真实 CLI 控制流，隔离配置/状态/网络与付费接口。"""
import io
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from http.client import BadStatusLine, LineTooLong
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from mrouter import cli, transport
from mrouter.adapters import DownloadError, GenResult, KindNotSupported
from mrouter.config import ConfigError, ModelSpec, Pool, RouterConfig
from mrouter.health import HealthStore


class GenerationControlsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mr-controls-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.models = [ModelSpec("m1", "openai", "image", api_key_env="MR_TEST_KEY"),
                       ModelSpec("m2", "openai", "image", api_key_env="MR_TEST_KEY"),
                       ModelSpec("native", "native", "image")]
        self.cfg = RouterConfig(self.root, self.root, self.root / "state", self.root / "models.yaml",
                                {"output_dir": str(self.root / "outputs"), "caption": {"mode": "none"}},
                                {"image": Pool("image", "priority_then_weight", self.models)},
                                {"keys": {"MR_TEST_KEY": "not-a-real-key"}}, "yaml")

    def invoke(self, first_error=None, strict=False, ordered=None, caption=None):
        calls = []
        active = list(self.models if ordered is None else ordered)
        args = cli.build_parser().parse_args(["generate", "--kind", "image", "--prompt", "p",
                                               "--max-attempts", "2", "--no-download"])
        args.fail_fast = strict  # 直调cmd_generate，兼容原parser，用行为RED而非参数未知错误。
        def adapter(spec, req, cfg, ctx):
            calls.append(spec.id)
            if spec.id == "m1" and first_error is not None:
                raise first_error
            if spec.is_native:
                return GenResult("delegate", spec.id, spec.provider, delegate={"tool": "native"})
            return GenResult("ok", spec.id, spec.provider, urls=["https://stub.invalid/output.png"])
        out = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch.object(cli, "load_config", return_value=self.cfg))
            stack.enter_context(patch.object(cli, "_resolve", return_value=(active, {"requires": ["text2img"]})))
            stack.enter_context(patch.object(cli, "run_adapter", side_effect=adapter))
            if caption is not None:
                stack.enter_context(patch.object(cli, "profile_files", return_value=[]))
                stack.enter_context(patch.object(cli, "build_caption", side_effect=caption))
            stack.enter_context(redirect_stdout(out))
            rc = cli.cmd_generate(args)
        return rc, json.loads(out.getvalue()), calls

    def test_strict_stops_all_failure_categories(self):
        errors = [ConfigError("bad config"), KindNotSupported("unsupported", kind="image"),
                  RuntimeError("provider failed"),
                  transport.HttpError("HTTP 404", 404, '{"code":"ModelNotFound","message":"model does not exist"}')]
        for exc in errors:
            with self.subTest(error=type(exc).__name__):
                rc, payload, calls = self.invoke(exc, strict=True)
                self.assertEqual(rc, 1)
                self.assertEqual(calls, ["m1"])
                self.assertEqual(payload["attempted"], ["m1"])
                self.assertNotIn("delegate", payload)
                self.assertNotEqual(payload["error"], "所有候选模型都失败了")

    def test_default_keeps_explicit_rejection_fallback(self):
        exc = transport.HttpError("HTTP 404", 404, '{"code":"ModelNotFound","message":"model does not exist"}')
        rc, payload, calls = self.invoke(exc)
        self.assertEqual((rc, calls), (0, ["m1", "m2"]))
        self.assertEqual(payload["fallback_trail"][0]["kind"], "config")
        self.assertFalse(HealthStore(self.cfg.health_path).state("m1").get("failure_count"))

    def test_default_and_strict_failure_matrix_preserves_trail_and_health(self):
        cases = (
            (ConfigError("bad config"), "config", 0),
            (KindNotSupported("unsupported", kind="image"), "config", 0),
            (RuntimeError("provider failed"), "runtime", 1),
            (transport.HttpError("HTTP 404", 404, '{"code":"ModelNotFound"}'), "config", 0),
        )
        for error, expected_kind, failures in cases:
            for strict in (False, True):
                with self.subTest(error=type(error).__name__, strict=strict):
                    HealthStore(self.cfg.health_path).reset()
                    rc, payload, calls = self.invoke(error, strict=strict)
                    self.assertEqual((rc, calls), (1, ["m1"]) if strict else (0, ["m1", "m2"]))
                    self.assertEqual([item["model"] for item in payload["fallback_trail"]], ["m1"])
                    self.assertEqual(payload["fallback_trail"][0]["kind"], expected_kind)
                    self.assertEqual(HealthStore(self.cfg.health_path).state("m1").get("failure_count", 0), failures)
                    if strict:
                        self.assertEqual(payload["attempted"], ["m1"])

    def test_default_explicit_failure_can_reach_native_but_strict_cannot(self):
        for strict in (False, True):
            with self.subTest(strict=strict):
                error = transport.HttpError("HTTP 404", 404, '{"code":"ModelNotFound"}')
                rc, payload, calls = self.invoke(error, strict=strict, ordered=[self.models[0], self.models[-1]])
                self.assertEqual((rc, calls), (1, ["m1"]) if strict else (3, ["m1", "native"]))
                self.assertEqual(payload["fallback_trail"][0]["kind"], "config")
                self.assertFalse(HealthStore(self.cfg.health_path).state("m1").get("failure_count"))

    def test_uncertain_submission_stops_even_default(self):
        exc = transport.HttpError("submission result unknown", 503)
        exc.submission_uncertain = True
        for strict in (False, True):
            with self.subTest(strict=strict):
                rc, payload, calls = self.invoke(exc, strict=strict)
                self.assertEqual((rc, calls), (1, ["m1"]))
                self.assertEqual(payload["attempted"], ["m1"])
                self.assertTrue(payload["submission_uncertain"])
                self.assertIn("平台", payload["hint"])
                self.assertFalse(HealthStore(self.cfg.health_path).state("m1").get("failure_count"))

    def test_strict_native_never_runs_or_delegates(self):
        rc, payload, calls = self.invoke(strict=True, ordered=[self.models[-1]])
        self.assertEqual((rc, calls), (1, []))
        self.assertNotIn("delegate", payload)
        self.assertIn("严格", payload["error"])
        self.assertEqual(payload["attempted"], [])

    def test_default_native_remains_delegate(self):
        rc, payload, calls = self.invoke(ordered=[self.models[-1]])
        self.assertEqual((rc, payload["status"], calls), (3, "delegate", ["native"]))

    def test_download_failure_never_falls_back(self):
        rc, payload, calls = self.invoke(DownloadError("CDN down", urls=["https://stub.invalid/a.png"]))
        self.assertEqual((rc, calls), (1, ["m1"]))
        self.assertEqual(payload["urls"], ["https://stub.invalid/a.png"])
        self.assertFalse(HealthStore(self.cfg.health_path).state("m1").get("failure_count"))

    def test_direct_command_context_covers_adapter_and_is_reset(self):
        seen = []
        original = cli.run_adapter
        def adapter(spec, req, cfg, ctx):
            seen.append(transport.is_fail_fast())
            return GenResult("ok", spec.id, spec.provider)
        args = cli.build_parser().parse_args(["generate", "--kind", "image", "--prompt", "p", "--no-download"])
        args.fail_fast = True
        with patch.object(cli, "load_config", return_value=self.cfg), \
             patch.object(cli, "_resolve", return_value=([self.models[0]], {})), \
             patch.object(cli, "run_adapter", side_effect=adapter), redirect_stdout(io.StringIO()):
            rc = cli.cmd_generate(args)
        self.assertEqual(rc, 0)
        self.assertEqual(seen, [True])
        self.assertFalse(transport.is_fail_fast())

    def test_strict_context_also_covers_caption(self):
        seen = []
        file = self.root / "synthetic.png"
        file.write_bytes(b"test-fixture")
        args = cli.build_parser().parse_args(["generate", "--kind", "image", "--prompt", "p"])
        args.fail_fast = True
        def caption(*args, **kwargs):
            seen.append(transport.is_fail_fast())
            return "", []
        with patch.object(cli, "load_config", return_value=self.cfg), \
             patch.object(cli, "_resolve", return_value=([self.models[0]], {})), \
             patch.object(cli, "run_adapter", return_value=GenResult("ok", "m1", "openai", files=[str(file)])), \
             patch.object(cli, "build_caption", side_effect=caption), redirect_stdout(io.StringIO()):
            rc = cli.cmd_generate(args)
        self.assertEqual(rc, 0)
        self.assertEqual(seen, [True])
        self.assertFalse(transport.is_fail_fast())

    def test_cli_reports_mismatch_without_changing_generation_success(self):
        file = self.root / "synthetic.mp4"
        file.write_bytes(b"test-fixture")
        spec = ModelSpec("video", "openai", "video", api_key_env="MR_TEST_KEY")
        args = cli.build_parser().parse_args(["generate", "--kind", "video", "--prompt", "p", "--duration", "5"])
        generated = GenResult("ok", spec.id, spec.provider, files=[str(file)],
                              meta={"effective_spec": {"duration_seconds": 5, "size_status": "platform_default"}, "watermark": True})
        output = io.StringIO()
        with patch.object(cli, "load_config", return_value=self.cfg), \
             patch.object(cli, "_resolve", return_value=([spec], {})), \
             patch.object(cli, "run_adapter", return_value=generated), \
             patch.object(cli, "profile_files", return_value=[{"path":str(file),"duration_seconds":5.19,"bytes":10,"format":"mp4"}]), \
             patch.object(cli, "build_caption", return_value=("", [])), redirect_stdout(output):
            rc = cli.cmd_generate(args)
        payload = json.loads(output.getvalue())
        self.assertEqual((rc, payload["status"]), (0, "ok"))
        self.assertEqual(payload["assessment"]["status"], "mismatch")
        self.assertEqual(payload["assessment"]["watermark"], "present")
        self.assertIn("5.19", " ".join(payload["notes"]))

    def invoke_generated(self, strict=False, failed_stage="", actual_caption=False):
        file = self.root / "generated.png"
        file.write_bytes(bytes.fromhex("89504e470d0a1a0a0000000d494844520000000100000001080200000000000000"))
        calls = []
        urls = ["https://stub.invalid/generated.png?signature=usable"]
        args = cli.build_parser().parse_args(["generate", "--kind", "image", "--prompt", "p"])
        args.fail_fast = strict
        if actual_caption:
            self.cfg.defaults["caption"] = {"mode": "vlm", "model": "test-caption",
                                             "endpoint": "https://stub.invalid/caption",
                                             "api_key_env": "MR_TEST_KEY"}
        def adapter(spec, req, cfg, ctx):
            calls.append(spec.id)
            return GenResult("ok", spec.id, spec.provider, files=[str(file)], urls=urls)
        class RawResponse(io.BytesIO):
            status = 200
            headers = {"Content-Type": "application/json"}
        output = io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch.object(cli, "load_config", return_value=self.cfg))
            stack.enter_context(patch.object(cli, "_resolve", return_value=(self.models, {})))
            stack.enter_context(patch.object(cli, "run_adapter", side_effect=adapter))
            stack.enter_context(patch.object(transport, "open_request", return_value=RawResponse(b'not JSON https://stub.invalid/?token=private')))
            if failed_stage in ("health_write", "health_replace"):
                name = "write_text" if failed_stage == "health_write" else "replace"
                stack.enter_context(patch.object(Path, name, side_effect=OSError("simulated disk failure")))
            elif failed_stage:
                target, name = (HealthStore, "record_success") if failed_stage == "health" else (cli, failed_stage)
                stack.enter_context(patch.object(target, name, side_effect=RuntimeError("postprocessing failed https://stub.invalid/?token=private")))
            stack.enter_context(redirect_stdout(output))
            rc = cli.cmd_generate(args)
        return rc, json.loads(output.getvalue()), calls, file, urls

    def test_actual_caption_invalid_json_preserves_success_without_regeneration(self):
        for strict in (False, True):
            with self.subTest(strict=strict):
                rc, payload, calls, file, urls = self.invoke_generated(strict, actual_caption=True)
                self.assertEqual(calls, ["m1"])
                self.assertEqual((rc, payload["status"]), (0, "ok"))
                self.assertEqual(payload["files"][0]["path"], str(file))
                self.assertEqual(payload["urls"], urls)
                self.assertTrue(file.is_file())
                self.assertEqual(payload["fallback_trail"], [])
                notes = " ".join(payload["notes"])
                self.assertIn("描述", notes)
                self.assertNotIn("private", notes)
                self.assertFalse(HealthStore(self.cfg.health_path).state("m1").get("failure_count"))

    def test_profile_assessment_and_health_failures_do_not_regenerate(self):
        for stage in ("profile_files", "assess_result", "health"):
            for strict in (False, True):
                with self.subTest(stage=stage, strict=strict):
                    rc, payload, calls, file, urls = self.invoke_generated(strict, failed_stage=stage)
                    self.assertEqual(calls, ["m1"])
                    self.assertEqual((rc, payload["status"]), (0, "ok"))
                    self.assertEqual(payload["files"][0]["path"], str(file))
                    self.assertEqual(payload["urls"], urls)
                    self.assertTrue(file.is_file())
                    self.assertEqual(payload["fallback_trail"], [])
                    self.assertNotIn("private", " ".join(payload["notes"]))
                    self.assertIn("postprocessing failed", " ".join(payload["notes"]))
                    if stage != "health":
                        self.assertEqual(payload["assessment"]["status"], "unverified")
                    self.assertFalse(HealthStore(self.cfg.health_path).state("m1").get("failure_count"))

    def test_real_health_save_oserror_warns_without_regeneration(self):
        for stage in ("health_write", "health_replace"):
            for strict in (False, True):
                with self.subTest(stage=stage, strict=strict):
                    rc, payload, calls, file, urls = self.invoke_generated(strict, failed_stage=stage)
                    self.assertEqual(calls, ["m1"])
                    self.assertEqual((rc, payload["status"]), (0, "ok"))
                    self.assertEqual(payload["files"][0]["path"], str(file))
                    self.assertEqual(payload["urls"], urls)
                    self.assertTrue(file.is_file())
                    self.assertEqual(payload["fallback_trail"], [])
                    self.assertFalse(HealthStore(self.cfg.health_path).state("m1").get("failure_count"))
                    self.assertIn("健康状态未能回写", " ".join(payload["notes"]))

    def test_health_save_outcome_is_explicit_and_failure_keeps_memory(self):
        store = HealthStore(self.cfg.health_path)
        self.assertIs(store.record_success("m1"), True)
        self.assertIs(store.save(), True)
        with patch.object(Path, "write_text", side_effect=OSError("disk full")):
            self.assertIs(store.record_success("m1"), False)
            self.assertIs(store.save(), False)
        self.assertEqual(store.state("m1")["success_count"], 2)
        self.assertEqual(HealthStore(self.cfg.health_path).state("m1")["success_count"], 1)

    def test_output_failure_does_not_reenter_generation_loop(self):
        args = cli.build_parser().parse_args(["generate", "--kind", "image", "--prompt", "p", "--no-download"])
        calls = []
        def adapter(spec, req, cfg, ctx):
            calls.append(spec.id)
            return GenResult("ok", spec.id, spec.provider)
        with patch.object(cli, "load_config", return_value=self.cfg), \
             patch.object(cli, "_resolve", return_value=(self.models, {})), \
             patch.object(cli, "run_adapter", side_effect=adapter), \
             patch.object(cli, "_emit", side_effect=RuntimeError("output unavailable")):
            with self.assertRaisesRegex(RuntimeError, "output unavailable"):
                cli.cmd_generate(args)
        self.assertEqual(calls, ["m1"])
        self.assertFalse(HealthStore(self.cfg.health_path).state("m1").get("failure_count"))

    def test_malformed_submission_response_stops_real_transport_adapter_chain(self):
        class RawResponse(io.BytesIO):
            status = 200
            headers = {"Content-Type": "application/json"}
        for strict in (False, True):
            with self.subTest(strict=strict):
                requests = []
                def opener(req, timeout):
                    requests.append(req.get_method())
                    return RawResponse(b'<html>response damaged</html>')
                args = cli.build_parser().parse_args(["generate", "--kind", "image", "--prompt", "p", "--no-download"])
                args.fail_fast = strict
                output = io.StringIO()
                with patch.object(cli, "load_config", return_value=self.cfg), \
                     patch.object(cli, "_resolve", return_value=(self.models, {})), \
                     patch.object(transport, "open_request", side_effect=opener), \
                     patch.object(cli, "run_adapter", wraps=cli.run_adapter) as adapter, \
                     patch.object(transport.time, "sleep") as sleep, redirect_stdout(output):
                    rc = cli.cmd_generate(args)
                payload = json.loads(output.getvalue())
                self.assertEqual([call.args[0].id for call in adapter.call_args_list], ["m1"])
                self.assertEqual(requests, ["POST"])
                self.assertEqual((rc, payload["status"]), (1, "error"))
                self.assertTrue(payload["submission_uncertain"])
                self.assertEqual(payload["attempted"], ["m1"])
                self.assertIn("平台", payload["hint"])
                self.assertFalse(HealthStore(self.cfg.health_path).state("m1").get("failure_count"))
                sleep.assert_not_called()

    def test_protocol_receive_errors_stop_real_transport_adapter_chain(self):
        for exc_class in (BadStatusLine, LineTooLong):
            for strict in (False, True):
                with self.subTest(error=exc_class.__name__, strict=strict):
                    requests = []
                    def opener(req, timeout):
                        requests.append(req.get_method())
                        raise exc_class("damaged response")
                    args = cli.build_parser().parse_args(["generate", "--kind", "image", "--prompt", "p", "--no-download"])
                    args.fail_fast = strict
                    output = io.StringIO()
                    with patch.object(cli, "load_config", return_value=self.cfg), \
                         patch.object(cli, "_resolve", return_value=(self.models, {})), \
                         patch.object(transport, "open_request", side_effect=opener), \
                         patch.object(cli, "run_adapter", wraps=cli.run_adapter) as adapter, \
                         patch.object(transport.time, "sleep") as sleep, redirect_stdout(output):
                        rc = cli.cmd_generate(args)
                    payload = json.loads(output.getvalue())
                    self.assertEqual([call.args[0].id for call in adapter.call_args_list], ["m1"])
                    self.assertEqual(requests, ["POST"])
                    self.assertEqual((rc, payload["status"]), (1, "error"))
                    self.assertTrue(payload["submission_uncertain"])
                    self.assertEqual(payload["attempted"], ["m1"])
                    self.assertEqual(payload["fallback_trail"][0]["kind"], "uncertain")
                    self.assertIn("平台", payload["hint"])
                    self.assertFalse(HealthStore(self.cfg.health_path).state("m1").get("failure_count"))
                    sleep.assert_not_called()

    def test_resolve_strict_plan_has_only_first_real_candidate(self):
        args = cli.build_parser().parse_args(["resolve", "--kind", "image", "--max-attempts", "2"])
        args.fail_fast = True
        output = io.StringIO()
        with patch.object(cli, "load_config", return_value=self.cfg), \
             patch.object(cli, "_resolve", return_value=(self.models, {})), redirect_stdout(output):
            rc = cli.cmd_resolve(args)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(output.getvalue())["will_attempt"], ["m1"])

    def test_parser_documents_candidate_budget_separately(self):
        args = cli.build_parser().parse_args(["generate", "--kind", "audio", "--prompt", "你好", "--fail-fast"])
        self.assertTrue(args.fail_fast)
        resolve = cli.build_parser().parse_args(["resolve", "--kind", "image", "--fail-fast"])
        self.assertTrue(resolve.fail_fast)


if __name__ == "__main__":
    unittest.main(verbosity=2)
