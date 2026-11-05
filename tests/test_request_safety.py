#!/usr/bin/env python3
"""请求安全回归：标准库桩、临时文件，不访问外部接口或个人配置。"""

import inspect
import io
import sys
import tempfile
import threading
import unittest
import urllib.error
from http.client import BadStatusLine, IncompleteRead, LineTooLong, RemoteDisconnected
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from mrouter import transport  # noqa: E402


class Response(io.BytesIO):
    status = 200
    headers = {"Content-Type": "application/json"}


class BrokenResponse(Response):
    def read(self, size=-1):
        raise IncompleteRead(b"partial", 4)


class ReceiveErrorResponse(Response):
    def __init__(self, error):
        super().__init__()
        self.error = error

    def read(self, size=-1):
        raise self.error


def http_error(req, status):
    return urllib.error.HTTPError(
        req.full_url, status, "failed", {},
        io.BytesIO(b'{"code":"upstream_failure","message":"vendor detail"}'),
    )


class RequestSafetyTests(unittest.TestCase):
    def setUp(self):
        # 即使遗漏某个用例的桩，也不能意外连到外部。
        self.network_guard = patch.object(
            transport, "open_request", side_effect=AssertionError("network not stubbed")
        )
        self.network_guard.start()
        self.addCleanup(self.network_guard.stop)

    def policy(self, **kwargs):
        self.assertTrue(callable(getattr(transport, "execution_policy", None)),
                        "缺少上下文局部 execution_policy 接口")
        self.assertTrue(callable(getattr(transport, "is_fail_fast", None)),
                        "缺少 is_fail_fast 接口")
        return transport.execution_policy(**kwargs)

    def test_post_503_not_replayed(self):
        calls = []

        def fail(req, timeout):
            calls.append(req.get_method())
            raise http_error(req, 503)

        with patch.object(transport, "open_request", fail), patch.object(transport.time, "sleep") as sleep:
            with self.assertRaises(transport.HttpError) as cm:
                transport.request("POST", "https://stub.invalid/submit", json_body={"prompt": "p"})
        self.assertEqual(calls, ["POST"])
        self.assertTrue(cm.exception.submission_uncertain)
        sleep.assert_not_called()

    def test_post_patch_http_errors_never_replay_and_preserve_evidence(self):
        statuses = (400, 401, 402, 403, 404, 408, 425, 429, *range(500, 600))
        for method in ("POST", "PATCH"):
            for status in statuses:
                with self.subTest(method=method, status=status):
                    calls = []

                    def fail(req, timeout):
                        calls.append(req.get_method())
                        raise http_error(req, status)

                    with patch.object(transport, "open_request", fail), patch.object(transport.time, "sleep") as sleep:
                        with self.assertRaises(transport.HttpError) as cm:
                            transport.request(method.lower(), "https://stub.invalid/submit", json_body={"prompt": "p"})
                    self.assertEqual(calls, [method])
                    self.assertEqual(cm.exception.status, status)
                    self.assertIn("vendor detail", cm.exception.body)
                    uncertain = status in (408, 425) or 500 <= status < 600
                    self.assertEqual(getattr(cm.exception, "submission_uncertain", None), uncertain)
                    if uncertain:
                        self.assertIn("确认", str(cm.exception))
                    sleep.assert_not_called()

    def test_submission_empty_json_is_uncertain_without_replay(self):
        for method in ("POST", "PATCH"):
            for strict in (False, True):
                with self.subTest(method=method, strict=strict):
                    raw = Response(b"")
                    raw.status = 202
                    with self.policy(fail_fast=strict), patch.object(transport, "open_request", return_value=raw) as opened, patch.object(transport.time, "sleep") as sleep:
                        with self.assertRaises(transport.HttpError) as cm:
                            transport.request(method.lower(), "https://stub.invalid/submit").json()
                    self.assertEqual(opened.call_count, 1)
                    self.assertEqual(opened.call_args.args[0].get_method(), method)
                    self.assertTrue(cm.exception.submission_uncertain)
                    self.assertEqual(cm.exception.status, 202)
                    self.assertEqual(cm.exception.body, "")
                    self.assertIn(transport._SUBMISSION_HINT, str(cm.exception))
                    sleep.assert_not_called()

    def test_submission_invalid_json_preserves_redacted_evidence_without_replay(self):
        body = b'<html>https://user:password@stub.invalid/x?token=body-secret&sig=body-sign</html>'
        for method in ("POST", "PATCH"):
            for strict in (False, True):
                for content in (body, b" \r\n", b'{"truncated":'):
                    with self.subTest(method=method, strict=strict, content=content):
                        raw = Response(content)
                        raw.status = 201
                        with self.policy(fail_fast=strict), patch.object(transport, "open_request", return_value=raw) as opened, patch.object(transport.time, "sleep") as sleep:
                            with self.assertRaises(transport.HttpError) as cm:
                                transport.request(method.lower(), "https://stub.invalid/submit").json()
                        self.assertEqual(opened.call_count, 1)
                        self.assertTrue(cm.exception.submission_uncertain)
                        self.assertEqual(cm.exception.status, 201)
                        self.assertEqual(cm.exception.body, transport.redact(content.decode()))
                        self.assertIn(transport._SUBMISSION_HINT, str(cm.exception))
                        for secret in ("password", "body-secret", "body-sign"):
                            self.assertNotIn(secret, str(cm.exception))
                            self.assertNotIn(secret, cm.exception.body)
                        sleep.assert_not_called()

    def test_get_invalid_json_is_not_uncertain_and_preserves_evidence(self):
        body = b'<html>https://stub.invalid/x?key=get-secret&Signature=get-sign</html>'
        for strict in (False, True):
            with self.subTest(strict=strict):
                raw = Response(body)
                raw.status = 200
                with self.policy(fail_fast=strict), patch.object(transport, "open_request", return_value=raw) as opened, patch.object(transport.time, "sleep") as sleep:
                    with self.assertRaises(transport.HttpError) as cm:
                        transport.request("get", "https://stub.invalid/query").json()
                self.assertEqual(opened.call_count, 1)
                self.assertFalse(cm.exception.submission_uncertain)
                self.assertEqual(cm.exception.status, 200)
                self.assertEqual(cm.exception.body, transport.redact(body.decode()))
                self.assertNotIn(transport._SUBMISSION_HINT, str(cm.exception))
                for secret in ("get-secret", "get-sign"):
                    self.assertNotIn(secret, str(cm.exception))
                    self.assertNotIn(secret, cm.exception.body)
                sleep.assert_not_called()

    def test_http_response_legacy_three_arguments_remain_compatible(self):
        headers = {"Content-Type": "application/json"}
        empty = transport.HttpResponse(204, headers, b"")
        self.assertEqual((empty.status, empty.headers, empty.body), (204, headers, b""))
        self.assertIsNone(empty.json())
        self.assertEqual(transport.HttpResponse(200, headers, b'{"ok":true}').json(), {"ok": True})
        with self.assertRaises(transport.HttpError) as cm:
            transport.HttpResponse(200, headers, b"invalid").json()
        self.assertFalse(cm.exception.submission_uncertain)
        self.assertNotIn(transport._SUBMISSION_HINT, str(cm.exception))
        with self.assertRaises(TypeError):
            transport.HttpResponse(200, headers, b"", "POST")

    def test_http_response_method_is_optional_keyword_only_and_normalized(self):
        parameter = inspect.signature(transport.HttpResponse).parameters.get("method")
        self.assertIsNotNone(parameter, "缺少 keyword-only method 请求方法证据")
        self.assertEqual(parameter.kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertEqual(parameter.default, "")
        self.assertEqual(transport.HttpResponse(200, {}, b"{}").method, "")
        for method in ("post", "patch", "get"):
            with self.subTest(method=method):
                response = transport.HttpResponse(200, {}, b'{"ok":true}', method=method)
                self.assertEqual(response.method, method.upper())
                self.assertEqual(response.json(), {"ok": True})

    def test_html_submission_response_is_uncertain_with_actionable_hint(self):
        body = (
            b'<!doctype html><html><head><title>Proxy Login</title></head><body>'
            b'<a href="https://login.invalid/?token=page-secret">login</a></body></html>'
        )
        raw = Response(body)
        raw.status = 200
        raw.headers = {"Content-Type": "text/html"}
        with patch.object(transport, "open_request", return_value=raw):
            with self.assertRaises(transport.HttpError) as cm:
                transport.request(
                    "POST", "https://stub.invalid/submit", generation_submission=True
                ).json()
        self.assertTrue(cm.exception.submission_uncertain)
        text = str(cm.exception)
        self.assertIn("HTML", text)
        self.assertIn("接口地址", text)
        self.assertIn("代理", text)
        self.assertIn(transport._SUBMISSION_HINT, text)
        self.assertNotIn("page-secret", text)
        self.assertNotIn("page-secret", cm.exception.body)

    def test_html_non_submission_response_is_not_uncertain(self):
        raw = Response(b"<html><head><title>Gateway</title></head><body>maintenance</body></html>")
        raw.status = 200
        with patch.object(transport, "open_request", return_value=raw):
            with self.assertRaises(transport.HttpError) as cm:
                transport.request("GET", "https://stub.invalid/query").json()
        self.assertFalse(cm.exception.submission_uncertain)
        self.assertIn("HTML", str(cm.exception))
        self.assertNotIn(transport._SUBMISSION_HINT, str(cm.exception))

    def test_new_api_gateway_page_recommends_full_endpoint(self):
        body = b'<!doctype html><html><head><title>New API</title></head><body>app</body></html>'
        raw = Response(body)
        raw.status = 200
        with patch.object(transport, "open_request", return_value=raw):
            with self.assertRaises(transport.HttpError) as cm:
                transport.request(
                    "POST", "https://gw.invalid/v1/images/generations",
                    generation_submission=True,
                ).json()
        text = str(cm.exception)
        self.assertIn("New API", text)
        self.assertIn("/v1/images/generations", text)
        self.assertIn("/v1/audio/speech", text)
        self.assertTrue(cm.exception.submission_uncertain)

    def test_request_keeps_binary_body_unparsed_and_records_method(self):
        body = b"RIFF\x00\xffWAVEbinary-audio"
        for method in ("POST", "PATCH", "GET"):
            with self.subTest(method=method):
                raw = Response(body)
                raw.headers = {"Content-Type": "audio/wav"}
                with patch.object(transport, "open_request", return_value=raw) as opened, patch.object(transport.json, "loads", side_effect=AssertionError("binary must not be parsed")) as loads:
                    response = transport.request(method.lower(), "https://stub.invalid/audio")
                self.assertEqual(opened.call_count, 1)
                self.assertEqual(response.body, body)
                self.assertEqual(response.headers["Content-Type"], "audio/wav")
                loads.assert_not_called()
                self.assertEqual(getattr(response, "method", None), method)

    def test_get_empty_json_keeps_none_result(self):
        with patch.object(transport, "open_request", return_value=Response(b"")):
            self.assertIsNone(transport.request("GET", "https://stub.invalid/query").json())

    def test_submission_network_errors_are_wrapped_once_as_uncertain(self):
        factories = (
            lambda: urllib.error.URLError("connection lost"),
            lambda: TimeoutError("timed out"),
            lambda: ConnectionResetError("connection reset"),
            lambda: RemoteDisconnected("remote closed"),
            lambda: IncompleteRead(b"partial", 4),
        )
        for method in ("POST", "PATCH"):
            for factory in factories:
                with self.subTest(method=method, error=type(factory()).__name__):
                    calls = []

                    def fail(req, timeout):
                        calls.append(req.get_method())
                        raise factory()

                    with patch.object(transport, "open_request", fail), patch.object(transport.time, "sleep") as sleep:
                        try:
                            transport.request(method, "https://stub.invalid/submit")
                        except Exception as exc:
                            caught = exc
                        else:
                            self.fail("提交失败必须抛异常")
                    self.assertEqual(calls, [method])
                    self.assertIsInstance(caught, transport.HttpError)
                    self.assertTrue(getattr(caught, "submission_uncertain", False))
                    self.assertIn("不会自动重发", str(caught))
                    sleep.assert_not_called()

    def test_submission_http_error_body_receive_failure_is_uncertain(self):
        factories = (lambda: TimeoutError("body timed out"),
                     lambda: RemoteDisconnected("body disconnected"),
                     lambda: IncompleteRead(b'{"code":"1113"}', 4))
        for method in ("POST", "PATCH"):
            for factory in factories:
                with self.subTest(method=method, error=type(factory()).__name__):
                    calls = []

                    def fail(req, timeout):
                        calls.append(req.get_method())
                        raise urllib.error.HTTPError(req.full_url, 429, "failed", {}, BrokenResponse())

                    with patch.object(BrokenResponse, "read", side_effect=factory()), patch.object(transport, "open_request", fail), patch.object(transport.time, "sleep") as sleep:
                        with self.assertRaises(transport.HttpError) as cm:
                            transport.request(method, "https://stub.invalid/submit")
                    self.assertEqual(calls, [method])
                    self.assertEqual(cm.exception.status, 429)
                    self.assertTrue(cm.exception.submission_uncertain)
                    if isinstance(factory(), IncompleteRead):
                        self.assertIn("1113", cm.exception.body)
                    sleep.assert_not_called()

    def test_submission_response_incomplete_read_is_uncertain(self):
        with patch.object(transport, "open_request", return_value=BrokenResponse()), patch.object(transport.time, "sleep") as sleep:
            try:
                transport.request("POST", "https://stub.invalid/submit")
            except Exception as exc:
                caught = exc
            else:
                self.fail("不完整响应必须抛异常")
        self.assertIsInstance(caught, transport.HttpError)
        self.assertTrue(getattr(caught, "submission_uncertain", False))
        sleep.assert_not_called()

    def test_submission_protocol_receive_errors_are_wrapped_once_as_uncertain(self):
        factories = (
            lambda: BadStatusLine("https://user:password@stub.invalid/x?token=protocol-secret&sig=protocol-sign"),
            lambda: LineTooLong("https://user:password@stub.invalid/x?token=protocol-secret&sig=protocol-sign"),
        )
        for method in ("POST", "PATCH"):
            for strict in (False, True):
                for stage in ("open", "body"):
                    for factory in factories:
                        with self.subTest(method=method, strict=strict, stage=stage, error=type(factory()).__name__):
                            def fail(req, timeout):
                                if stage == "open":
                                    raise factory()
                                return ReceiveErrorResponse(factory())

                            with self.policy(fail_fast=strict), patch.object(transport, "open_request", side_effect=fail) as opened, patch.object(transport.time, "sleep") as sleep:
                                with self.assertRaises(Exception) as cm:
                                    transport.request(method, "https://stub.invalid/submit?key=url-secret&Signature=url-sign")
                            self.assertEqual(opened.call_count, 1)
                            self.assertIsInstance(cm.exception, transport.HttpError)
                            self.assertTrue(getattr(cm.exception, "submission_uncertain", False))
                            self.assertIn(transport._SUBMISSION_HINT, str(cm.exception))
                            for secret in ("password", "protocol-secret", "protocol-sign", "url-secret", "url-sign"):
                                self.assertNotIn(secret, str(cm.exception))
                            sleep.assert_not_called()

    def test_submission_http_error_body_protocol_failure_is_uncertain(self):
        factories = (lambda: BadStatusLine("bad status"), lambda: LineTooLong("header"))
        for method in ("POST", "PATCH"):
            for strict in (False, True):
                for status in (400, 429):
                    for factory in factories:
                        with self.subTest(method=method, strict=strict, status=status, error=type(factory()).__name__):
                            def fail(req, timeout):
                                raise urllib.error.HTTPError(
                                    req.full_url, status, "failed", {}, ReceiveErrorResponse(factory())
                                )

                            with self.policy(fail_fast=strict), patch.object(transport, "open_request", side_effect=fail) as opened, patch.object(transport.time, "sleep") as sleep:
                                with self.assertRaises(transport.HttpError) as cm:
                                    transport.request(method, "https://stub.invalid/submit")
                            self.assertEqual(opened.call_count, 1)
                            self.assertEqual(cm.exception.status, status)
                            self.assertEqual(cm.exception.body, "")
                            self.assertTrue(cm.exception.submission_uncertain)
                            self.assertIn(transport._SUBMISSION_HINT, str(cm.exception))
                            sleep.assert_not_called()

    def test_submission_http_error_tail_signature_preserves_hint_and_evidence(self):
        url = "https://user:password@stub.invalid/submit?key=url-secret&Signature=tail-signature"
        for method in ("POST", "PATCH"):
            for strict in (False, True):
                for status in (408, 503):
                    with self.subTest(method=method, strict=strict, status=status):
                        def fail(req, timeout):
                            return_error = urllib.error.HTTPError(
                                req.full_url, status, "failed", {},
                                io.BytesIO(b'vendor detail https://stub.invalid/x?token=body-secret'),
                            )
                            raise return_error

                        with self.policy(fail_fast=strict), patch.object(transport, "open_request", side_effect=fail) as opened, patch.object(transport.time, "sleep") as sleep:
                            with self.assertRaises(transport.HttpError) as cm:
                                transport.request(method, url)
                        error = cm.exception
                        self.assertEqual(opened.call_count, 1)
                        self.assertTrue(error.submission_uncertain)
                        self.assertEqual(error.status, status)
                        self.assertIn("vendor detail", error.body)
                        self.assertIn(transport._SUBMISSION_HINT, str(error))
                        self.assertIn("\n" + transport._SUBMISSION_HINT, str(error))
                        for secret in ("password", "url-secret", "tail-signature", "body-secret"):
                            self.assertNotIn(secret, str(error))
                            self.assertNotIn(secret, error.body)
                        sleep.assert_not_called()

    def test_submission_network_error_tail_signature_preserves_hint(self):
        url = "https://stub.invalid/submit?Signature=tail-signature"
        reason = "https://user:password@stub.invalid/x?token=reason-secret&sig=reason-signature"
        factories = (
            lambda: urllib.error.URLError(reason),
            lambda: TimeoutError(reason),
            lambda: BadStatusLine(reason),
            lambda: LineTooLong(reason),
        )
        for method in ("POST", "PATCH"):
            for strict in (False, True):
                for factory in factories:
                    with self.subTest(method=method, strict=strict, error=type(factory()).__name__):
                        with self.policy(fail_fast=strict), patch.object(transport, "open_request", side_effect=factory()) as opened, patch.object(transport.time, "sleep") as sleep:
                            with self.assertRaises(transport.HttpError) as cm:
                                transport.request(method, url)
                        self.assertEqual(opened.call_count, 1)
                        self.assertTrue(cm.exception.submission_uncertain)
                        self.assertIn(transport._SUBMISSION_HINT, str(cm.exception))
                        self.assertIn("\n" + transport._SUBMISSION_HINT, str(cm.exception))
                        for secret in ("tail-signature", "password", "reason-secret", "reason-signature"):
                            self.assertNotIn(secret, str(cm.exception))
                        sleep.assert_not_called()

    def test_submission_invalid_json_tail_sensitive_url_preserves_hint(self):
        for method in ("POST", "PATCH"):
            for parameter in ("Signature", "sig", "token", "key"):
                with self.subTest(method=method, parameter=parameter):
                    body = f"invalid JSON https://user:password@stub.invalid/x?{parameter}=body-secret"
                    response = transport.HttpResponse(200, {}, body.encode(), method=method)
                    with self.assertRaises(transport.HttpError) as cm:
                        response.json()
                    self.assertTrue(cm.exception.submission_uncertain)
                    self.assertEqual(cm.exception.status, 200)
                    self.assertEqual(cm.exception.body, transport.redact(body))
                    self.assertIn(transport._SUBMISSION_HINT, str(cm.exception))
                    self.assertIn("\n" + transport._SUBMISSION_HINT, str(cm.exception))
                    for secret in ("password", "body-secret"):
                        self.assertNotIn(secret, str(cm.exception))
                        self.assertNotIn(secret, cm.exception.body)

    def test_get_default_protocol_receive_errors_retry_to_success(self):
        factories = (lambda: BadStatusLine("bad status"), lambda: LineTooLong("header"))
        for stage in ("open", "body"):
            for factory in factories:
                with self.subTest(stage=stage, error=type(factory()).__name__):
                    calls = []

                    def flaky(req, timeout):
                        calls.append(req.get_method())
                        if len(calls) <= 2:
                            if stage == "open":
                                raise factory()
                            return ReceiveErrorResponse(factory())
                        return Response(b'{"ok":true}')

                    with patch.object(transport, "open_request", flaky), patch.object(transport.time, "sleep") as sleep:
                        try:
                            response = transport.request("GET", "https://stub.invalid/query", max_retries=2)
                        except Exception as exc:
                            self.fail(f"GET 协议接收失败应能重试恢复，而非抛出 {type(exc).__name__}")
                    self.assertEqual(calls, ["GET", "GET", "GET"])
                    self.assertEqual(response.json(), {"ok": True})
                    self.assertEqual(sleep.call_count, 2)

    def test_get_protocol_receive_error_retries_are_bounded(self):
        factories = (lambda: BadStatusLine("bad status"), lambda: LineTooLong("header"))
        for stage in ("open", "body"):
            for factory in factories:
                with self.subTest(stage=stage, error=type(factory()).__name__):
                    def fail(req, timeout):
                        if stage == "open":
                            raise factory()
                        return ReceiveErrorResponse(factory())

                    with patch.object(transport, "open_request", side_effect=fail) as opened, patch.object(transport.time, "sleep") as sleep:
                        with self.assertRaises(Exception) as cm:
                            transport.request("GET", "https://stub.invalid/query", max_retries=2)
                    self.assertEqual(opened.call_count, 3)
                    self.assertEqual(sleep.call_count, 2)
                    self.assertIsInstance(cm.exception, transport.HttpError)
                    self.assertFalse(cm.exception.submission_uncertain)
                    self.assertNotIn(transport._SUBMISSION_HINT, str(cm.exception))

    def test_fail_fast_get_protocol_receive_error_does_not_retry(self):
        factories = (lambda: BadStatusLine("bad status"), lambda: LineTooLong("header"))
        for stage in ("open", "body"):
            for factory in factories:
                with self.subTest(stage=stage, error=type(factory()).__name__):
                    def fail(req, timeout):
                        if stage == "open":
                            raise factory()
                        return ReceiveErrorResponse(factory())

                    with self.policy(fail_fast=True), patch.object(transport, "open_request", side_effect=fail) as opened, patch.object(transport.time, "sleep") as sleep:
                        with self.assertRaises(Exception) as cm:
                            transport.request("GET", "https://stub.invalid/query")
                    self.assertEqual(opened.call_count, 1)
                    self.assertIsInstance(cm.exception, transport.HttpError)
                    self.assertFalse(cm.exception.submission_uncertain)
                    self.assertNotIn(transport._SUBMISSION_HINT, str(cm.exception))
                    sleep.assert_not_called()

    def test_get_http_error_body_protocol_failure_respects_retry_policy(self):
        factories = (lambda: BadStatusLine("bad status"), lambda: LineTooLong("header"))
        for strict in (False, True):
            for factory in factories:
                with self.subTest(strict=strict, error=type(factory()).__name__):
                    def fail(req, timeout):
                        raise urllib.error.HTTPError(
                            req.full_url, 503, "failed", {}, ReceiveErrorResponse(factory())
                        )

                    with self.policy(fail_fast=strict), patch.object(transport, "open_request", side_effect=fail) as opened, patch.object(transport.time, "sleep") as sleep:
                        with self.assertRaises(transport.HttpError) as cm:
                            transport.request("GET", "https://stub.invalid/query", max_retries=2)
                    self.assertEqual(opened.call_count, 1 if strict else 3)
                    self.assertEqual(sleep.call_count, 0 if strict else 2)
                    self.assertEqual(cm.exception.status, 503)
                    self.assertFalse(cm.exception.submission_uncertain)
                    self.assertNotIn(transport._SUBMISSION_HINT, str(cm.exception))

    def test_local_submission_parameter_and_serialization_errors_are_not_wrapped(self):
        cases = (
            ("https://stub.invalid/submit", {"json_body": object()}, TypeError),
            ("not-a-valid-url", {}, ValueError),
            ("https://stub.invalid/submit", {"files": {"input": ("input.bin", b"data")}}, ValueError),
        )
        for method in ("POST", "PATCH"):
            for strict in (False, True):
                for url, kwargs, error_type in cases:
                    with self.subTest(method=method, strict=strict, error=error_type.__name__, url=url):
                        with self.policy(fail_fast=strict), patch.object(transport, "open_request") as opened, patch.object(transport.time, "sleep") as sleep:
                            with self.assertRaises(error_type) as cm:
                                transport.request(method, url, **kwargs)
                        self.assertNotIsInstance(cm.exception, transport.HttpError)
                        self.assertFalse(getattr(cm.exception, "submission_uncertain", False))
                        opened.assert_not_called()
                        sleep.assert_not_called()

    def test_local_open_parameter_errors_are_not_wrapped_or_retried(self):
        for method in ("POST", "PATCH", "GET"):
            for error in (TypeError("invalid header"), ValueError("invalid timeout")):
                with self.subTest(method=method, error=type(error).__name__):
                    with patch.object(transport, "open_request", side_effect=error) as opened, patch.object(transport.time, "sleep") as sleep:
                        with self.assertRaises(type(error)) as cm:
                            transport.request(method, "https://stub.invalid/query")
                    self.assertIs(cm.exception, error)
                    self.assertEqual(opened.call_count, 1)
                    self.assertNotIsInstance(cm.exception, transport.HttpError)
                    self.assertFalse(getattr(cm.exception, "submission_uncertain", False))
                    sleep.assert_not_called()

    def test_get_head_default_http_retries_are_preserved(self):
        for method in ("GET", "HEAD"):
            with self.subTest(method=method):
                calls = []

                def flaky(req, timeout):
                    calls.append(req.get_method())
                    if len(calls) == 1:
                        raise http_error(req, 503)
                    return Response(b'{"ok":true}')

                with patch.object(transport, "open_request", flaky), patch.object(transport.time, "sleep") as sleep:
                    result = transport.request(method, "https://stub.invalid/query")
                self.assertEqual(calls, [method, method])
                self.assertEqual(result.status, 200)
                sleep.assert_called_once()

    def test_get_default_network_and_incomplete_read_retry(self):
        for factory in (lambda: TimeoutError("timed out"), lambda: urllib.error.URLError("lost"),
                        lambda: IncompleteRead(b"partial", 4)):
            with self.subTest(error=type(factory()).__name__):
                calls = []

                def flaky(req, timeout):
                    calls.append(req.get_method())
                    if len(calls) == 1:
                        raise factory()
                    return Response(b"ok")

                with patch.object(transport, "open_request", flaky), patch.object(transport.time, "sleep"):
                    try:
                        response = transport.request("GET", "https://stub.invalid/query")
                    except Exception as exc:
                        self.fail(f"GET 应能恢复而非抛出 {type(exc).__name__}")
                self.assertEqual(calls, ["GET", "GET"])
                self.assertEqual(response.body, b"ok")

    def test_get_retries_are_bounded_and_not_submission_uncertain(self):
        def fail(req, timeout):
            raise http_error(req, 503)

        with patch.object(transport, "open_request", side_effect=fail) as opened, patch.object(transport.time, "sleep") as sleep:
            with self.assertRaises(transport.HttpError) as cm:
                transport.request("GET", "https://stub.invalid/query", max_retries=2)
        self.assertEqual(opened.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        self.assertFalse(getattr(cm.exception, "submission_uncertain", None))

    def test_fail_fast_get_disables_http_and_network_retry(self):
        for failure in ("http", "network", "incomplete"):
            with self.subTest(failure=failure):
                calls = []

                def fail(req, timeout):
                    calls.append(req.get_method())
                    if failure == "http":
                        raise http_error(req, 503)
                    if failure == "network":
                        raise TimeoutError("timed out")
                    raise IncompleteRead(b"partial", 4)

                with self.policy(fail_fast=True), patch.object(transport, "open_request", fail), patch.object(transport.time, "sleep") as sleep:
                    with self.assertRaises(transport.HttpError) as cm:
                        transport.request("GET", "https://stub.invalid/query")
                self.assertEqual(calls, ["GET"])
                self.assertFalse(cm.exception.submission_uncertain)
                sleep.assert_not_called()

    def test_download_default_http_retry_writes_atomically(self):
        calls = []
        with tempfile.TemporaryDirectory(prefix="mr-safety-") as tmp:
            dest = Path(tmp) / "artifact.bin"

            def flaky(req, timeout):
                calls.append(req.get_method())
                self.assertFalse(dest.exists())
                if len(calls) == 1:
                    raise http_error(req, 503)
                return Response(b"complete artifact")

            with patch.object(transport, "open_request", flaky), patch.object(transport.time, "sleep") as sleep:
                result = transport.download("https://stub.invalid/artifact", dest)
            self.assertEqual(result, dest)
            self.assertEqual(dest.read_bytes(), b"complete artifact")
            self.assertEqual(list(Path(tmp).glob("*.part")), [])
            self.assertEqual(calls, ["GET", "GET"])
            sleep.assert_called_once()

    def test_download_default_incomplete_read_retries_and_removes_partial(self):
        with tempfile.TemporaryDirectory(prefix="mr-safety-") as tmp:
            dest = Path(tmp) / "artifact.bin"
            with patch.object(transport, "open_request", side_effect=[BrokenResponse(), Response(b"complete")]) as opened, patch.object(transport.time, "sleep"):
                try:
                    transport.download("https://stub.invalid/artifact", dest)
                except Exception as exc:
                    self.fail(f"下载应能恢复而非抛出 {type(exc).__name__}")
            self.assertEqual(opened.call_count, 2)
            self.assertEqual(dest.read_bytes(), b"complete")
            self.assertEqual(list(Path(tmp).glob("*.part")), [])

    def test_fail_fast_download_stops_and_keeps_existing_file(self):
        for failure in ("http", "network", "incomplete"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory(prefix="mr-safety-") as tmp:
                dest = Path(tmp) / "artifact.bin"
                dest.write_bytes(b"existing")
                calls = []

                def fail(req, timeout):
                    calls.append(req.get_method())
                    if failure == "http":
                        raise http_error(req, 503)
                    if failure == "network":
                        raise TimeoutError("timed out")
                    return BrokenResponse()

                with self.policy(fail_fast=True), patch.object(transport, "open_request", fail), patch.object(transport.time, "sleep") as sleep:
                    with self.assertRaises(transport.HttpError) as cm:
                        transport.download("https://stub.invalid/artifact", dest)
                self.assertEqual(calls, ["GET"])
                self.assertEqual(dest.read_bytes(), b"existing")
                self.assertEqual(list(Path(tmp).glob("*.part")), [])
                self.assertFalse(cm.exception.submission_uncertain)
                sleep.assert_not_called()

    def test_fail_fast_keeps_normal_pending_to_success_poll(self):
        calls = []

        def pending_then_ready():
            calls.append("probe")
            return None if len(calls) < 3 else {"task": "done"}

        with self.policy(fail_fast=True), patch.object(transport.time, "sleep") as sleep:
            result = transport.poll(pending_then_ready, interval=1, max_seconds=10)
        self.assertEqual(result, {"task": "done"})
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleep.call_count, 2)

    def test_policy_resets_after_exception(self):
        with self.policy():
            self.assertFalse(transport.is_fail_fast())
            with self.assertRaisesRegex(ValueError, "expected"):
                with transport.execution_policy(fail_fast=True):
                    self.assertTrue(transport.is_fail_fast())
                    raise ValueError("expected")
            self.assertFalse(transport.is_fail_fast())
        self.assertFalse(transport.is_fail_fast())

    def test_nested_default_policy_cannot_lower_fail_fast(self):
        with self.policy(fail_fast=True):
            with transport.execution_policy():
                self.assertTrue(transport.is_fail_fast())
            self.assertTrue(transport.is_fail_fast())
        self.assertFalse(transport.is_fail_fast())

    def test_barrier_threads_have_independent_policy_and_retries(self):
        with self.policy():
            barrier = threading.Barrier(2, timeout=5)
            calls = {"strict": 0, "default": 0}
            observed = {}
            errors = []

            def fail(req, timeout):
                name = threading.current_thread().name
                calls[name] += 1
                raise http_error(req, 503)

            def worker(name, strict):
                try:
                    with transport.execution_policy(fail_fast=strict):
                        barrier.wait()
                        observed[name] = transport.is_fail_fast()
                        with self.assertRaises(transport.HttpError):
                            transport.request("GET", "https://stub.invalid/query", max_retries=1)
                        barrier.wait()
                    observed[name + "_after"] = transport.is_fail_fast()
                except BaseException as exc:
                    errors.append(exc)

            with patch.object(transport, "open_request", fail), patch.object(transport.time, "sleep"):
                threads = [threading.Thread(target=worker, name=name, args=(name, strict))
                           for name, strict in (("strict", True), ("default", False))]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=10)
                self.assertFalse(any(thread.is_alive() for thread in threads))
            self.assertEqual(errors, [])
            self.assertEqual(observed, {"strict": True, "default": False,
                                        "strict_after": False, "default_after": False})
            self.assertEqual(calls, {"strict": 1, "default": 2})
            self.assertFalse(transport.is_fail_fast())

    def test_http_error_positional_compatibility_and_keyword_only_evidence(self):
        old = transport.HttpError("HTTP 400", 400, "body")
        self.assertEqual((str(old), old.status, old.body), ("HTTP 400", 400, "body"))
        self.assertEqual(getattr(old, "submission_uncertain", None), False)
        explicit = transport.HttpError("HTTP 503", 503, "body", submission_uncertain=True)
        self.assertTrue(explicit.submission_uncertain)
        with self.assertRaises(TypeError):
            transport.HttpError("HTTP 503", 503, "body", True)

    def test_transport_error_text_masks_credentials_without_changing_redact(self):
        url = "https://stub.invalid/submit?key=url-secret&Signature=signed-secret"
        reason = "lost https://user:password@stub.invalid/x?token=reason-secret&sig=reason-sign"
        with patch.object(transport, "open_request", side_effect=urllib.error.URLError(reason)):
            with self.assertRaises(transport.HttpError) as cm:
                transport.request("POST", url)
        for secret in ("url-secret", "signed-secret", "password", "reason-secret", "reason-sign"):
            self.assertNotIn(secret, str(cm.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
