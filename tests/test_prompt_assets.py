"""提示词资料快照验收；仅使用标准库，不依赖用户源目录。"""

import hashlib
import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "references/prompts/library"
MANIFEST_SHA = "e70c43c29467a76e353dcb6aa53a3b7baa779a8c958604a233d6c2cffd43e5b7"
SNAPSHOT_SHA = "89174c0d0ea92b3a3a8d07a0f335e20ec4017ef34a01394c00c6ca5646a071bb"
PROMPT = re.compile(
    rb"<!-- prompt:start -->\s*```[^\r\n]*\r?\n(.*?)\r?\n```\s*<!-- prompt:end -->",
    re.S,
)
LINK = re.compile(r"\]\((?:<([^>]+)>|([^\s)]+))\)")


def check_snapshot(root):
    root = Path(root)
    manifest = root / "manifest.json"
    if not manifest.is_file():
        raise AssertionError("缺少资料清单：%s" % manifest)
    manifest_bytes = manifest.read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != MANIFEST_SHA:
        raise AssertionError("资料清单字节改变")
    data = json.loads(manifest_bytes.decode("utf-8"))
    assert len(data["categories"]) == 13, "分类数量改变"
    assert len(data["cases"]) == data["totalCases"] == 541, "案例数量改变"
    assert len({c["promptSha256"] for c in data["cases"]}) == data["uniquePromptTexts"] == 521
    assert sum(c["promptStatus"] == "provided" for c in data["cases"]) == 539
    assert data["unpublishedPromptCases"] == 2
    assert {c["id"] for c in data["cases"] if c["promptStatus"] == "unpublished"} == {335, 336}
    assert {c["id"] for c in data["cases"]} == set(range(1, 545)) - {12, 169, 170}

    files = {}
    for case in data["cases"]:
        path = root / case["file"]
        assert path.is_file(), "缺少案例文件：%s" % case["file"]
        if case["file"] not in files:
            files[case["file"]] = path.read_bytes().splitlines(keepends=True)
        lines = files[case["file"]]
        assert 1 <= case["startLine"] <= case["endLine"] <= len(lines), case["id"]
        excerpt = b"".join(lines[case["startLine"] - 1:case["endLine"]])
        assert excerpt.startswith(('<a id="case-%d"></a>' % case["id"]).encode("utf-8")), case["id"]
        assert re.findall(rb'^<a id="case-(\d+)"></a>', excerpt, re.M) == [str(case["id"]).encode("ascii")]
        matches = list(PROMPT.finditer(excerpt))
        assert len(matches) == 1, case["id"]
        assert hashlib.sha256(matches[0].group(1)).hexdigest() == case["promptSha256"], case["id"]

    paths = sorted((p.relative_to(root).as_posix(), p) for p in root.rglob("*") if p.is_file())
    assert len(paths) == 60, "快照文件数改变"
    digest = hashlib.sha256()
    for relative, path in paths:
        # 路径及内容哈希均参与计算，覆盖许可、导航和混合换行的原文副本。
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    assert digest.hexdigest() == SNAPSHOT_SHA, "快照文件路径或原始字节改变"
    return data


