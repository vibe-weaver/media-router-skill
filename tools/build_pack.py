#!/usr/bin/env python3
"""把仓库打包成 DSH 插件市场要求的 pack zip。

市场客户端（dsh-plugin-market 的 src/host.js）安装时的硬性校验：
解压后**根目录必须有 manifest.json**，随后按 manifest.skills[] 里的名字，
把 skills/<名字>/ 整个复制到 ~/.agents/skills/<名字>/。

所以 zip 内部的布局必须是：

    manifest.json
    LICENSE
    skills/<skill-id>/SKILL.md
    skills/<skill-id>/scripts/...
    skills/<skill-id>/config/...
    ...

用法：
    python3 tools/build_pack.py

产物落在 dist/，文件名形如 media-router-skill-v1.0.0.zip。
"""

import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACK_MANIFEST = ROOT / "pack" / "manifest.json"
DIST_DIR = ROOT / "dist"

# 放进 skills/<id>/ 的内容（白名单；没列进来的一律不进包）
SKILL_FILES = ["SKILL.md", "README.md"]
SKILL_DIRS = ["scripts", "config", "references", "assets"]

# 遍历时整个跳过的目录名
SKIP_DIR_NAMES = {"__pycache__", "state", "outputs", ".git", ".idea", ".workbuddy"}

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
            if is_secret_like(p.name):
                print("  跳过（密钥守卫）：" + str(rel).replace("\\", "/"))
                continue
            out.append((p, str(rel).replace("\\", "/")))

    return out


def main():
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
    print("")
    print("挂到 Release 资产：")
    print("  gh release upload " + version + " \"" + str(zip_path) + "\" --clobber")


if __name__ == "__main__":
    main()
