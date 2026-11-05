#!/usr/bin/env python3
"""公共错误诊断回归：合成厂商响应，不调用配置加载或外部接口。"""

import importlib
import importlib.util
import json
import sys
import unittest
import urllib.error
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from mrouter.config import ConfigError  # noqa: E402
from mrouter.transport import HttpError  # noqa: E402


def vendor_error(status, payload, **kwargs):
    return HttpError(f"HTTP {status}", status, json.dumps(payload, ensure_ascii=False), **kwargs)


class DiagnosticTests(unittest.TestCase):
    def module(self):
        spec = importlib.util.find_spec("mrouter.diagnostics")
        self.assertIsNotNone(spec, "缺少公共 mrouter.diagnostics 模块")
        return importlib.import_module("mrouter.diagnostics")

    def diagnose(self, exc):
        result = self.module().diagnose_error(exc)
        self.assertEqual(set(result), {"kind", "category", "summary", "detail", "hint",
                                       "error_code", "submission_uncertain"})
        self.assertIn(result["kind"], ("config", "runtime", "uncertain"))
        self.assertIsInstance(result["submission_uncertain"], bool)
        for key in ("category", "summary", "detail", "hint", "error_code"):
            self.assertIsInstance(result[key], str)
        return result

    def test_public_contract_and_known_config_codes(self):
        module = self.module()
        for name in ("error_payload", "vendor_error_code", "vendor_error_detail", "diagnose_error"):
            self.assertTrue(callable(getattr(module, name, None)), name)
        self.assertTrue({"1113", "1102", "arrearage", "invalidapikey", "modelnotfound", "modelnotopen", "accessdenied"}
                        .issubset(module.CONFIG_ERROR_CODES))
        self.assertNotIn("invalidparameter", module.CONFIG_ERROR_CODES,
                         "顶层 InvalidParameter 不足以确定配置根因")

    def test_error_payload_only_accepts_json_objects(self):
        module = self.module()
        for body in ("", "not JSON", "[1,2]", '"message"', "null", "42"):
            with self.subTest(body=body):
                self.assertIsNone(module.error_payload(HttpError("failure", body=body)))
        self.assertEqual(module.error_payload(vendor_error(400, {"code": "x", "message": "detail"})),
                         {"code": "x", "message": "detail"})
        self.assertIsNone(module.error_payload(RuntimeError("no body")))

    def test_vendor_error_codes_support_existing_response_shapes(self):
        module = self.module()
        for payload, expected in (
            ({"code": "Arrearage"}, "Arrearage"),
            ({"error_code": "ModelNotFound"}, "ModelNotFound"),
            ({"error": {"code": 1113, "message": "余额不足"}}, "1113"),
            ({"header": {"code": 1102, "message": "Account balance not enough"}}, "1102"),
            ({"code": "top", "error": {"code": "nested"}}, "top"),
        ):
            with self.subTest(payload=payload):
                self.assertEqual(module.vendor_error_code(vendor_error(400, payload)), expected)
        self.assertEqual(module.vendor_error_code(RuntimeError("x")), "")

    def test_vendor_detail_preserves_original_text_and_fallback(self):
        module = self.module()
        for payload, expected in (
            ({"code": "Arrearage", "message": "Account is in arrears"}, "Arrearage Account is in arrears"),
            ({"error": {"code": 1113, "message": "余额不足或无可用资源包"}}, "1113 余额不足或无可用资源包"),
            ({"header": {"code": 1102, "message": "Account balance not enough"}}, "1102 Account balance not enough"),
            ({"error": "vendor original text"}, "vendor original text"),
        ):
            with self.subTest(payload=payload):
                self.assertEqual(module.vendor_error_detail(vendor_error(400, payload)), expected)
        self.assertEqual(module.vendor_error_detail(HttpError("x", body=" first\n second  ")), "first second")
        self.assertEqual(module.vendor_error_detail(RuntimeError("no body")), "")
        self.assertLessEqual(len(module.vendor_error_detail(HttpError("x", body="v" * 800))), 200)

    def test_previous_400_411_is_engine_runtime_with_unknown_root_cause(self):
        exc = vendor_error(400, {"code": "InvalidParameter", "message":
                                "[cosyvoice:]Engine error [411]: TTS speak operation failed"})
        diag = self.diagnose(exc)
        self.assertEqual((diag["kind"], diag["category"]), ("runtime", "engine"))
        self.assertIn("根因待确认", diag["hint"])
        self.assertIn("厂商引擎", diag["summary"])
        self.assertIn("[cosyvoice:]Engine error [411]: TTS speak operation failed", diag["detail"])
        self.assertEqual(diag["error_code"], "InvalidParameter")
        self.assertFalse(diag["submission_uncertain"])

    def test_engine_evidence_overrides_invalid_parameter_validation_words(self):
        diag = self.diagnose(vendor_error(400, {"code": "InvalidParameter", "message":
                                              "parameter voice is invalid: engine failure [411]"}))
        self.assertEqual((diag["kind"], diag["category"]), ("runtime", "engine"))
        self.assertIn("根因待确认", diag["hint"])

    def test_unknown_invalid_parameter_remains_runtime(self):
        for message in ("Something went wrong", "Invalid parameter", "Request is invalid", "operation failed"):
            with self.subTest(message=message):
                diag = self.diagnose(vendor_error(400, {"code": "InvalidParameter", "message": message}))
                self.assertEqual(diag["kind"], "runtime")
                self.assertIn(message, diag["detail"])
                self.assertIn("待确认", diag["hint"])

    def test_zhipu_1113_means_quota_or_resource_package_not_rate_limit(self):
        diag = self.diagnose(vendor_error(429, {"error": {"code": 1113, "message": "余额不足或无可用资源包"}}))
        self.assertEqual((diag["kind"], diag["category"]), ("config", "quota"))
        self.assertIn("额度", diag["summary"])
        self.assertIn("资源包", diag["summary"] + diag["hint"])
        self.assertNotIn("限流", diag["summary"])
        self.assertEqual(diag["error_code"], "1113")
        self.assertIn("余额不足或无可用资源包", diag["detail"])

    def test_balance_and_purchase_errors_are_config(self):
        for code in ("Arrearage", "InsufficientBalance", "1102", "Unpurchased"):
            with self.subTest(code=code):
                diag = self.diagnose(vendor_error(400, {"code": code, "message": "vendor original"}))
                self.assertEqual((diag["kind"], diag["category"]), ("config", "quota"))

    def test_model_not_found_or_not_open_is_config(self):
        for code in ("ModelNotFound", "ModelNotOpen"):
            with self.subTest(code=code):
                diag = self.diagnose(vendor_error(404, {"code": code, "message": "vendor original"}))
                self.assertEqual((diag["kind"], diag["category"]), ("config", "model"))
        for message in ("The model `missing` does not exist", "模型不存在，请检查模型名"):
            with self.subTest(message=message):
                diag = self.diagnose(vendor_error(400, {"code": "InvalidParameter", "message": message}))
                self.assertEqual((diag["kind"], diag["category"]), ("config", "model"))

    def test_unavailable_model_without_absence_evidence_is_runtime(self):
        diag = self.diagnose(vendor_error(503, {"code": "InvalidParameter", "message": "The model is not available right now"}))
        self.assertEqual(diag["kind"], "runtime")
        self.assertIn("待确认", diag["hint"])

    def test_escaped_unicode_model_absence_evidence_is_config(self):
        exc = HttpError("HTTP 400", 400, json.dumps({"code": "InvalidParameter", "message": "模型不存在，请核对名称"}))
        diag = self.diagnose(exc)
        self.assertEqual((diag["kind"], diag["category"]), ("config", "model"))

    def test_concrete_field_validation_is_config(self):
        for message in ("Parameter 'size' must be one of [1024x1024, 1536x1024]",
                        "input.text is required", "duration must be between 1 and 10",
                        "参数 size 不合法，允许 1024x1024"):
            with self.subTest(message=message):
                diag = self.diagnose(vendor_error(400, {"code": "InvalidParameter", "message": message}))
                self.assertEqual((diag["kind"], diag["category"]), ("config", "parameter"))
                self.assertIn(message, diag["detail"])

    def test_authentication_permission_and_http_payment_errors_are_config(self):
        for status, code, category in ((401, "", "authentication"), (400, "InvalidApiKey", "authentication"),
                                       (403, "", "permission"), (400, "AccessDenied", "permission"),
                                       (402, "", "quota")):
            with self.subTest(status=status, code=code):
                diag = self.diagnose(vendor_error(status, {"code": code, "message": "vendor original"}))
                self.assertEqual((diag["kind"], diag["category"]), ("config", category))

    def test_local_config_error_uses_configuration_category(self):
        diag = self.diagnose(ConfigError("缺少 submit_url"))
        self.assertEqual((diag["kind"], diag["category"]), ("config", "configuration"))
        self.assertIn("缺少 submit_url", diag["detail"])

    def test_plain_429_is_runtime_rate_limit(self):
        diag = self.diagnose(vendor_error(429, {"code": "TooManyRequests", "message": "rate limited"}))
        self.assertEqual((diag["kind"], diag["category"]), ("runtime", "rate_limit"))
        self.assertIn("限流", diag["summary"])

    def test_network_timeout_without_submission_evidence_is_runtime(self):
        for exc in (TimeoutError("query timed out"), urllib.error.URLError("connection reset"),
                    HttpError("网络错误：query timed out")):
            with self.subTest(exc=type(exc).__name__):
                diag = self.diagnose(exc)
                self.assertEqual((diag["kind"], diag["category"]), ("runtime", "network"))
                self.assertFalse(diag["submission_uncertain"])

    def test_submission_uncertainty_has_priority_over_config_or_engine_code(self):
        for payload in ({"code": "ModelNotFound", "message": "model does not exist"},
                        {"code": "InvalidParameter", "message": "Engine error [411]"},
                        {"code": "1113", "message": "余额不足"}):
            with self.subTest(payload=payload):
                diag = self.diagnose(vendor_error(503, payload, submission_uncertain=True))
                self.assertEqual((diag["kind"], diag["category"]), ("uncertain", "submission_uncertain"))
                self.assertTrue(diag["submission_uncertain"])
                self.assertIn("提交结果不确定", diag["summary"])
                self.assertIn("平台", diag["hint"])
                self.assertIn("不要", diag["hint"])
                self.assertIn(payload["message"], diag["detail"])

    def test_html_submission_uncertainty_is_actionable_and_compact(self):
        body = (
            '<!doctype html><html><head><title>Proxy Login</title></head><body>'
            '<p>请先登录网络</p><a href="https://login.invalid/?token=page-secret">x</a>'
            "</body></html>"
        )
        diag = self.diagnose(HttpError("响应不是合法 JSON", 200, body, submission_uncertain=True))
        self.assertEqual((diag["kind"], diag["category"]), ("uncertain", "submission_uncertain"))
        self.assertIn("HTML", diag["summary"])
        self.assertIn("接口地址", diag["hint"])
        self.assertIn("代理", diag["hint"])
        self.assertIn("不要", diag["hint"])
        self.assertIn("Proxy Login", diag["detail"])
        self.assertNotIn("<html", diag["detail"])
        self.assertNotIn("page-secret", diag["detail"])

    def test_html_response_without_uncertainty_is_html_category(self):
        body = b"<html><head><title>Gateway</title></head><body>maintenance</body></html>"
        diag = self.diagnose(HttpError("响应不是合法 JSON", 200, body))
        self.assertEqual((diag["kind"], diag["category"]), ("runtime", "html_response"))
        self.assertFalse(diag["submission_uncertain"])
        self.assertIn("HTML", diag["summary"])
        self.assertIn("接口地址", diag["hint"])

    def test_unknown_errors_do_not_guess_config_root_cause(self):
        for exc in (RuntimeError("unknown vendor failure"), vendor_error(400, {"code": "Mystery", "message": "unknown"}),
                    vendor_error(404, {"message": "not found"}), vendor_error(503, {"message": "gateway down"})):
            with self.subTest(exc=str(exc)):
                diag = self.diagnose(exc)
                self.assertEqual(diag["kind"], "runtime")
                self.assertFalse(diag["submission_uncertain"])
                self.assertTrue(diag["detail"])
                self.assertTrue(diag["hint"])

    def test_diagnostics_mask_every_text_field_and_payload_without_url_exceptions(self):
        module = self.module()
        url = "https://user:password@stub.invalid/file?key=key-secret&token=token-secret&Signature=signature-secret&sig=sig-secret"
        exc = RuntimeError("upstream error " + url)
        exc.body = json.dumps({"code": "Mystery", "message": "vendor original " + url,
                               "urls": [url], "nested": {"url": url}})
        diag = self.diagnose(exc)
        serialized = json.dumps([diag, module.error_payload(exc), module.vendor_error_detail(exc)], ensure_ascii=False)
        for secret in ("password", "key-secret", "token-secret", "signature-secret", "sig-secret"):
            self.assertNotIn(secret, serialized)
        self.assertIn("vendor original", diag["detail"])
        self.assertIn("***", serialized)

    def test_unknown_and_local_config_error_details_are_also_masked(self):
        for error_type in (RuntimeError, ConfigError):
            with self.subTest(error_type=error_type.__name__):
                diag = self.diagnose(error_type("failed https://stub.invalid/x?api_key=private&Signature=signed"))
                self.assertNotIn("private", json.dumps(diag))
                self.assertNotIn("signed", json.dumps(diag))


if __name__ == "__main__":
    unittest.main(verbosity=2)
