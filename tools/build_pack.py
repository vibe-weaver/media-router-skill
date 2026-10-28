#!/usr/bin/env python3
"""把仓库打包成可直接安装的运行时目录，或 DSH 插件市场要求的 pack zip。

用法：

    python3 tools/build_pack.py             # 插件市场 pack zip
    python3 tools/build_pack.py --runtime   # 干净运行时目录 + 运行时 zip

--runtime 生成的 dist/media-router/ 只包含技能真正需要的东西（SKILL.md、scripts/、
config/、references/、assets/、LICENSE），可以直接整个复制到 agent 的 skills 目录；
README、测试、outputs、dist、.git 等开发文件不会进去。

插件市场 pack zip 的内部布局：

    manifest.json
    LICENSE
    skills/<skill-id>/SKILL.md
    skills/<skill-id>/scripts/...
    ...
"""

import argparse
import json
import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACK_MANIFEST = ROOT / "pack" / "manifest.json"
DIST_DIR = ROOT / "dist"

# 放进 skills/<id>/ 的内容（白名单；没列进来的一律不进包）
# .gitignore 也要进包：config/secrets.example.yaml 里写着「已在 .gitignore 里」，
# 装完技能后用户如果把这个目录纳进自己的 git，得有一份忽略规则兜着。
SKILL_FILES = ["SKILL.md", ".gitignore"]
SKILL_DIRS = ["scripts", "config", "references", "assets"]

# 遍历时整个跳过的目录名
SKIP_DIR_NAMES = {"__pycache__", "state", "outputs", ".git", ".idea", ".workbuddy"}

# 只服务于开发/自检的文件，不进运行时包
SKIP_SKILL_FILES = {
    "scripts/selftest.py",
    "scripts/selftest_web.py",
}

# 密钥守卫：命中的文件不进包。带 .example. 的模板文件例外，它们是给用户看的样板。
SECRET_PATTERNS = [
    re.compile(r"^secrets", re.I),
    re.compile(r"^models\.web\.(ya?ml|json)$", re.I),
    re.compile(r"\.local-backup", re.I),
    re.compile(r"\.bak", re.I),
    re.compile(r"\.orig$", re.I),
    re.compile(r"\.rej$", re.I),
    re.compile(r"\.(key|pem|p12|pfx)$", re.I),
    re.compile(r"^\.env", re.I),
]


def is_secret_like(name):
    """文件名看着像密钥载体就返回 True。模板（.example.）不算。"""
    if ".example." in name.lower():
        return False
    return any(p.search(name) for p in SECRET_PATTERNS)


def collect_skill_files(skill_dir):
    """收集要放进 skills/<id>/ 的文件，返回 [(绝对路径, zip 内相对 skills/<id>/ 的路径)]。"""
    out = []
    for name in SKILL_FILES:
        p = ROOT / name
        if not p.is_file():
            raise SystemExit("缺少必需文件：" + name)
        out.append((p, name))

    for dirname in SKILL_DIRS:
        base = ROOT / dirname
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(ROOT)
            if any(part in SKIP_DIR_NAMES for part in rel.parts):
                continue
            rel_posix = str(rel).replace("\\", "/")
            if rel_posix in SKIP_SKILL_FILES:
                continue
            if is_secret_like(p.name):
                print("  跳过（密钥守卫）：" + rel_posix)
                continue
            out.append((p, rel_posix))

    return out


def build_runtime(skill_id, version, skill_files):
    """生成可直接复制到 skills 目录的干净运行时目录和 zip。"""
    DIST_DIR.mkdir(exist_ok=True)
    target = DIST_DIR / skill_id
    if target.exists():
        shutil.rmtree(target)

    for src, rel in skill_files:
        dest = target / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    license_file = ROOT / "LICENSE"
    if license_file.is_file():
        shutil.copy2(license_file, target / "LICENSE")

    zip_path = DIST_DIR / (skill_id + "-runtime-" + version + ".zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(target.rglob("*")):
            if p.is_file():
                arc = str(Path(skill_id) / p.relative_to(target)).replace("\\", "/")
                zf.write(p, arc)

    files = [p for p in target.rglob("*") if p.is_file()]
    total_kb = sum(p.stat().st_size for p in files) / 1024
    print("已生成运行时目录：" + str(target))
    print("已生成运行时 zip：" + str(zip_path))
    print("  文件 " + str(len(files)) + " 个，约 " + ("%.1f" % total_kb) + " KB")
    print("")
    print("安装：把 " + str(target) + " 整个复制到 <skills>/" + skill_id + "/ 即可。")
    print("不要直接复制仓库根目录（含 README、测试、outputs、dist、.git 等开发文件）。")


def main():
    parser = argparse.ArgumentParser(description="构建 media-router 运行时目录 / 插件市场 pack")
    parser.add_argument("--runtime", action="store_true",
                        help="生成可直接复制到 skills 目录的干净运行时目录和 zip")
    args = parser.parse_args()

    if not PACK_MANIFEST.is_file():
        raise SystemExit("找不到 " + str(PACK_MANIFEST))

    manifest = json.loads(PACK_MANIFEST.read_text(encoding="utf-8"))
    pack_id = manifest["id"]
    version = manifest["version"]
    skills = manifest.get("skills") or []
    if not skills:
        raise SystemExit("pack/manifest.json 的 skills 为空，包内不会有任何 skill")

    skill_id = skills[0]
    skill_files = collect_skill_files(skill_id)

    if args.runtime:
        build_runtime(skill_id, version, skill_files)
        return

    DIST_DIR.mkdir(exist_ok=True)
    zip_path = DIST_DIR / (pack_id + "-" + version + ".zip")

    written = []
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(PACK_MANIFEST, "manifest.json")
        written.append("manifest.json")

        license_file = ROOT / "LICENSE"
        if license_file.is_file():
            zf.write(license_file, "LICENSE")
            written.append("LICENSE")

        for src, rel in skill_files:
            arc = "skills/" + skill_id + "/" + rel
            zf.write(src, arc)
            written.append(arc)

    # 自检：模拟市场客户端的校验，别把坏包传上去
    problems = []
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        if "manifest.json" not in names:
            problems.append("zip 根目录缺少 manifest.json（市场会拒绝安装）")
        if "skills/" + skill_id + "/SKILL.md" not in names:
            problems.append("缺少 skills/" + skill_id + "/SKILL.md")
        for n in names:
            if is_secret_like(Path(n).name):
                problems.append("包里混进了疑似密钥文件：" + n)

    if problems:
        zip_path.unlink()
        for p in problems:
            print("自检失败：" + p, file=sys.stderr)
        raise SystemExit(1)

    size_kb = zip_path.stat().st_size / 1024
    print("已生成：" + str(zip_path))
    print("  条目 " + str(len(written)) + " 个，" + ("%.1f" % size_kb) + " KB")
    print("  id=" + pack_id + "  version=" + version + "  skill=" + skill_id)
    print("  不含 README、测试、outputs、state 等开发文件")
    print("")
    print("挂到 Release 资产：")
    print("  gh release upload " + version + " \"" + str(zip_path) + "\" --clobber")


if __name__ == "__main__":
    main()
