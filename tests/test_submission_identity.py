#!/usr/bin/env python3
"""模型身份边界：公共合成配置、假凭据、HTTP/文件硬桩，禁止外网。"""
import copy
import importlib
import inspect
import json
import socket
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from mrouter import adapters, transport
from mrouter.adapters import Ctx, GenRequest
from mrouter.config import ConfigError, ModelSpec


class SubmissionCaptured(BaseException):
    pass


class SubmissionIdentityTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name in ("socket", "create_connection", "getaddrinfo"):
            self.stack.enter_context(patch.object(socket, name,
                side_effect=AssertionError("network forbidden")))
        self.stack.enter_context(patch.object(transport, "open_request",
            side_effect=AssertionError("network forbidden")))
        self.stack.enter_context(patch.object(adapters, "read_upload",
            return_value=("fixture.png", b"offline", "image/png")))
        self.tmp = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="mr-identity-"))
        self.ctx = Ctx("offline-access:offline-secret", "offline-fixture", 1, 0, 1)
        self.calls = []
        self.stack.enter_context(patch.object(adapters, "request", side_effect=self.capture))

    def capture(self, method, url, **kwargs):
        self.calls.append((method, url, copy.deepcopy(kwargs)))
        raise SubmissionCaptured()

    def spec(self, provider="openai", kind="image", **kwargs):
        return ModelSpec("configured", provider, kind, model="approved-model",
            endpoint="https://stub.invalid/submit", **kwargs)

    def invoke(self, spec, *, extra=None, images=None, helper=None):
        req = GenRequest(spec.kind, prompt="offline", extra=copy.deepcopy(extra or {}),
                         images=images or [], output_dir=Path(self.tmp))
        before = copy.deepcopy((spec, req))
        try:
            (helper or adapters.ADAPTERS[spec.provider])(spec, req, None, self.ctx)
        finally:
            self.assertEqual((spec, req), before, "policy must not mutate trusted config or request")

    def assert_blocked(self, spec, extra, *, helper=None, images=None):
        self.calls.clear()
        try:
            self.invoke(spec, extra=extra, helper=helper, images=images)
        except ConfigError as exc:
            self.assertNotIn("unapproved-value", str(exc))
        except SubmissionCaptured:
            self.fail("unsafe request reached submission instead of ConfigError")
        else:
            self.fail("unsafe request was not rejected")
        self.assertEqual(self.calls, [])

    def captured(self, spec, **kwargs):
        self.calls.clear()
        with self.assertRaises(SubmissionCaptured):
            self.invoke(spec, **kwargs)
        self.assertEqual(len(self.calls), 1)
        return self.calls[0]

    def policy(self):
        try:
            policy = importlib.import_module("mrouter.policy")
        except ModuleNotFoundError:
            self.fail("missing pure submission policy module")
        return policy

    def test_direct_adapter_model_overrides_are_rejected_before_http(self):
        cases = [("openai", "image"), ("openai", "video"), ("openai", "audio"),
                 ("kling", "image"), ("kling", "video"), ("volcengine", "image"),
                 ("volcengine", "audio"), ("generic_http", "image")]
        for provider, kind in cases:
            for field in ("model", "model_name", "model_id", "version", "resource_id"):
                with self.subTest(provider=provider, kind=kind, field=field):
                    params = {"speaker": "offline-speaker"} if kind == "audio" else {}
                    self.assert_blocked(self.spec(provider, kind, params=params),
                                        {field: "unapproved-value"})
        self.assert_blocked(self.spec(), {"model": "approved-model"}, images=["fixture.png"])

    def test_sensitive_aliases_and_nested_inputs_are_rejected(self):
        fields = ("Model", "MODEL-NAME", "__model__", "input.model_id", "input[model]",
                  "Resource-Id", "api_key", "API-KEY", "authorization", "endpoint",
                  "provider", "access_key", "secret_key", "api_key_env")
        for provider in ("openai", "dashscope", "volcengine", "kling", "replicate", "fal"):
            for field in fields:
                with self.subTest(provider=provider, field=field):
                    self.assert_blocked(self.spec(provider),
                        {"inputs": {"safe": [{field: "unapproved-value"}]}})

    def test_same_identity_value_and_empty_sensitive_values_are_not_silently_stripped(self):
        for value in ("approved-model", "", None, 0, False):
            with self.subTest(value=value):
                self.assert_blocked(self.spec(), {"model": value})

    def test_configuration_model_override_is_preserved(self):
        for provider, field in (("openai", "model"), ("kling", "model_name"),
                                ("volcengine", "model")):
            with self.subTest(provider=provider):
                spec = self.spec(provider, params={field: "approved-config-alias"})
                _, _, kwargs = self.captured(spec, extra={"seed": 0, "watermark": False})
                self.assertEqual(kwargs["json_body"][field], "approved-config-alias")
                self.assertEqual(kwargs["json_body"]["seed"], 0)
                self.assertIs(kwargs["json_body"]["watermark"], False)
        _, _, kwargs = self.captured(self.spec(params={"model": "not-used-for-edits"}),
                                     images=["fixture.png"])
        self.assertEqual(kwargs["form"]["model"], "approved-model")

    def test_volc_tts_uses_trusted_independent_model_and_resource(self):
        spec = self.spec("volcengine", "audio", params={"speaker": "offline-speaker",
            "model": "approved-tts-version", "resource_id": "approved-resource"},
            options={"resource_id": "lower-priority-resource"})
        _, _, kwargs = self.captured(spec)
        self.assertEqual(kwargs["json_body"]["req_params"]["model"], "approved-tts-version")
        self.assertEqual(kwargs["headers"]["X-Api-Resource-Id"], "approved-resource")
        self.assert_blocked(spec, {"resource_id": "approved-resource"})

    def test_regular_parameters_and_inputs_keep_existing_merges(self):
        for provider in ("openai", "dashscope", "volcengine", "kling", "replicate", "fal"):
            with self.subTest(provider=provider):
                extra = {"seed": 0, "watermark": False, "mode": "standard", "n": 2}
                _, _, kwargs = self.captured(self.spec(provider), extra=extra)
                body = kwargs["json_body"]
                merged = body.get("parameters") or body.get("input") or body
                self.assertEqual(merged["seed"], 0)
                self.assertIs(merged["watermark"], False)
        for provider in ("replicate", "fal"):
            _, _, kwargs = self.captured(self.spec(provider),
                extra={"seed": 1, "inputs": {"seed": 0, "watermark": False}})
            inputs = kwargs["json_body"].get("input", kwargs["json_body"])
            self.assertEqual(inputs["seed"], 0)

    def test_generic_extra_requires_explicit_scalar_allowlist(self):
        self.assert_blocked(self.spec("generic_http"), {"engine": "unapproved-value"})
        spec = self.spec("generic_http", options={"allowed_params": ["seed", "watermark", "custom_flag"]})
        _, _, kwargs = self.captured(spec, extra={"seed": 0, "watermark": False, "custom_flag": "ok"})
        self.assertEqual(kwargs["json_body"]["custom_flag"], "ok")
        self.assertEqual(kwargs["json_body"]["seed"], 0)
        self.assertIs(kwargs["json_body"]["watermark"], False)
        for value in ({"engine": "unapproved-value"}, ["unapproved-value"]):
            self.assert_blocked(self.spec("generic_http", options={"allowed_params": ["payload"]}),
                                {"payload": value})

    def test_generic_invalid_allowlists_fail_closed(self):
        for allowed in ("seed", {}, [1], [""], ["model"], ["seed", "seed"]):
            with self.subTest(allowed=allowed):
                self.assert_blocked(self.spec("generic_http", options={"allowed_params": allowed}), {})

    def test_generic_nested_model_template_and_literal_mapping_are_preserved(self):
        options = {"submit": {"body": {"request": {"engine": "prefix-{{ model }}"},
                    "items": [{"selector": "{{model}}"}], "prompt": "{{prompt}}"}},
                   "allowed_params": ["seed"]}
        spec = self.spec("generic_http", options=options)
        _, _, kwargs = self.captured(spec, extra={"seed": 0})
        self.assertEqual(kwargs["json_body"]["request"]["engine"], "prefix-approved-model")
        self.assertEqual(kwargs["json_body"]["items"][0]["selector"], "approved-model")
        literal = self.spec("generic_http", options={"submit": {"body": {"input": {"engine": "fixed-alias"}}},
            "model_fields": ["input.engine"], "allowed_params": ["seed"]})
        _, _, kwargs = self.captured(literal, extra={"seed": 0})
        self.assertEqual(kwargs["json_body"]["input"]["engine"], "fixed-alias")
        self.assert_blocked(literal, {"input": "unapproved-value"})

    def test_generic_template_parent_cannot_become_allowed_mutable_field(self):
        for body in ({"engine": "{{model}}"}, {"input": {"engine": "{{model}}"}},
                     {"items": [{"engine": "prefix-{{model}}"}]}):
            field = next(iter(body))
            with self.subTest(field=field):
                spec = self.spec("generic_http", options={"submit": {"body": body}, "allowed_params": [field]})
                self.assert_blocked(spec, {field: "unapproved-value"})

    def test_generic_model_fields_and_dynamic_identity_fail_closed(self):
        cases = [("input.engine", {"input": {"engine": "fixed"}}),
                 (["missing"], {"engine": "fixed"}), ([1], {"engine": "fixed"}),
                 (["engine", "engine"], {"engine": "fixed"}),
                 (["engine"], {"engine": "{{duration}}"}),
                 ([], {"engine": "{{model}}-{{prompt}}"}),
                 ([], {"model": "{{prompt}}"})]
        for fields, body in cases:
            with self.subTest(fields=fields, body=body):
                self.assert_blocked(self.spec("generic_http", options={"submit": {"body": body},
                    "model_fields": fields}), {})

    def test_generic_only_fixed_or_model_based_submit_url_is_approved(self):
        for variable in ("prompt", "duration", "image", "size", "unknown", "__model__"):
            with self.subTest(variable=variable):
                spec = self.spec("generic_http", options={"submit": {"url":
                    "https://stub.invalid/{{" + variable + "}}"}})
                self.assert_blocked(spec, {})
        spec = self.spec("generic_http", options={"submit": {"url": "https://stub.invalid/{{model}}"}})
        _, url, _ = self.captured(spec)
        self.assertEqual(url, "https://stub.invalid/approved-model")

    def test_submit_identity_is_required_and_precedes_lifecycle_and_http(self):
        parameter = inspect.signature(adapters._submit).parameters.get("identity")
        self.assertIsNotNone(parameter, "submission has no mandatory identity guard")
        self.assertEqual(parameter.default, inspect.Parameter.empty)
        with patch.object(adapters, "submission_started") as started:
            with self.assertRaises(TypeError):
                adapters._submit("POST", "https://stub.invalid/submit")
        started.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_final_guard_rejects_body_form_header_url_method_and_new_alias(self):
        policy = self.policy()
        cases = [({"json_body": {"model": "approved"}}, {"json_body": {"model": "other"}}),
                 ({"form": {"model": "approved"}}, {"form": {}}),
                 ({"headers": {"X-Api-Resource-Id": "approved"}},
                  {"headers": {"x-api-resource-id": "other"}}),
                 ({"json_body": {}}, {"json_body": {"model_id": "other"}}),
                 ({"json_body": {"input": {"model": "approved"}}},
                  {"json_body": {"input": "other"}})]
        for trusted, actual in cases:
            with self.subTest(trusted=trusted):
                identity = policy.SubmissionIdentity.from_config("POST", "https://stub.invalid/submit", **trusted)
                with patch.object(adapters, "submission_started") as started:
                    with self.assertRaises(ConfigError):
                        adapters._submit("POST", "https://stub.invalid/submit", identity=identity, **actual)
                started.assert_not_called()
        identity = policy.SubmissionIdentity.from_config("POST", "https://stub.invalid/submit",
                                                        json_body={"model": "approved"})
        for method, url in (("PUT", "https://stub.invalid/submit"), ("POST", "https://other.invalid/submit")):
            with patch.object(adapters, "submission_started") as started:
                with self.assertRaises(ConfigError):
                    adapters._submit(method, url, identity=identity, json_body={"model": "approved"})
            started.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_final_guard_captures_expected_values_not_references(self):
        policy = self.policy()
        trusted = {"model": "approved"}
        identity = policy.SubmissionIdentity.from_config("POST", "https://stub.invalid/submit", json_body=trusted)
        trusted["model"] = "other"
        with self.assertRaises(ConfigError):
            identity.validate("POST", "https://stub.invalid/submit", {"json_body": trusted})
        identity.validate("post", "https://stub.invalid/submit", {"json_body": {"model": "approved"}})

    def test_query_identity_and_duplicate_header_are_fail_closed(self):
        policy = self.policy()
        identity = policy.SubmissionIdentity.from_config(
            "POST", "https://stub.invalid/submit",
            params={"model": "approved-model", "deployment": "approved-deployment"},
            headers={"X-Model": "approved-model"},
        )
        for tampered in ({"model": "other"}, {}, {"model": "approved-model"},
                         {"deployment": "other"}, {"extra": "approved-model"}):
            with self.subTest(params=tampered):
                with self.assertRaises(ConfigError):
                    identity.validate("POST", "https://stub.invalid/submit",
                                      {"params": tampered,
                                       "headers": {"X-Model": "approved-model"}})
        identity.validate("POST", "https://stub.invalid/submit",
                          {"params": {"model": "approved-model",
                                      "deployment": "approved-deployment",
                                      "timestamp": "1"},
                           "headers": {"x-model": "approved-model"}})
        for headers in ({"X-Model": "approved-model", "x-model": "other"},
                        {"X-Model": "other"}):
            with self.subTest(headers=headers):
                with self.assertRaises(ConfigError):
                    identity.validate("POST", "https://stub.invalid/submit",
                                      {"params": {"model": "approved-model",
                                                  "deployment": "approved-deployment"},
                                       "headers": headers})

    def test_common_model_and_resource_aliases_are_rejected(self):
        policy = self.policy()
        for name in ("engine", "deployment", "deployment_name", "api_base", "base_url",
                     "baseURL", "project", "sku", "signature", "callback_url",
                     "webhook_url", "aimodel", "model2"):
            with self.subTest(name=name):
                with self.assertRaises(ConfigError):
                    policy.validate_request_params({name: "unapproved-value"})

    def test_generic_nested_model_object_template_is_preserved(self):
        options = {"submit": {"body": {"model": {"id": "{{ model }}"}, "prompt": "{{prompt}}"}},
                   "allowed_params": ["seed"]}
        _, _, kwargs = self.captured(self.spec("generic_http", options=options), extra={"seed": 0})
        self.assertEqual(kwargs["json_body"]["model"], {"id": "approved-model"})
        literal = {"submit": {"body": {"model": {"id": "fixed-alias"}}},
                   "model_fields": ["model.id"], "allowed_params": []}
        _, _, kwargs = self.captured(self.spec("generic_http", options=literal))
        self.assertEqual(kwargs["json_body"]["model"], {"id": "fixed-alias"})
        self.assert_blocked(self.spec("generic_http", options={
            "submit": {"body": {"model": {"id": "{{prompt}}"}}}, "allowed_params": []}), {})

    def test_all_fifteen_adapter_submission_sites_supply_working_identity(self):
        cases = [("openai", "image", False), ("openai", "image", True), ("openai", "video", False),
                 ("openai", "audio", False), ("dashscope", "audio", False),
                 ("dashscope", "image", False), ("dashscope", "video", False),
                 ("volcengine", "audio", False), ("volcengine", "image", False), ("volcengine", "video", False),
                 ("kling", "audio", False), ("kling", "image", False), ("replicate", "image", False),
                 ("fal", "image", False), ("generic_http", "image", False)]
        original = adapters._submit
        seen = []
        def verify(method, url, *, identity, **kwargs):
            identity.validate(method, url, kwargs)
            seen.append(identity)
            return original(method, url, identity=identity, **kwargs)
        with patch.object(adapters, "_submit", side_effect=verify):
            for provider, kind, edits in cases:
                with self.subTest(provider=provider, kind=kind, edits=edits):
                    spec = self.spec(provider, kind, params={"speaker": "offline-speaker", "voice_id": "offline-voice"})
                    if provider == "dashscope" and kind == "image":
                        spec.model = "qwen-image-test"
                    self.captured(spec, images=["fixture.png"] if edits else [])
        self.assertEqual(len(seen), 15)


if __name__ == "__main__":
    unittest.main(verbosity=2)
