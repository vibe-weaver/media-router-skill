#!/usr/bin/env python3
"""mp4 元数据解析的单元测试（时长 / 分辨率 / 水印标记）。

零依赖、不联网、不碰真实配置：全部用现场拼出来的合成 mp4。
覆盖的是两个真实翻过的车：

  1. moov 落在文件尾（非 faststart）时，只读文件头 1MB 会彻底找不到 mvhd
     —— 智谱 CogVideoX 的成片就是这样，时长永远 None。
  2. 用 find(b"tkhd") 猜位置时，udta 里的元数据文本只要含这四个字符就能
     让解析器读出一组假宽高。

运行：python tests/test_mp4_meta.py
"""

import struct
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from mrouter.caption import mp4_dimensions, mp4_duration, profile_files  # noqa: E402

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


def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def mvhd(timescale: int, duration: int) -> bytes:
    # version+flags + creation + modification + timescale + duration + 余量
    body = bytes([0, 0, 0, 0])
    body += struct.pack(">III", 0, 0, timescale)
    body += struct.pack(">I", duration)
    body += b"\x00" * 80  # rate/volume/matrix/next_track_id 等，解析器不读
    return box(b"mvhd", body)


def tkhd(width: int, height: int, version: int = 0) -> bytes:
    if version == 1:
        body = bytes([1, 0, 0, 0]) + b"\x00" * 16
    else:
        body = bytes([0, 0, 0, 0]) + b"\x00" * 8
    body += struct.pack(">I", 1)          # track_ID
    body += b"\x00" * 4                   # reserved
    body += struct.pack(">I", 5000)       # duration
    body += b"\x00" * 8                   # reserved
    body += struct.pack(">hhh", 0, 0, 0)  # layer / alt_group / volume
    body += b"\x00" * 2                   # reserved
    body += b"\x00" * 36                  # matrix
    body += struct.pack(">II", width << 16, height << 16)
    return box(b"tkhd", body)


def build(width: int, height: int, seconds: float, faststart: bool, decoy: bool) -> bytes:
    timescale = 1000
    trak = box(b"trak", tkhd(width, height))
    children = mvhd(timescale, int(seconds * timescale))
    if decoy:
        # 元数据里塞一段看起来像 tkhd 的文本，诱使「按魔数猜位置」的实现上当
        children += box(b"udta", b"\x00\x00\x00\x20tkhd" + b"\x00" * 8 + struct.pack(">II", 99 << 16, 88 << 16))
    children += trak
    moov = box(b"moov", children)
    ftyp = box(b"ftyp", b"isom" + b"\x00\x00\x02\x00" + b"isomiso2avc1mp41")
    mdat = box(b"mdat", b"\x00" * (1024 * 1024))  # 1MB，把 moov 推到 1MB 之后

    if faststart:
        return ftyp + moov + mdat
    return ftyp + mdat + moov


def write(tmp: Path, name: str, data: bytes) -> Path:
    p = tmp / name
    p.write_bytes(data)
    return p


print("mp4 元数据解析测试")
tmp = Path(tempfile.mkdtemp(prefix="mr-mp4-"))

# ---------------------------------------------------------------- 时长
tail = write(tmp, "tail.mp4", build(1280, 720, 5.2, faststart=False, decoy=False))
head = write(tmp, "head.mp4", build(1920, 1080, 3.5, faststart=True, decoy=False))
check(mp4_duration(tail) == 5.2, "moov 在文件尾（>1MB）也能读到时长为 5.2")
check(mp4_duration(head) == 3.5, "faststart（moov 在头）仍能读出 3.5")

# ---------------------------------------------------------------- 分辨率
check(mp4_dimensions(tail) == (1280, 720), "文件尾 moov：分辨率 1280×720")
check(mp4_dimensions(head) == (1920, 1080), "faststart：分辨率 1920×1080")
check(mp4_dimensions(write(tmp, "v1.mp4", build(640, 360, 2.0, False, False))) == (640, 360),
      "v0 tkhd 正常")

# version=1 的 tkhd（时间戳 64 位，字段整体后移 8 字节）
v1_payload = tkhd(3840, 2160, version=1)
v1_moov = box(b"moov", mvhd(1000, 2000) + box(b"trak", v1_payload))
v1_file = write(tmp, "v1_tkhd.mp4", box(b"ftyp", b"isom\x00\x00\x02\x00isom") + v1_moov)
check(mp4_dimensions(v1_file) == (3840, 2160), "v1 tkhd 的 8 字节偏移处理正确")

# ---------------------------------------------------------------- 诱饵
decoy = write(tmp, "decoy.mp4", build(1280, 720, 4.0, faststart=False, decoy=True))
check(mp4_dimensions(decoy) == (1280, 720), "udta 里伪造的 tkhd 不会被误读成 99×88")
check(mp4_duration(decoy) == 4.0, "udta 诱饵不影响时长")

# ---------------------------------------------------------------- 坏文件
check(mp4_duration(write(tmp, "empty.mp4", b"")) is None, "空文件返回 None")
check(mp4_dimensions(write(tmp, "junk.mp4", b"not an mp4 at all")) is None, "非 mp4 返回 None")
check(mp4_duration(write(tmp, "truncated.mp4", build(1280, 720, 5.0, False, False)[:40])) is None,
      "截断文件不抛异常")

# ---------------------------------------------------------------- profile_files
prof = profile_files([str(tail)], "video")[0]
check(prof.get("duration_seconds") == 5.2 and prof.get("width") == 1280 and prof.get("height") == 720,
      "profile_files 同时回填时长与宽高")

# 图片路径不受影响
png = tmp / "x.png"
png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + struct.pack(">II", 64, 32))
img = profile_files([str(png)], "image")[0]
check((img.get("width"), img.get("height")) == (64, 32), "图片仍走宽高解析")
check("duration_seconds" not in img, "图片不会去解析 mp4 时长")

print()
print(f"通过 {passed} 项，失败 {failed} 项")
sys.exit(1 if failed else 0)
