"""提示词 CLI 的真实控制流、参考正文保真及离线隔离测试。"""

import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from mrouter import adapters, cli, config, health, transport


class CliTestCase(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        forbidden = AssertionError("prompts 不应读取配置、密钥、状态或调用网络／生成")
        for owner, name in ((cli, "load_config"), (config, "load_config"),
                            (config.RouterConfig, "resolve_api_key"),
                            (cli, "HealthStore"), (health, "HealthStore"),
                            (cli, "run_adapter"), (adapters, "run_adapter"),
                            (transport, "request"), (transport, "open_request")):
            stack.enter_context(patch.object(owner, name, side_effect=forbidden))
        for name in ("socket.create_connection", "socket.socket.connect", "socket.getaddrinfo",
                     "urllib.request.urlopen", "urllib.request.OpenerDirector.open"):
            stack.enter_context(patch(name, side_effect=forbidden))

    def invoke(self, argv):
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = cli.main(argv)
            except SystemExit as exc:
                rc = exc.code
        raw = out.getvalue()
        result = json.loads(raw)
        self.assertIsInstance(result, dict)
        return rc, result, raw, err.getvalue()

    def parse(self, argv):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            try:
                return cli.build_parser().parse_args(argv)
            except SystemExit as exc:
                self.fail("有效 prompts 参数应可解析，退出码为 %s：%r" % (exc.code, argv))


class PromptsArgumentTests(CliTestCase):
    def test_search_parser_accepts_defaults_and_limit_boundaries(self):
        base = ["prompts", "search", "--kind", "image", "--query", "商品 极简"]
        args = self.parse(base)
        self.assertEqual((args.command, args.prompt_command, args.kind, args.query),
                         ("prompts", "search", "image", "商品 极简"))
        self.assertEqual((args.limit, args.category, args.pretty), (3, None, False))
        self.assertEqual(args.func.__name__, "cmd_prompts")
        for limit in (1, 5):
            args = self.parse(base + ["--limit", str(limit), "--category", "product"])
            self.assertEqual((args.limit, args.category), (limit, "product"))
        self.assertEqual(self.parse(["prompts", "search", "--kind", "video", "--query", "场景"]).kind,
                         "video")

    def test_show_parser_accepts_exactly_one_selector(self):
        args = self.parse(["prompts", "show", "--id", "190"])
        self.assertEqual((args.case_id, args.category, args.func.__name__), (190, None, "cmd_prompts"))
        args = self.parse(["prompts", "show", "--category", "product"])
        self.assertEqual((args.case_id, args.category), (None, "product"))

    def test_pretty_is_not_shadowed_by_nested_parser_defaults(self):
        for command in (["search", "--kind", "image", "--query", "商品"],
                        ["show", "--id", "190"]):
            for argv in (["--pretty", "prompts"] + command,
                         ["prompts", "--pretty"] + command,
                         ["prompts"] + command + ["--pretty"],
                         ["--pretty", "prompts", "--pretty"] + command + ["--pretty"]):
                with self.subTest(argv=argv):
                    self.assertIs(self.parse(argv).pretty, True)

    def test_search_parameter_errors_keep_config_json_and_validate_before_library(self):
        base = ["prompts", "search"]
        valid = base + ["--kind", "image", "--query", "x"]
        cases = ((base + ["--query", "x"], "--kind"),
                 (base + ["--kind", "image"], "--query"),
                 (base + ["--kind", "audio", "--query", "x"], "audio"),
                 (base + ["--kind", "image", "--query", " \t\n"], "--query"),
                 (valid + ["--limit", "0"], "--limit"),
                 (valid + ["--limit", "6"], "--limit"),
                 (valid + ["--limit", "one"], "--limit"))
        for argv, expected in cases:
            with self.subTest(argv=argv):
                rc, result, raw, err = self.invoke(argv)
                self.assertEqual((rc, result["status"], result["kind"]), (1, "error", "config"))
                self.assertIn(expected, result["error"])
                self.assertNotIn("code", result)

    def test_show_and_nested_command_errors_keep_existing_contract(self):
        for argv, expected in ((["prompts"], "prompt_command"),
                               (["prompts", "show"], "--id"),
                               (["prompts", "show", "--id", "1", "--category", "ui"], "--category"),
                               (["prompts", "show", "--id", "not-an-int"], "--id")):
            with self.subTest(argv=argv):
                rc, result, raw, err = self.invoke(argv)
                self.assertEqual((rc, result["kind"]), (1, "config"))
                self.assertIn(expected, result["error"])

    def test_external_library_configuration_is_not_registered(self):
        valid = ["prompts", "search", "--kind", "image", "--query", "x"]
        for option in ("--root", "--library-root", "--config", "--api-key", "--endpoint"):
            with self.subTest(option=option):
                rc, result, raw, err = self.invoke(valid + [option, "not-used"])
                self.assertEqual((rc, result["kind"]), (1, "config"))
                self.assertIn(option, result["error"])

    def test_guide_adds_prompts_without_loading_library(self):
        rc, result, raw, err = self.invoke([])
        self.assertEqual((rc, result["status"]), (0, "help"))
        self.assertIn("prompts", result["commands"])
        self.assertTrue(any("prompts search --kind image" in v for v in result["examples"]))
        self.assertIn("prompts", cli.build_parser().format_help())


class PromptsCliTests(CliTestCase):
    def fixture(self, prompt="公开商品参考", template="商品模板"):
        """创建真实最小清单／正文；不替换 PromptLibrary 的任何成功返回。"""
        tmp = tempfile.TemporaryDirectory(prefix="mr-prompts-cli-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name) / "skill/references/prompts/library"
        for name in ("cases", "catalogs", "templates"):
            (root / "references" / name).mkdir(parents=True, exist_ok=True)
        body = ('<a id="case-190"></a>\r\n# 190 公开商品参考\r\n'
                '- 原来源署名：公开示例\r\n'
                '- 原来源 URL：https://example.invalid/reference\r\n'
                '<!-- prompt:start -->\r\n```text\r\n').encode("utf-8")
        body += prompt.encode("utf-8") + b"\r\n```\r\n<!-- prompt:end -->\r\n"
        end_line = len(body.splitlines())
        category = {"source": "Products & E-commerce", "label": "商品与电商", "slug": "product",
                    "anchor": "tpl-product", "count": 1, "file": "references/cases/product.md",
                    "catalog": "references/catalogs/product.md", "template": "references/templates/product.md",
                    "keywords": "商品 product"}
        case = {"id": 190, "title": "公开商品参考", "category": category["source"],
                "file": category["file"], "catalog": category["catalog"], "startLine": 1,
                "endLine": end_line, "promptStatus": "provided", "sourceUrlAvailable": True,
                "requiresUserMaterials": False, "samePromptAs": [], "quality": "源数据提供正文",
                "promptSha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()}
        data = {"name": "public-test-reference", "formatVersion": 1, "totalCases": 1,
                "uniquePromptTexts": 1, "unpublishedPromptCases": 0,
                "categories": [category], "cases": [case]}
        (root / "manifest.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        (root / category["file"]).write_bytes(body)
        (root / category["template"]).write_bytes(template.encode("utf-8"))
        row = '| [190](<../cases/product.md#case-190>) | 公开商品参考 | Product | Commerce | 源数据提供正文 | 1–%d |\n' % end_line
        (root / category["catalog"]).write_text(row, encoding="utf-8")
        return root

    def test_real_commands_work_without_config_keys_network_generation_or_health(self):
        for argv, command in ((["prompts", "search", "--kind", "image", "--query", "全自动咖啡机"], "prompts.search"),
                              (["prompts", "search", "--kind", "video", "--query", "场景 叙事"], "prompts.search"),
                              (["prompts", "show", "--id", "190"], "prompts.show"),
                              (["prompts", "show", "--category", "product"], "prompts.show")):
            with self.subTest(argv=argv):
                rc, result, raw, err = self.invoke(argv)
                self.assertEqual((rc, result["status"], result.get("command")), (0, "ok", command))
                self.assertEqual(result["data_kind"], "reference_data")
                self.assertEqual(err, "")
                if result.get("type") == "case":
                    self.assertEqual(hashlib.sha256(result["prompt"].encode("utf-8")).hexdigest(),
                                     result["prompt_sha256"])

    def test_zero_match_is_success_without_query_echo(self):
        query = "private-unmatched-sample-971"
        rc, result, raw, err = self.invoke(["prompts", "search", "--kind", "image", "--query", query])
        self.assertEqual((rc, result["status"], result.get("matched")), (0, "ok", False))
        self.assertEqual((result["categories"], result["cases"]), ([], []))
        self.assertNotIn(query, raw)

    def test_pretty_output_at_prefix_middle_and_suffix(self):
        for command in (["search", "--kind", "image", "--query", "商品"], ["show", "--id", "190"]):
            for argv in (["--pretty", "prompts"] + command,
                         ["prompts", "--pretty"] + command,
                         ["prompts"] + command + ["--pretty"]):
                with self.subTest(argv=argv):
                    rc, result, raw, err = self.invoke(argv)
                    self.assertEqual((rc, result["status"]), (0, "ok"))
                    self.assertIn('\n  "status": "ok"', raw)

    def test_real_reference_prompt_and_template_bypass_vendor_redaction_losslessly(self):
        from mrouter import prompts
        text = ('公开示例 Ａ 中文 😀 "引号"\\路径\t末尾空格  \r\n'
                'https://example.invalid/demo?key=public-example&token=public-reference&signature=public-signature\n'
                'http://demo:reference@example.invalid/\r\n{"example": true}')
        self.assertNotEqual(cli.redact(text), text)
        root = self.fixture(prompt=text, template=text)
        with patch.object(prompts, "DEFAULT_ROOT", root):
            rc, result, raw, err = self.invoke(["prompts", "show", "--id", "190"])
            self.assertEqual((rc, result["prompt"]), (0, text))
            self.assertEqual(hashlib.sha256(result["prompt"].encode("utf-8")).hexdigest(), result["prompt_sha256"])
            rc, result, raw, err = self.invoke(["prompts", "show", "--category", "product"])
            self.assertEqual((rc, result["text"]), (0, text))

    def test_real_library_errors_are_distinct_and_do_not_echo_query(self):
        from mrouter import prompts
        query = "private-sample-words"
        root = self.fixture()
        for argv, code in ((["show", "--id", "9999"], "case_not_found"),
                           (["show", "--id", "0"], "case_not_found"),
                           (["show", "--id", "-1"], "case_not_found"),
                           (["show", "--category", "unknown"], "category_not_found"),
                           (["search", "--kind", "image", "--query", query, "--category", "unknown"], "category_not_found")):
            with self.subTest(code=code), patch.object(prompts, "DEFAULT_ROOT", root):
                rc, result, raw, err = self.invoke(["prompts"] + argv)
                self.assertEqual((rc, result["kind"], result["code"]), (1, "prompt_library", code))
                self.assertNotIn(query, raw)
        for target, code in ((root / "missing", "library_unavailable"), (root, "library_corrupt")):
            if target == root:
                (root / "manifest.json").write_bytes(b"{")
            with self.subTest(code=code), patch.object(prompts, "DEFAULT_ROOT", target):
                rc, result, raw, err = self.invoke(["prompts", "search", "--kind", "image", "--query", query])
                self.assertEqual((rc, result["kind"], result["code"]), (1, "prompt_library", code))
                self.assertNotIn(query, raw)
                self.assertNotIn("matched", result)

    def test_library_error_text_still_uses_existing_redaction(self):
        from mrouter import prompts
        root = self.fixture()
        error = prompts.PromptLibraryError("library_corrupt", "参考资料失败 https://example.invalid/?token=error-secret&signature=error-signature")
        with patch.object(prompts, "DEFAULT_ROOT", root), patch.object(prompts.PromptLibrary, "search", side_effect=error):
            rc, result, raw, err = self.invoke(["prompts", "search", "--kind", "image", "--query", "x", "--pretty"])
        self.assertEqual((rc, result["kind"], result["code"]), (1, "prompt_library", "library_corrupt"))
        self.assertNotIn("error-secret", raw)
        self.assertNotIn("error-signature", raw)
        self.assertIn("***", result["error"])
        self.assertIn('\n  "status"', raw)


ISOLATED_WORKER = r'''
import io
import json
import os
import sys
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

mode = sys.argv[2]
sys.path.insert(0, sys.argv[1])
blocked_roots = [os.environ[name].replace("\\", "/").lower() for name in
                 ("MEDIA_ROUTER_CONFIG_DIR", "MEDIA_ROUTER_STATE_DIR", "MEDIA_ROUTER_OUTPUT_DIR")]
def audit(event, args):
    if event == "import" and mode == "import" and args[0] == "mrouter.prompts":
        raise AssertionError("CLI import / old parsing must not import prompts")
    if event == "open":
        path = str(args[0]).replace("\\", "/").lower()
        if any(path == root or path.startswith(root + "/") for root in blocked_roots):
            raise AssertionError("config / state / outputs must not be read")
        if mode == "import" and "/references/prompts/library/" in path:
            raise AssertionError("CLI import / old parsing must not read library")
        if args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC):
            raise AssertionError("read-only CLI must not write files")
    if event in ("os.mkdir", "os.remove", "os.rmdir", "os.rename", "subprocess.Popen",
                 "socket.connect", "socket.getaddrinfo", "socket.bind"):
        raise AssertionError("unexpected side effect: " + event)
sys.addaudithook(audit)
from mrouter import adapters, cli, config, health, transport
if mode == "import":
    assert "mrouter.prompts" not in sys.modules
    parser = cli.build_parser()
    assert parser.parse_args(["generate", "--kind", "image", "--prompt", "unchanged"]).prompt == "unchanged"
    assert parser.parse_args(["generate", "--kind", "audio", "--prompt", "tts"]).kind == "audio"
    assert parser.parse_args(["web", "--no-browser"]).func is cli.cmd_web
    assert "mrouter.prompts" not in sys.modules
else:
    with ExitStack() as stack:
        for owner, name in ((cli, "load_config"), (config, "load_config"),
                            (config.RouterConfig, "resolve_api_key"), (cli, "HealthStore"),
                            (health, "HealthStore"), (cli, "run_adapter"), (adapters, "run_adapter"),
                            (transport, "request"), (transport, "open_request")):
            stack.enter_context(patch.object(owner, name, side_effect=AssertionError("forbidden generation/config path")))
        for argv in (["prompts", "search", "--kind", "image", "--query", "商品"],
                     ["prompts", "search", "--kind", "video", "--query", "场景"],
                     ["prompts", "show", "--id", "190"],
                     ["prompts", "show", "--category", "product"]):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = cli.main(argv)
            result = json.loads(out.getvalue())
            assert (rc, result["status"]) == (0, "ok"), result
            assert not err.getvalue(), err.getvalue()
print(json.dumps({"status": "ok", "mode": mode}))
'''


class PromptsProcessIsolationTests(unittest.TestCase):
    def run_isolated(self, mode):
        with tempfile.TemporaryDirectory(prefix="mr-prompts-process-") as tmp:
            root = Path(tmp)
            work = root / "work"
            work.mkdir()
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env["PYTHONDONTWRITEBYTECODE"] = "1"
            for name, directory in (("MEDIA_ROUTER_CONFIG_DIR", "config"),
                                    ("MEDIA_ROUTER_STATE_DIR", "state"),
                                    ("MEDIA_ROUTER_OUTPUT_DIR", "outputs")):
                env[name] = str(root / directory)
            # 捕获文件放在 cwd／受测配置与状态目录外，受测进程禁止所有写操作。
            with (root / "stdout.txt").open("w", encoding="utf-8") as out, (root / "stderr.txt").open("w", encoding="utf-8") as err:
                process = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", ISOLATED_WORKER,
                                          str(ROOT / "scripts"), mode], cwd=str(work), env=env,
                                         stdout=out, stderr=err, timeout=30)
            stderr = (root / "stderr.txt").read_text(encoding="utf-8")
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(json.loads((root / "stdout.txt").read_text(encoding="utf-8")),
                             {"status": "ok", "mode": mode})
            self.assertEqual(list(work.iterdir()), [])
            for name in ("config", "state", "outputs"):
                self.assertFalse((root / name).exists(), name)

    def test_cli_import_and_old_generate_audio_web_parse_do_not_load_library(self):
        self.run_isolated("import")

    def test_real_commands_from_foreign_cwd_are_read_only_and_offline(self):
        self.run_isolated("commands")


if __name__ == "__main__":
    unittest.main()
