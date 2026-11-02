"""临时打包与异地 CLI 验收；不依赖用户源库，不写仓库 dist 或安装目录。"""

import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_prompt_assets import check_snapshot

SKILL_ID = "media-router"
LIBRARY = "references/prompts/library"
REQUIRED = {
    "SKILL.md", ".gitignore", "LICENSE", "scripts/media_router.py",
    "scripts/mrouter/prompts.py",
    "references/prompts/index.md",
    "references/prompts/image.md",
    "references/prompts/video.md",
    LIBRARY + "/LICENSE",
    LIBRARY + "/SOURCES.md",
    LIBRARY + "/manifest.json",
    LIBRARY + "/references/docs-prompts/source-notice.md",
    LIBRARY + "/references/docs-prompts/originals/disclaimer.md.txt",
}
# 不导入旧冒烟脚本：它有顶层执行与 sys.exit。这里保持其全部禁止项，并追加新边界。
FORBIDDEN_PARTS = (
    "README", "selftest", "__pycache__", ".pyc", "secrets.web",
    "models.web.yaml", "models.web.yml", "outputs/", "state/", ".git/",
    ".idea/", "tests/", "tools/", "pack/", "dist/",
    ".archify/", "gpt-image-2-style-library/", "host-generation.md",
)


def build_module():
    path = ROOT / "tools/build_pack.py"
    spec = importlib.util.spec_from_file_location("mr_prompt_pack_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def tree_snapshot(root):
    """连目录和已有文件字节一并记录，发现新状态文件或对包的意外改写。"""
    return {
        path.relative_to(root).as_posix(): (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        )
        for path in root.rglob("*")
    }


class PromptPackagingTests(unittest.TestCase):
    def build_packages(self):
        temporary = tempfile.TemporaryDirectory(prefix="mr-prompts-pack-")
        self.addCleanup(temporary.cleanup)
        home = Path(temporary.name)
        builder = build_module()
        builder.DIST_DIR = home / "packages"
        manifest = json.loads(builder.PACK_MANIFEST.read_bytes())
        self.assertEqual(manifest["skills"], [SKILL_ID], "市场清单只注册一个技能")
        files = builder.collect_skill_files(SKILL_ID)
        builder.build_runtime(SKILL_ID, "v0.0.0-test", files)
        with patch.object(sys, "argv", ["build_pack.py"]):
            builder.main()
        runtime = builder.DIST_DIR / SKILL_ID
        runtime_zip = builder.DIST_DIR / (SKILL_ID + "-runtime-v0.0.0-test.zip")
        market_zip = builder.DIST_DIR / (manifest["id"] + "-" + manifest["version"] + ".zip")
        return home, manifest, runtime, runtime_zip, market_zip

    def assert_navigation(self, library, data):
        managed = {"references/index.md", "references/styles.md", "references/templates.md"}
        for category in data["categories"]:
            for key in ("file", "catalog", "template"):
                self.assertTrue((library / category[key]).is_file(), category[key])
            managed.update((category["catalog"], category["template"]))
        checked = 0
        for relative in sorted(managed):
            source = library / relative
            for raw in re.findall(r"\]\(<([^>]+)>\)", source.read_text(encoding="utf-8")):
                if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", raw) or raw.startswith("//"):
                    continue
                checked += 1
                file_part, _, anchor = raw.partition("#")
                target = (source.parent / file_part if file_part else source).resolve()
                target.relative_to(library.resolve())
                self.assertTrue(target.is_file(), (relative, raw))
                if not anchor:
                    continue
                text = target.read_text(encoding="utf-8")
                line_anchor = re.fullmatch(r"L([1-9]\d*)(?:-L([1-9]\d*))?", anchor)
                if line_anchor:
                    start = int(line_anchor.group(1))
                    end = int(line_anchor.group(2) or start)
                    self.assertLessEqual(start, end)
                    self.assertLessEqual(end, len(text.splitlines()))
                else:
                    explicit = re.search(r'(?:id|name)=["\']' + re.escape(anchor) + r'["\']', text)
                    headings = {
                        re.sub(r"[^\w\- ]", "", title.strip().lower()).replace(" ", "-")
                        for title in re.findall(r"^#{1,6}\s+(.+)$", text, re.M)
                    }
                    self.assertTrue(explicit or anchor in headings, (relative, raw))
        self.assertGreater(checked, 0, "干净导出／包中必须实际校验关键导航")

    def assert_skill_complete(self, skill, package_license=None):
        relative = {path.relative_to(skill).as_posix() for path in skill.rglob("*") if path.is_file()}
        # runtime 的 LICENSE 在技能根；既有市场包格式将同一许可放在 zip 根。
        required = REQUIRED if package_license is None else REQUIRED - {"LICENSE"}
        license_path = skill / "LICENSE" if package_license is None else package_license
        self.assertTrue(license_path.is_file(), "缺少项目 LICENSE")
        self.assertEqual(license_path.read_bytes(), (ROOT / "LICENSE").read_bytes())
        self.assertFalse(required - relative, "缺少运行时资源：%s" % sorted(required - relative))
        self.assertEqual(sorted(name for name in relative if Path(name).name == "SKILL.md"), ["SKILL.md"])
        self.assertFalse(
            [(name, part) for name in sorted(relative) for part in FORBIDDEN_PARTS if part in name],
            "包中不允许开发文件、私人配置、缓存或上游技能入口",
        )
        data = check_snapshot(skill / LIBRARY)
        self.assert_navigation(skill / LIBRARY, data)
        return data

    def assert_cli_portable_and_read_only(self, home, skill, data):
        cwd = home / "unrelated-cwd"
        cwd.mkdir()
        config = home / "never-create-config"
        state = home / "never-create-state"
        env = os.environ.copy()
        # 不让开发者环境或用户 site 把缺失的包内模块偷偷补齐。
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["PYTHONUTF8"] = "1"
        env["MEDIA_ROUTER_CONFIG_DIR"] = str(config)
        env["MEDIA_ROUTER_STATE_DIR"] = str(state)
        before = tree_snapshot(home)

        def invoke(arguments):
            completed = subprocess.run(
                [sys.executable, "-B", "-s", str(skill / "scripts/media_router.py")] + arguments,
                cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                encoding="utf-8", timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout[:2000])
            self.assertEqual(completed.stderr, "", "正常离线命令不应输出诊断或警告到 stderr")
            try:
                result = json.loads(completed.stdout)
            except ValueError as exc:
                self.fail("stdout 必须是单个 JSON 对象：%s" % exc)
            self.assertIsInstance(result, dict)
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["data_kind"], "reference_data")
            self.assertEqual(
                result["library_version"],
                hashlib.sha256((skill / LIBRARY / "manifest.json").read_bytes()).hexdigest(),
            )
            return result

        image = invoke(["prompts", "search", "--kind", "image", "--query", "全自动咖啡机"])
        self.assertEqual(image["command"], "prompts.search")
        self.assertTrue(image["matched"])
        self.assertEqual(image["cases"][0]["id"], 190)
        self.assertEqual(image["guide_path"], "references/prompts/image.md")
        video = invoke(["prompts", "search", "--kind", "video", "--query", "场景 叙事"])
        self.assertTrue(video["matched"])
        self.assertEqual(video["guide_path"], "references/prompts/video.md")
        for result in (image, video):
            self.assertTrue((skill / result["guide_path"]).is_file())
            for category in result["categories"]:
                self.assertTrue((skill / category["template"]).is_file())
            for case in result["cases"]:
                self.assertTrue((skill / case["file"]).is_file())
                self.assertTrue((skill / case["catalog"]).is_file())

        category = invoke(["prompts", "show", "--category", "product"])
        self.assertEqual(category["command"], "prompts.show")
        self.assertEqual(category["type"], "category")
        product = next(item for item in data["categories"] if item["slug"] == "product")
        self.assertEqual(category["file"], LIBRARY + "/" + product["template"])
        self.assertEqual(category["text"], (skill / category["file"]).read_bytes().decode("utf-8"))
        for case_id in (190, 335):
            result = invoke(["prompts", "show", "--id", str(case_id)])
            self.assertEqual(result["command"], "prompts.show")
            self.assertEqual(result["id"], case_id)
            entry = next(item for item in data["cases"] if item["id"] == case_id)
            if entry["promptStatus"] == "provided":
                prompt = result["prompt"]
                self.assertIsNone(result["goal_description"])
            else:
                self.assertIsNone(result["prompt"])
                self.assertTrue(result["warnings"])
                prompt = result["goal_description"]
            self.assertTrue(prompt)
            self.assertEqual(hashlib.sha256(prompt.encode("utf-8")).hexdigest(), entry["promptSha256"])
            self.assertEqual(result["prompt_sha256"], entry["promptSha256"])

        self.assertFalse(config.exists(), "只读检索不创建配置目录")
        self.assertFalse(state.exists(), "只读检索不创建状态目录")
        self.assertFalse((cwd / "outputs").exists())
        self.assertFalse((skill / "outputs").exists())
        self.assertEqual(tree_snapshot(home), before, "-B 下真实 search/show 不得新增或改写临时文件")

    def test_runtime_directory_and_zip_are_complete_and_portable(self):
        home, _, runtime, runtime_zip, _ = self.build_packages()
        self.assert_skill_complete(runtime)
        self.assertTrue(runtime_zip.is_file())
        with zipfile.ZipFile(runtime_zip) as package:
            names = package.namelist()
            self.assertEqual(len(names), len(set(names)), "zip 不得包含重复条目")
            expected = {
                SKILL_ID + "/" + path.relative_to(runtime).as_posix(): path
                for path in runtime.rglob("*") if path.is_file()
            }
            self.assertEqual(set(names), set(expected), "runtime zip 与运行时目录逐文件对应")
            for name, path in expected.items():
                self.assertEqual(package.read(name), path.read_bytes(), name)
            extracted = home / "runtime-extract"
            package.extractall(extracted)
        installed = extracted / SKILL_ID
        data = self.assert_skill_complete(installed)
        self.assert_cli_portable_and_read_only(home, installed, data)

    def test_market_pack_is_complete_and_portable(self):
        home, manifest, runtime, _, market_zip = self.build_packages()
        self.assertTrue(market_zip.is_file())
        with zipfile.ZipFile(market_zip) as package:
            names = package.namelist()
            self.assertEqual(len(names), len(set(names)), "市场包不得包含重复条目")
            prefix = "skills/" + SKILL_ID + "/"
            expected = {
                prefix + path.relative_to(runtime).as_posix(): path
                for path in runtime.rglob("*") if path.is_file() and path.relative_to(runtime).as_posix() != "LICENSE"
            }
            self.assertEqual(set(names), {"manifest.json", "LICENSE"} | set(expected))
            self.assertEqual(json.loads(package.read("manifest.json")), manifest)
            self.assertEqual(package.read("LICENSE"), (ROOT / "LICENSE").read_bytes())
            self.assertEqual(
                sorted(name for name in names if Path(name).name == "SKILL.md"),
                [prefix + "SKILL.md"],
            )
            self.assertFalse([name for name in names for part in FORBIDDEN_PARTS if part in name])
            for name, path in expected.items():
                self.assertEqual(package.read(name), path.read_bytes(), name)
            extracted = home / "market-extract"
            package.extractall(extracted)
        installed = extracted / "skills" / SKILL_ID
        data = self.assert_skill_complete(installed, package_license=extracted / "LICENSE")
        self.assert_cli_portable_and_read_only(home, installed, data)

    def test_clean_git_export_preserves_snapshot_and_navigation(self):
        with tempfile.TemporaryDirectory(prefix="mr-prompts-export-") as tmp:
            home = Path(tmp)
            archive = home / "source.zip"
            completed = subprocess.run(
                ["git", "archive", "--format=zip", "--output=" + str(archive), "HEAD"],
                cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                encoding="utf-8", timeout=30,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            with zipfile.ZipFile(archive) as package:
                # 只解压本次构建的本地 HEAD，不接受外部压缩包。
                source = home / "source"
                package.extractall(source)
            library = source / LIBRARY
            data = check_snapshot(library)
            self.assert_navigation(library, data)
            attributes = source / ".gitattributes"
            self.assertTrue(attributes.is_file(), "HEAD 缺少资料字节保护规则；需先提交 Task1")
            rules = [line.strip() for line in attributes.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(rules, ["references/prompts/library/** -text"])


if __name__ == "__main__":
    unittest.main()
