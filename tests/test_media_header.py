#!/usr/bin/env python3
"""下载后文件头识别：真实临时文件、离线下载桩和生成证据回归。"""

import base64
import io
import json
import socket
import sys
import tempfile
import unittest
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from mrouter import adapters, cli, transport  # noqa: E402
from mrouter.config import ModelSpec, Pool, RouterConfig  # noqa: E402
from mrouter.lifecycle import generation_attempt  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"complete offline artifact" * 4
URL = "https://cdn.invalid/artifact.bin"


class RecordingFile:
    def __init__(self, handle, reads, error=None):
        self.handle = handle
        self.reads = reads
        self.error = error

    def __enter__(self):
        self.handle.__enter__()
        return self

    def __exit__(self, *args):
        return self.handle.__exit__(*args)

    def read(self, size=-1):
        self.reads.append(size)
        if self.error is not None:
            raise self.error
        return self.handle.read(size)


class MediaHeaderTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="mr-header-")
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(socket.socket, "connect", side_effect=AssertionError("no external network")))
        self.stack.enter_context(patch.object(transport, "open_request", side_effect=AssertionError("missing offline download stub")))
        self.stack.enter_context(patch.object(adapters, "request", side_effect=AssertionError("no real generation")))
        self.stack.enter_context(patch.object(cli, "load_config", side_effect=AssertionError("no user config")))

    def download_stub(self, data=PNG, calls=None):
        def save(url, dest, **kwargs):
            if calls is not None:
                calls.append((url, dest))
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            return dest
        return patch.object(adapters, "download", side_effect=save)

    @contextmanager
    def header_failure(self, failure, handles):
        real_open, real_replace = Path.open, Path.replace

        def opened(path, mode="r", *args, **kwargs):
            handle = real_open(path, mode, *args, **kwargs)
            if mode == "rb" and path.suffix == ".bin":
                error = OSError("offline header read denied") if failure == "read" else None
                wrapped = RecordingFile(handle, [], error)
                handles.append(wrapped)
                return wrapped
            return handle

        def replaced(path, target):
            if failure == "rename" and path.suffix == ".bin":
                raise PermissionError("offline header rename denied")
            return real_replace(path, target)

        with patch.object(Path, "open", opened), patch.object(Path, "replace", replaced):
            yield

    def test_remote_reads_only_32_binary_bytes_and_closes_before_rename(self):
        dest = self.root / "artifact_01.bin"
        renamed = dest.with_suffix(".png")
        reads, modes, handles, events = [], [], [], []
        real_open, real_replace = Path.open, Path.replace
        real_completed = adapters.file_completed

        def completed(path, **kwargs):
            events.append((path, kwargs.get("replaces")))
            return real_completed(path, **kwargs)

        def opened(path, mode="r", *args, **kwargs):
            handle = real_open(path, mode, *args, **kwargs)
            if path == dest and mode == "rb":
                self.assertEqual(events, [(dest, None)])
                modes.append(mode)
                wrapped = RecordingFile(handle, reads)
                handles.append(wrapped)
                return wrapped
            return handle

        def replaced(path, target):
            self.assertEqual((path, target), (dest, renamed))
            self.assertEqual(len(handles), 1)
            self.assertTrue(handles[0].handle.closed, "close the header before rename")
            return real_replace(path, target)

        with self.download_stub() as downloaded, patch.object(Path, "open", opened), patch.object(
                Path, "read_bytes", side_effect=AssertionError("forbidden whole-file Path.read_bytes")), patch.object(
                Path, "replace", replaced), patch.object(adapters, "file_completed", completed), patch.object(
                adapters, "sniff_extension", wraps=adapters.sniff_extension) as sniffed:
            result = adapters._fetch_to_local(URL, self.root, "artifact", 1, "image", {"X-Offline": "yes"})
        self.assertEqual((modes, reads), (["rb"], [32]))
        self.assertTrue(handles[0].handle.closed)
        self.assertEqual(events, [(dest, None), (renamed, dest)])
        sniffed.assert_called_once_with(PNG[:32])
        downloaded.assert_called_once_with(URL, dest, headers={"X-Offline": "yes"}, timeout=300.0)
        self.assertEqual(result, renamed)
        self.assertEqual(result.read_bytes(), PNG)
        self.assertFalse(dest.exists())

    def test_known_magic_corrects_wrong_extension_without_changing_bytes(self):
        cases = (
            (b"\x89PNG\r\n\x1a\n", ".png"), (b"\xff\xd8\xff", ".jpg"),
            (b"GIF87a", ".gif"), (b"GIF89a", ".gif"), (b"BM", ".bmp"),
            (b"RIFF\x00\x00\x00\x00WEBP", ".webp"), (b"\x00\x00\x00\x18ftyp", ".mp4"),
            (b"\x1a\x45\xdf\xa3", ".webm"), (b"fLaC", ".flac"), (b"OggS", ".ogg"),
            (b"ID3", ".mp3"), (b"\xff\xfb", ".mp3"),
        )
        for index, (magic, extension) in enumerate(cases, 1):
            with self.subTest(extension=extension, magic=magic):
                data = magic + b"0" * 64
                with self.download_stub(data):
                    result = adapters._fetch_to_local(URL, self.root, "known", index, "image")
                self.assertEqual(result.suffix, extension)
                self.assertEqual(result.read_bytes(), data)

    def test_matching_extension_keeps_downloaded_path(self):
        with self.download_stub(), patch.object(Path, "replace", side_effect=AssertionError("no rename needed")):
            result = adapters._fetch_to_local("https://cdn.invalid/image.png", self.root, "same", 1, "image")
        self.assertEqual(result, self.root / "same_01.png")
        self.assertEqual(result.read_bytes(), PNG)

    def test_existing_download_and_renamed_targets_are_not_overwritten(self):
        existing = self.root / "artifact_01.bin"
        existing_png = self.root / "artifact_01-1.png"
        existing.write_bytes(b"existing download")
        existing_png.write_bytes(b"existing corrected target")
        with self.download_stub():
            result = adapters._fetch_to_local(URL, self.root, "artifact", 1, "image")
        self.assertEqual(result, self.root / "artifact_01-1-1.png")
        self.assertEqual(result.read_bytes(), PNG)
        self.assertEqual(existing.read_bytes(), b"existing download")
        self.assertEqual(existing_png.read_bytes(), b"existing corrected target")

    def test_unknown_short_and_empty_headers_keep_original_extension(self):
        for index, data in enumerate((b"unknown header" * 4, b"x", b"\x89PNG", b""), 1):
            with self.subTest(data=data), self.download_stub(data):
                result = adapters._fetch_to_local(URL, self.root, "unknown", index, "video")
                self.assertEqual(result, self.root / ("unknown_%02d.bin" % index))
                self.assertEqual(result.read_bytes(), data)

    def test_wav_still_repairs_after_header_handle_closes(self):
        # 一个合法 fmt/data 布局，但 RIFF/data 使用流式占位长度。
        wav = (b"RIFF\xff\xff\xff\x7fWAVEfmt \x10\x00\x00\x00"
               b"\x01\x00\x01\x00\x80\x3e\x00\x00\x00\x7d\x00\x00\x02\x00\x10\x00"
               b"data\xff\xff\xff\x7f" + b"\x00\x00" * 8)
        handles, reads = [], []
        real_open, real_repair = Path.open, adapters._repair_wav_sizes

        def opened(path, mode="r", *args, **kwargs):
            handle = real_open(path, mode, *args, **kwargs)
            if mode == "rb":
                wrapped = RecordingFile(handle, reads)
                handles.append(wrapped)
                return wrapped
            return handle

        def repaired(path):
            self.assertTrue(handles and all(item.handle.closed for item in handles))
            self.assertEqual(path.suffix, ".wav")
            return real_repair(path)

        with self.download_stub(wav), patch.object(Path, "open", opened), patch.object(
                adapters, "_repair_wav_sizes", side_effect=repaired) as repair:
            result = adapters._fetch_to_local(URL, self.root, "speech", 1, "audio")
        repair.assert_called_once_with(result)
        self.assertEqual(reads, [32])
        fixed = result.read_bytes()
        self.assertEqual(int.from_bytes(fixed[4:8], "little"), len(wav) - 8)
        self.assertEqual(int.from_bytes(fixed[40:44], "little"), 16)
        self.assertEqual(fixed[44:], wav[44:])

    def test_inline_and_data_uri_use_existing_writers_without_remote_reads(self):
        encoded = base64.b64encode(PNG).decode("ascii")
        cases = ((encoded, "_write_inline_b64"), ("data:image/png;base64," + encoded, "_write_b64_to_local"))
        for index, (value, writer) in enumerate(cases, 1):
            with self.subTest(writer=writer), patch.object(adapters, "download", side_effect=AssertionError("no remote download")), patch.object(
                    Path, "read_bytes", side_effect=AssertionError("no remote header read")), patch.object(
                    adapters, writer, wraps=getattr(adapters, writer)) as written:
                result = adapters._fetch_to_local(value, self.root, "inline", index, "image")
                written.assert_called_once_with(value, self.root, "inline", index, "image")
            self.assertEqual(result.read_bytes(), PNG)
            self.assertEqual(result.suffix, ".png")

    def test_finalize_header_errors_preserve_urls_and_all_completed_files(self):
        spec = ModelSpec("offline", "generic_http", "image")
        urls = ["https://cdn.invalid/first.png", URL, "https://cdn.invalid/later.png"]
        for failure in ("read", "rename"):
            calls, handles = [], []
            out_dir = self.root / failure
            req = adapters.GenRequest(kind="image", output_dir=out_dir)
            with self.subTest(failure=failure), self.download_stub(calls=calls), self.header_failure(failure, handles):
                with self.assertRaises(adapters.DownloadError) as caught:
                    with generation_attempt():
                        adapters._finalize(spec, req, urls, out_dir)
            error = caught.exception
            self.assertEqual([url for url, path in calls], urls[:2])
            self.assertEqual(error.urls, urls)
            self.assertEqual(error.files, [str(path) for url, path in calls])
            self.assertTrue(error.stop_generation)
            self.assertEqual(error.phase, "delivery")
            self.assertFalse(error.submission_uncertain)
            self.assertIsInstance(error.__cause__, OSError)
            self.assertIn("offline header " + failure + " denied", str(error.__cause__))
            self.assertTrue(handles and all(item.handle.closed for item in handles))
            for path in error.files:
                self.assertEqual(Path(path).read_bytes(), PNG)

    def test_header_errors_do_not_regenerate_or_download_later_outputs(self):
        urls = [URL, "https://cdn.invalid/later.png"]
        for failure in ("read", "rename"):
            calls, handles, generated = [], [], []
            models = [ModelSpec(mid, "generic_http", "image") for mid in ("first", "second")]
            cfg = RouterConfig(self.root, self.root, self.root / "state" / failure, self.root / "unused.json",
                               {"output_dir": str(self.root / "outputs" / failure), "caption": {"mode": "none"}},
                               {"image": Pool("image", "priority_then_weight", models)}, {}, "json")

            def generate(spec, req, config, ctx):
                generated.append(spec.id)
                return adapters._finalize(spec, req, urls, req.output_dir)

            args = cli.build_parser().parse_args(["generate", "--kind", "image", "--prompt", "offline", "--max-attempts", "2"])
            output = io.StringIO()
            with self.subTest(failure=failure), self.download_stub(calls=calls), self.header_failure(failure, handles), patch.dict(
                    adapters.ADAPTERS, {"generic_http": generate}), patch.object(cli, "load_config", return_value=cfg), patch.object(
                    cli, "_resolve", return_value=(models, {})), patch.object(
                    cfg, "resolve_api_key", return_value=("", "offline no key")), redirect_stdout(output), redirect_stderr(io.StringIO()):
                rc = cli.cmd_generate(args)
            result = json.loads(output.getvalue())
            self.assertEqual((rc, result["status"]), (1, "error"))
            self.assertEqual(generated, ["first"])
            self.assertEqual(result["attempted"], ["first"])
            self.assertEqual([url for url, path in calls], [URL])
            self.assertEqual(result["urls"], urls)
            self.assertEqual(result["files"], [str(calls[0][1])])
            self.assertEqual(result["phase"], "delivery")
            self.assertEqual(result["stop_reason"], "generated")
            self.assertFalse(result["submission_uncertain"])
            self.assertTrue(handles and all(item.handle.closed for item in handles))
            self.assertEqual(Path(result["files"][0]).read_bytes(), PNG)


if __name__ == "__main__":
    unittest.main(verbosity=2)
