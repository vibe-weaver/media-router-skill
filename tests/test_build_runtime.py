#!/usr/bin/env python3
"""运行时包冒烟测试：不写仓库 dist，不启动服务。

运行：python tests/test_build_runtime.py
"""

import importlib.util
import tempfile
import zipfile
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent

passed = 0
failed = 0


def check(cond: bool, message: str) -> None:
    global passed, failed
    if cond:
        passed += 1
        print("  [PASS] " + message)
    else:
        failed += 1
        print("  [FAIL] " + message)


def load_build_pack():
    path = ROOT / "tools" / "build_pack.py"
    spec = importlib.util.spec_from_file_location("build_pack_smoke", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


REQUIRED = {
    "SKILL.md",
    "LICENSE",
    "scripts/media_router.py",
    "scripts/mrouter/config.py",
    "scripts/mrouter/cli.py",
    "references/setup.md",
    "references/providers.md",
    "assets/config.html",
    "config/models.yaml",
}
FORBIDDEN_PARTS = (
    "README",
    "selftest",
    "__pycache__",
    ".pyc",
    "secrets.web",
    "models.web.yaml",
    "models.web.yml",
    "outputs/",
    "state/",
    ".git/",
    ".idea/",
    "tests/",
    "tools/",
    "pack/",
    "dist/",
)

print("运行时包冒烟测试")
bp = load_build_pack()

with tempfile.TemporaryDirectory(prefix="mr-build-test-") as tmp:
    bp.DIST_DIR = Path(tmp) / "dist"
    files = bp.collect_skill_files("media-router")
    bp.build_runtime("media-router", "v0.0.0-test", files)

    target = bp.DIST_DIR / "media-router"
    rel = {
        p.relative_to(target).as_posix()
        for p in target.rglob("*")
        if p.is_file()
    }

    check((target / "SKILL.md").is_file(), "生成 SKILL.md")
    check(REQUIRED.issubset(rel), "包含全部运行时必需文件")
    check(not any(part in name for name in rel for part in FORBIDDEN_PARTS),
          "没有 README / 测试 / 密钥 / 缓存 / outputs")
    check("scripts/selftest.py" not in rel and "scripts/selftest_web.py" not in rel,
          "开发自检脚本没有进包")

    zip_path = bp.DIST_DIR / "media-router-runtime-v0.0.0-test.zip"
    check(zip_path.is_file(), "生成运行时 zip")
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        check(all(n.startswith("media-router/") for n in names), "zip 内统一在 media-router/ 下")
        check("media-router/SKILL.md" in names, "zip 含 SKILL.md")
        check(not any(part in name for name in names for part in FORBIDDEN_PARTS),
              "zip 内没有开发/密钥文件")
        check("快速路径" in zf.read("media-router/SKILL.md").decode("utf-8"),
              "包内 SKILL.md 是精简版")

    # 密钥防线有两条：git 侧靠 .gitignore，打包侧靠 SECRET_PATTERNS。
    # 前者以前只存在于文档承诺里（八处注释都写"已在 .gitignore 里"，仓库里却没有这个文件），
    # 真被删掉也没人会发现 —— 所以在这里钉死。
    root_ignore = ROOT / ".gitignore"
    check(root_ignore.is_file(), "仓库根有 .gitignore")
    ignore_text = root_ignore.read_text(encoding="utf-8") if root_ignore.is_file() else ""
    check("config/secrets.web.yaml" in ignore_text and "config/secrets.yaml" in ignore_text,
          ".gitignore 覆盖了明文密钥文件")
    check("config/models.web.yaml" in ignore_text, ".gitignore 覆盖了页面写出的本机配置")
    check("media-router/.gitignore" in names, "zip 里带 .gitignore（装完也能防误提交）")

print(f"\n通过 {passed} 项，失败 {failed} 项")
sys.exit(1 if failed else 0)