class PromptAssetsTests(unittest.TestCase):
    def test_snapshot_and_cases_are_intact(self):
        check_snapshot(LIB)

    def test_source_not_registered_as_second_skill_and_rights_retained(self):
        self.assertTrue(LIB.is_dir(), "缺少内嵌资料根目录")
        for relative in ("SKILL.md", "README.md", "references/host-generation.md"):
            with self.subTest(excluded=relative):
                self.assertFalse((LIB / relative).exists())
        required = (
            "LICENSE", "SOURCES.md", "references/docs-prompts/source-notice.md",
            "references/docs-prompts/originals/disclaimer.md.txt",
        )
        for relative in required:
            self.assertTrue((LIB / relative).is_file(), relative)
        license_text = (LIB / "LICENSE").read_text(encoding="utf-8")
        self.assertIn("MIT License", license_text)
        self.assertIn("Copyright (c) 2026 freestylefly", license_text)
        sources = (LIB / "SOURCES.md").read_text(encoding="utf-8")
        self.assertIn("https://github.com/freestylefly/awesome-gpt-image-2", sources)
        self.assertIn("来源署名和可访问链接不是授权证明", sources)
        self.assertIn("不表示已经核验所有第三方", sources)
        notice = (LIB / "references/docs-prompts/source-notice.md").read_text(encoding="utf-8")
        disclaimer = (LIB / "references/docs-prompts/originals/disclaimer.md.txt").read_text(encoding="utf-8")
        self.assertIn("不保证第三方内容可用于商业用途", notice)
        self.assertIn("不保证第三方内容可用于商业用途", disclaimer)

    def test_catalog_ids_and_ranges_match_manifest(self):
        data = check_snapshot(LIB)
        by_id = {c["id"]: c for c in data["cases"]}
        seen = set()
        for category in data["categories"]:
            path = LIB / category["catalog"]
            category_ids = set()
            for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                match = re.match(r"^\|\s*\[(\d+)\]", line)
                if not match:
                    continue
                with self.subTest(catalog=category["catalog"], line=line_number):
                    cells = [value.strip() for value in re.split(r"(?<!\\)\|", line)[1:-1]]
                    self.assertEqual(len(cells), 6)
                    cid = int(match.group(1))
                    self.assertNotIn(cid, seen)
                    self.assertIn(cid, by_id)
                    seen.add(cid)
                    category_ids.add(cid)
                    case = by_id[cid]
                    self.assertEqual(case["category"], category["source"])
                    self.assertEqual(case["catalog"], category["catalog"])
                    self.assertEqual(case["file"], category["file"])
                    self.assertEqual(cells[5], "%d–%d" % (case["startLine"], case["endLine"]))
                    link = LINK.search(cells[0])
                    self.assertIsNotNone(link)
                    relative, _, anchor = (link.group(1) or link.group(2)).partition("#")
                    self.assertEqual((path.parent / relative).resolve(), (LIB / case["file"]).resolve())
                    self.assertEqual(anchor, "case-%d" % cid)
            self.assertEqual(len(category_ids), category["count"])
            self.assertEqual(category_ids, {c["id"] for c in data["cases"] if c["category"] == category["source"]})
        self.assertEqual(seen, set(by_id))

    def test_managed_reference_links_and_anchors_resolve(self):
        self.assertTrue((LIB / "references/index.md").is_file(), "缺少资料导航")
        managed = [
            LIB / "SOURCES.md", LIB / "references/index.md",
            LIB / "references/styles.md", LIB / "references/templates.md",
        ]
        for directory in ("references/templates", "references/catalogs", "references/docs-prompts"):
            managed.extend(sorted((LIB / directory).glob("*.md")))
        # originals/*.txt 是历史原文，不是受维护导航；不能要求其旧上游链接有效。
        checked = 0
        for source in managed:
            self.assertTrue(source.is_file(), str(source))
            for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
                for match in LINK.finditer(line):
                    raw = match.group(1) or match.group(2)
                    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", raw) or raw.startswith("//"):
                        continue
                    checked += 1
                    with self.subTest(source=source.relative_to(LIB).as_posix(), line=line_number, link=raw):
                        file_part, _, anchor = raw.partition("#")
                        target = (source.parent / file_part if file_part else source).resolve()
                        target.relative_to(LIB.resolve())
                        self.assertTrue(target.is_file(), "链接目标不存在：%s" % raw)
                        if not anchor:
                            continue
                        text = target.read_text(encoding="utf-8")
                        line_anchor = re.fullmatch(r"L([1-9]\d*)(?:-L([1-9]\d*))?", anchor)
                        if line_anchor:
                            start = int(line_anchor.group(1))
                            end = int(line_anchor.group(2) or start)
                            self.assertLessEqual(start, end)
                            self.assertLessEqual(end, len(text.splitlines()))
                            continue
                        explicit = re.search(r'(?:id|name)=["\']' + re.escape(anchor) + r'["\']', text)
                        headings = [
                            re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")
                            for heading in re.findall(r"^#{1,6}\s+(.+)$", text, re.M)
                        ]
                        self.assertTrue(explicit or anchor in headings, "链接锚点不存在：%s" % raw)
        self.assertGreater(checked, 0, "没有检查到本地导航链接")

    def test_only_library_has_explicit_byte_preservation_rule(self):
        attributes = ROOT / ".gitattributes"
        self.assertTrue(attributes.is_file(), "缺少资料字节保护规则")
        rules = [line.strip() for line in attributes.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(rules, ["references/prompts/library/** -text"])


if __name__ == "__main__":
    unittest.main()
