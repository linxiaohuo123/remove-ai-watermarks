"""GUI pipeline regression tests (PRD BUG-01..BUG-10).

Pure-logic tests only: no Tk window is created. The Tk shell in
``scripts/gui_app.py`` is a thin adapter over the functions tested here, so a
failure in this file means the packaged exe carries the same defect.

Every test targets the NEW structured interface; the pre-fix implementation
either lacked the function (ImportError/AttributeError) or behaved as the
assertion names say it must not.
"""

from __future__ import annotations

import hashlib
import os
import queue
import sys
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

# The GUI under test is a Tk application; CI Python ships no tkinter.
pytest.importorskip("tkinter")

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import gui_app  # noqa: E402  (scripts/ added to sys.path above)

# ── Format precheck (BUG-01 / BUG-02 / 5.1) ────────────────────────────────


@pytest.fixture
def static_images(tmp_path: Path) -> dict[str, Path]:
    """One valid single-frame image per supported static format."""
    img = Image.new("RGB", (64, 48), (90, 140, 190))
    paths: dict[str, Path] = {}
    for fmt, ext in [
        ("PNG", "png"),
        ("JPEG", "jpg"),
        ("JPEG", "jpeg"),
        ("WEBP", "webp"),
        ("AVIF", "avif"),
        ("BMP", "bmp"),
        ("TIFF", "tif"),
        ("TIFF", "tiff"),
    ]:
        p = tmp_path / f"img.{ext}"
        img.save(p, format=fmt)
        paths[ext] = p
    return paths


def test_precheck_accepts_all_static_formats(static_images: dict[str, Path]) -> None:
    for p in static_images.values():
        ok, fmt, reason, n_frames = gui_app.precheck_image(p)
        assert ok, f"{p.suffix} rejected: {reason}"
        assert fmt is not None
        assert n_frames == 1


def _save_frames(path: Path, fmt: str, frames: int) -> None:
    imgs = [Image.new("RGB", (32, 24), (c, 60, 120)) for c in (10, 60, 110)]
    if fmt in ("GIF", "WEBP", "PNG"):
        imgs[0].save(path, format=fmt, save_all=True, append_images=imgs[1:], duration=100)
        return
    imgs[0].save(path, format=fmt, save_all=True, append_images=imgs[1:])


def test_precheck_rejects_gif(tmp_path: Path) -> None:
    p = tmp_path / "anim.gif"
    _save_frames(p, "GIF", 3)
    ok, fmt, reason, n_frames = gui_app.precheck_image(p)
    assert not ok and fmt == "GIF"
    assert "GIF" in (reason or "")
    assert n_frames == 3


def test_precheck_rejects_apng(tmp_path: Path) -> None:
    p = tmp_path / "anim.png"
    _save_frames(p, "PNG", 3)
    ok, _fmt, reason, n_frames = gui_app.precheck_image(p)
    assert not ok
    assert "PNG" in (reason or "") and "帧" in (reason or "")
    assert n_frames == 3


def test_precheck_rejects_animated_webp(tmp_path: Path) -> None:
    p = tmp_path / "anim.webp"
    _save_frames(p, "WEBP", 3)
    ok, _fmt, reason, n_frames = gui_app.precheck_image(p)
    assert not ok
    assert "WEBP" in (reason or "") and "帧" in (reason or "")
    assert n_frames == 3


def test_precheck_rejects_multipage_tiff(tmp_path: Path) -> None:
    p = tmp_path / "pages.tiff"
    _save_frames(p, "TIFF", 3)
    ok, _fmt, reason, n_frames = gui_app.precheck_image(p)
    assert not ok
    assert "TIFF" in (reason or "") and ("帧" in (reason or "") or "页" in (reason or ""))
    assert n_frames == 3


def test_precheck_rejects_misnamed_png_named_bmp(tmp_path: Path) -> None:
    """PNG bytes with a .bmp extension must be rejected before any output."""
    p = tmp_path / "fake.bmp"
    Image.new("RGB", (16, 16), (1, 2, 3)).save(p, format="PNG")
    ok, _fmt, reason, _n = gui_app.precheck_image(p)
    assert not ok
    assert reason and ("扩展名" in reason or "格式" in reason or "BMP" in reason)


def test_precheck_rejects_truncated_file(tmp_path: Path) -> None:
    p = tmp_path / "trunc.png"
    Image.new("RGB", (16, 16), (1, 2, 3)).save(p, format="PNG")
    data = p.read_bytes()[: len(p.read_bytes()) // 2]
    p.write_bytes(data)
    ok, _fmt, reason, _n = gui_app.precheck_image(p)
    assert not ok
    assert reason and "解码" in reason


def test_precheck_rejects_directory_named_png(tmp_path: Path) -> None:
    d = tmp_path / "album.png"
    d.mkdir()
    ok, _fmt, reason, _n = gui_app.precheck_image(d)
    assert not ok
    assert "文件" in (reason or "")


# ── Exclusive naming (BUG-04 / 5.5) ───────────────────────────────────────


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _prefill_clean_outputs(directory: Path, up_to: int) -> list[Path]:
    existing: list[Path] = []
    for i in range(up_to + 1):
        name = "photo_clean.png" if i == 0 else f"photo_clean_{i}.png"
        p = directory / name
        p.write_bytes(b"KEEP")
        existing.append(p)
    return existing


def test_next_name_skips_existing_beyond_1005(tmp_path: Path) -> None:
    existing = _prefill_clean_outputs(tmp_path, 1005)
    (tmp_path / "photo_clean_final.png").write_bytes(b"KEEP")
    reserved = gui_app.next_exclusive_name(tmp_path, "photo", ".png")
    assert reserved.name == "photo_clean_1006.png"
    # The reservation must actually exist (O_EXCL), and no pre-existing file moved.
    assert reserved.exists()
    for p in existing:
        assert _sha(p) == hashlib.sha256(b"KEEP").hexdigest()


def test_reservation_does_not_touch_existing_bytes(tmp_path: Path) -> None:
    existing = _prefill_clean_outputs(tmp_path, 3)
    before = [(p, _sha(p), p.stat().st_mtime_ns) for p in existing]
    gui_app.next_exclusive_name(tmp_path, "photo", ".png")
    for p, h, mtime in before:
        assert _sha(p) == h
        assert p.stat().st_mtime_ns == mtime


def test_concurrent_reservations_get_distinct_names(tmp_path: Path) -> None:
    results: list[Path] = []
    lock = threading.Lock()

    def reserve() -> None:
        p = gui_app.next_exclusive_name(tmp_path, "photo", ".png")
        with lock:
            results.append(p)

    threads = [threading.Thread(target=reserve) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 8
    assert len({p.name for p in results}) == 8


# ── Structured results (BUG-08 / 5.6) ──────────────────────────────────────


def test_run_pipeline_clean_static_success(static_images: dict[str, Path]) -> None:
    q: queue.Queue[str] = __import__("queue").Queue()
    r = gui_app.run_pipeline(
        static_images["png"],
        None,
        do_visible=False,
        do_report=True,
        do_strip=True,
        stop_event=threading.Event(),
        log_q=q,
    )
    assert r.status == "success"
    assert r.output is not None and r.output.exists()
    assert r.error_stage is None and r.error is None


def test_run_pipeline_rejected_input_status_failed_stage_input(tmp_path: Path) -> None:
    p = tmp_path / "anim.gif"
    _save_frames(p, "GIF", 3)
    q: queue.Queue[str] = __import__("queue").Queue()
    r = gui_app.run_pipeline(
        p, None, do_visible=False, do_report=True, do_strip=True, stop_event=threading.Event(), log_q=q
    )
    assert r.status == "failed"
    assert r.error_stage == "input"
    assert r.output is None


def test_status_is_structured_not_text_based(tmp_path: Path) -> None:
    """BUG-08: the selftest exit code must come from the structured status, never
    from scanning log text for the word 失败."""

    ok = gui_app.FileProcessResult(
        source=tmp_path / "a.png",
        status="success",
        output=None,
        messages=("检测报告: 失败重试 3 次后完成",),
        error_stage=None,
        error=None,
    )
    bad = gui_app.FileProcessResult(
        source=tmp_path / "b.png", status="failed", output=None, messages=("全部成功",), error_stage="input", error="x"
    )
    warn = gui_app.FileProcessResult(
        source=tmp_path / "c.png",
        status="warning",
        output=None,
        messages=("报告失败但处理成功",),
        error_stage=None,
        error=None,
    )
    assert gui_app.selftest_exit_code(ok) == 0
    assert gui_app.selftest_exit_code(warn) == 0
    assert gui_app.selftest_exit_code(bad) != 0


def test_batch_summary_counts(static_images: dict[str, Path]) -> None:
    from queue import Queue

    q: queue.Queue[str] = Queue()
    stop = threading.Event()
    results = [
        gui_app.run_pipeline(p, None, do_visible=False, do_report=False, do_strip=True, stop_event=stop, log_q=q)
        for p in [static_images["png"], static_images["jpg"], static_images["webp"]]
    ]
    summary = gui_app.format_batch_summary(results)
    assert "成功 3" in summary
    assert "失败 0" in summary


# ── Close protocol (BUG-03 / 5.8) ─────────────────────────────────────────


def test_batch_worker_is_not_daemon(static_images: dict[str, Path]) -> None:
    """BUG-03: the batch thread must not be a daemon, or closing the window can
    kill an in-flight write and leave a half file."""
    from queue import Queue

    runner = gui_app.BatchRunner(
        files=[static_images["png"]],
        out_dir=None,
        do_visible=False,
        do_report=False,
        do_strip=True,
        stop_event=threading.Event(),
        log_q=Queue(),
    )
    assert runner.thread.daemon is False


def test_stop_event_cancels_pending_files(static_images: dict[str, Path]) -> None:
    from queue import Queue

    stop = threading.Event()
    stop.set()  # already stopped: nothing may start
    q: queue.Queue[str] = Queue()
    results = gui_app.BatchRunner.run_sync(
        files=list(static_images.values()),
        out_dir=None,
        do_visible=False,
        do_report=False,
        do_strip=True,
        stop_event=stop,
        log_q=q,
    )
    assert results and all(r.status == "cancelled" for r in results)


def test_close_during_first_file_completes_current_and_cancels_rest(
    static_images: dict[str, Path],
) -> None:
    """BUG-03: closing mid-batch must finish the current file atomically, then
    mark everything not yet started as cancelled."""
    from queue import Queue

    stop = threading.Event()
    q: queue.Queue[str] = Queue()
    started = threading.Event()

    def slow_processor(
        source: Path,
        out_dir: Path | None,
        *,
        do_visible: bool,
        do_report: bool,
        do_strip: bool,
        stop_event: threading.Event,
        log_q: queue.Queue[str],
        **kwargs: object,
    ) -> gui_app.FileProcessResult:
        started.set()
        time.sleep(0.4)  # give the "window close" time to fire stop
        return gui_app.FileProcessResult(
            source=source, status="success", output=Path("x.png"), messages=("done",), error_stage=None, error=None
        )

    original = gui_app.run_pipeline
    gui_app.run_pipeline = slow_processor  # type: ignore[assignment]
    try:
        runner = gui_app.BatchRunner(
            files=list(static_images.values()),
            out_dir=None,
            do_visible=False,
            do_report=False,
            do_strip=True,
            stop_event=stop,
            log_q=q,
        )
        runner.start()
        started.wait(5)
        stop.set()  # user closed the window while file #1 was processing
        results = runner.join_result()
    finally:
        gui_app.run_pipeline = original  # type: ignore[assignment]

    assert results[0].status == "success"
    assert all(r.status == "cancelled" for r in results[1:])
    assert results[0].source == static_images["png"]


# ── Background scanning (BUG-09 / BUG-10 / 5.7) ───────────────────────────


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="case-collapsing dedup relies on Windows normcase; POSIX filesystems (incl. macOS CI) are case-sensitive",
)
def test_scan_case_insensitive_dedup(tmp_path: Path) -> None:
    """Windows-only: the GUI's input scan collapses differently-cased spellings
    of one file (os.path.normcase lowercases on Windows). On case-sensitive
    filesystems -- Linux and the macOS CI runner's APFS volume -- the three
    spellings are distinct files, so the contract only applies on Windows."""
    p = tmp_path / "Photo.png"
    p.write_bytes(b"x")
    scanned = gui_app.scan_paths([tmp_path / "photo.png", tmp_path / "PHOTO.PNG", p, str(p)])
    assert [q.resolve() for q in scanned] == [p.resolve()]


def test_scan_directory_named_png_is_not_file(tmp_path: Path) -> None:
    d = tmp_path / "album.png"
    d.mkdir()
    inner = d / "real.png"
    inner.write_bytes(b"x")
    scanned = gui_app.scan_paths([d])
    assert all(s.is_file() for s in scanned)
    assert inner.resolve() in [s.resolve() for s in scanned]


def test_scan_keeps_clean_suffixed_inputs(tmp_path: Path) -> None:
    """BUG-10: `portrait_clean.png` and files under `exports_clean/` are legal
    user inputs and must NOT be filtered out."""
    p1 = tmp_path / "portrait_clean.png"
    p1.write_bytes(b"x")
    d = tmp_path / "exports_clean"
    d.mkdir()
    p2 = d / "hero.png"
    p2.write_bytes(b"x")
    scanned = {s.resolve() for s in gui_app.scan_paths([tmp_path])}
    assert p1.resolve() in scanned
    assert p2.resolve() in scanned


def test_scan_stable_order(tmp_path: Path) -> None:
    for name in ("b.png", "a.png", "c.jpg"):
        (tmp_path / name).write_bytes(b"x")
    sub = tmp_path / "sub"
    sub.mkdir()
    for name in ("z.png", "y.png"):
        (sub / name).write_bytes(b"x")
    scanned = gui_app.scan_paths([tmp_path])
    names = [p.name for p in scanned]
    assert names == sorted(names)


def test_scan_missing_input_is_skipped(tmp_path: Path) -> None:
    scanned = gui_app.scan_paths([tmp_path / "missing.png"])
    assert scanned == []


def test_scan_warns_on_missing_input(tmp_path: Path) -> None:
    files, warnings = gui_app.scan_paths_with_warnings([tmp_path / "missing.png"])
    assert files == []
    assert any("不存在" in w for w in warnings)


def test_scan_thread_is_daemon(tmp_path: Path) -> None:
    """Closing the window while a scan runs must not keep the process alive:
    the scan thread is daemon (it only collects file lists); the batch thread
    is the non-daemon one (see test_batch_worker_is_not_daemon)."""
    app = _FakeApp()
    app._enqueue_scan([tmp_path])
    assert app.scan_thread is not None
    assert app.scan_thread.daemon is True
    app.scan_thread.join(timeout=10)
    assert not app.scan_thread.is_alive()
    assert app.scan_q.get(timeout=1)[0] == "FILES"


class _FakeApp:
    """Minimal stand-in for App so scan lifecycle tests need no Tk window."""

    def __init__(self) -> None:
        self.files: list[Path] = []
        self.scan_busy = False
        self.busy = False
        self.closing = False
        self.scan_thread: threading.Thread | None = None
        self.scan_q: queue.Queue[tuple[str, list[Path], list[str]]] = queue.Queue()
        self.log_q: queue.Queue[str] = queue.Queue()
        self._scan_warnings: list[str] = []
        self.status = _FakeStatus()
        self._buttons_enabled = True

    def _enqueue_scan(self, inputs: list[Path]) -> None:
        from gui_app import App

        App._enqueue_scan(self, inputs)

    def _scan_worker(self, inputs: list[Path]) -> None:
        from gui_app import App

        App._scan_worker(self, inputs)

    def _append_log(self, text: str) -> None:
        self._scan_warnings.append(text)

    def _set_scan_busy(self, scan_busy: bool) -> None:
        self.scan_busy = scan_busy

    def _set_busy(self, busy: bool) -> None:
        self._buttons_enabled = not busy and not self.scan_busy


class _FakeStatus:
    def set(self, value: str) -> None:
        pass


def test_gps_and_interop_survive_pixel_pass(tmp_path: Path) -> None:
    """BUG-06 regression: GPS and Interop IFD payloads must survive a visible-
    mark pixel pass, not be silently dropped."""
    import piexif

    img = tmp_path / "gps.jpg"
    exif = {
        "0th": {piexif.ImageIFD.Make: b"Cam", piexif.ImageIFD.Orientation: 1},
        "Exif": {piexif.ExifIFD.DateTimeOriginal: b"2026:08:12 10:00:00"},
        "GPS": {
            piexif.GPSIFD.GPSLatitudeRef: b"N",
            piexif.GPSIFD.GPSLatitude: ((31, 1), (13, 1), (0, 1)),
        },
        "Interop": {piexif.InteropIFD.InteroperabilityIndex: b"R98"},
    }
    Image.new("RGB", (64, 48), (90, 140, 190)).save(img, format="JPEG", exif=piexif.dump(exif))
    r = gui_app.run_pipeline(
        img,
        tmp_path,
        do_visible=True,
        do_report=False,
        do_strip=True,
        stop_event=threading.Event(),
        log_q=queue.Queue(),
    )
    assert r.status == "success", (r.status, r.error)
    assert r.output is not None and r.output.exists()
    with Image.open(r.output) as out:
        ex = out.getexif()
        gps = ex.get_ifd(0x8825)
        interop = ex.get_ifd(0xA005)
        assert gps.get(piexif.GPSIFD.GPSLatitudeRef) in (b"N", "N")
        assert interop.get(piexif.InteropIFD.InteroperabilityIndex) in (b"R98", "R98")


def test_jpeg_comment_survives_pixel_pass(tmp_path: Path) -> None:
    """A user JPEG COM segment must survive a visible-mark pixel pass."""
    import piexif

    img = tmp_path / "comment.jpg"
    Image.new("RGB", (64, 48), (90, 140, 190)).save(
        img, format="JPEG", exif=piexif.dump({"0th": {piexif.ImageIFD.Orientation: 1}}), comment=b"a user comment here"
    )
    r = gui_app.run_pipeline(
        img,
        tmp_path,
        do_visible=True,
        do_report=False,
        do_strip=True,
        stop_event=threading.Event(),
        log_q=queue.Queue(),
    )
    assert r.status == "success", (r.status, r.error)
    assert r.output is not None and r.output.exists()
    with Image.open(r.output) as out:
        assert out.info.get("comment") == b"a user comment here"


def test_png_xmp_chunk_is_ai_related_and_not_verified(tmp_path: Path) -> None:
    """XMP chunks (the AI generators' primary carrier) are treated as AI-related:
    a pixel pass must not fail verification on them, and the output must not
    carry the stale XMP block (regression: real Gemini PNGs carry an XMP chunk
    and previously made every visible pass fail)."""
    from PIL.PngImagePlugin import PngInfo

    img = tmp_path / "xmp.png"
    info = PngInfo()
    info.add_itxt("XML:com.adobe.xmp", '<?xpacket begin=""?><x:xmpmeta xmlns:x="adobe:ns:meta/">X</x:xmpmeta>')
    info.add_text("Author", "Human Artist")
    Image.new("RGB", (64, 48), (90, 140, 190)).save(img, format="PNG", pnginfo=info)
    assert "xmp" in {k.lower() for k in Image.open(img).info}, "fixture must actually carry XMP"
    r = gui_app.run_pipeline(
        img,
        tmp_path,
        do_visible=True,
        do_report=False,
        do_strip=True,
        stop_event=threading.Event(),
        log_q=queue.Queue(),
    )
    assert r.status == "success", (r.status, r.error)
    assert r.output is not None and r.output.exists()
    with Image.open(r.output) as out:
        assert "Author" in out.info, "non-AI text must survive"
        xmp_keys = [k for k in out.info if "xmp" in k.lower() or "adobe" in k.lower()]
        assert xmp_keys == [], f"AI-related XMP chunk survived: {xmp_keys}"


# ── Batch epoch: stale queue messages must never bleed into the next batch ──


class _FakeRoot:
    """Minimal Tk-free stand-in for the root window used by the harness."""

    def withdraw(self) -> None:  # pragma: no cover - trivial
        pass

    def destroy(self) -> None:  # pragma: no cover - trivial
        pass


class _FakeVar:
    """StringVar stand-in holding a value, without a Tk root."""

    def __init__(self, value: str = "") -> None:
        self._value = value

    def set(self, value: str) -> None:
        self._value = value

    def get(self) -> str:
        return self._value


class _UiHarness:
    """Tk-free stand-in for App's queue-consuming half."""

    def __init__(self) -> None:
        self.root = _FakeRoot()
        self.log_q: queue.Queue[str] = queue.Queue()
        self.batch_epoch = 1
        self.busy = False
        self.batch_runner: object | None = None
        self.closing = False
        self._done_epochs: list[int] = []
        self._resultee: list[tuple[int, int, str]] = []
        self._after_ok = True
        self.status: _FakeVar = _FakeVar()

    def _is_current_epoch(self, epoch: int) -> bool:
        return epoch == self.batch_epoch

    def _on_batch_done(self, epoch: int) -> None:
        self._done_epochs.append(epoch)

    def _apply_result_line(self, idx: int, status: str) -> None:
        self._resultee.append((self.batch_epoch, idx, status))

    def destroy(self) -> None:
        self.root.destroy()


def test_stale_done_from_previous_batch_is_ignored() -> None:
    """BUG: a `!DONE` left in the queue by a PREVIOUS batch must not flip the UI
    or join the CURRENT runner. Pre-fix, the message carried no epoch and was
    indistinguishable from the live batch's."""
    h = _UiHarness()
    try:
        gui_app.App.consume_batch_message(h, "!DONE:0")
        assert h._done_epochs == [], "stale !DONE must not be acted upon"
        gui_app.App.consume_batch_message(h, "!DONE:1")
        assert h._done_epochs == [1], "live !DONE must be acted upon"
    finally:
        h.destroy()


def test_stale_result_from_previous_batch_is_ignored() -> None:
    h = _UiHarness()
    try:
        gui_app.App.consume_batch_message(h, "!RESULT:0:2:failed")
        assert h._resultee == [], "stale !RESULT must not touch the tree"
        gui_app.App.consume_batch_message(h, "!RESULT:1:2:success")
        assert h._resultee == [(1, 2, "success")]
    finally:
        h.destroy()


def test_batch_runner_emits_epoch_in_messages(tmp_path: Path) -> None:
    """The runner must tag every control message with its epoch so the UI can
    discard stragglers from earlier batches."""
    img = tmp_path / "img.png"
    Image.new("RGB", (16, 16), (1, 2, 3)).save(img, format="PNG")
    q: queue.Queue[str] = queue.Queue()
    gui_app.BatchRunner.run_sync(
        files=[img],
        out_dir=None,
        do_visible=False,
        do_report=False,
        do_strip=True,
        stop_event=threading.Event(),
        log_q=q,
        epoch=7,
    )
    msgs = []
    while not q.empty():
        msgs.append(q.get_nowait())
    assert "!DONE:7" in msgs
    assert any(m.startswith("!STATUS:7:") for m in msgs)
    assert any(m.startswith("!RESULT:7:") for m in msgs)


# ── Fault injection: no half file may ever carry the final name (PRD 8.3.3) ──


def _assert_no_final_named_file(tmp_path: Path) -> None:
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith("img_clean")]
    assert leftovers == [], f"final-name artifact survived a failed run: {leftovers}"


def _run(img: Path, tmp_path: Path, **overrides: object) -> gui_app.FileProcessResult:
    return gui_app.run_pipeline(
        img,
        tmp_path,
        do_visible=False,
        do_report=False,
        do_strip=True,
        stop_event=threading.Event(),
        log_q=queue.Queue(),
        **overrides,  # type: ignore[arg-type]
    )


def test_failure_in_metadata_stage_leaves_no_final(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import remove_ai_watermarks.metadata as md

    img = tmp_path / "img.png"
    Image.new("RGB", (16, 16), (1, 2, 3)).save(img, format="PNG")

    def boom(*args: object, **kwargs: object) -> tuple[object, object]:
        raise RuntimeError("injected strip failure")

    monkeypatch.setattr(md, "strip_and_verify", boom)
    r = _run(img, tmp_path)
    assert r.status == "failed" and r.error_stage == "internal"
    assert r.output is None
    _assert_no_final_named_file(tmp_path)


def test_failure_in_publish_stage_leaves_no_final(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """os.replace (the atomic publish) failing must leave no final-name artifact.
    Needs do_visible=True: the metadata-only path writes straight into the
    placeholder and never reaches os.replace."""
    img = tmp_path / "img.png"
    Image.new("RGB", (16, 16), (1, 2, 3)).save(img, format="PNG")

    real_replace = os.replace
    calls = 0

    def boom(src: object, dst: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected publish failure")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", boom)
    r = gui_app.run_pipeline(
        img,
        tmp_path,
        do_visible=True,
        do_report=False,
        do_strip=True,
        stop_event=threading.Event(),
        log_q=queue.Queue(),
    )
    assert r.status == "failed" and r.error_stage == "internal"
    assert r.output is None
    _assert_no_final_named_file(tmp_path)


def test_failure_in_verify_stage_leaves_no_final(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    img = tmp_path / "img.png"
    Image.new("RGB", (16, 16), (1, 2, 3)).save(img, format="PNG")

    monkeypatch.setattr(gui_app, "verify_output", lambda *a, **k: ["注入的验证失败"])
    r = _run(img, tmp_path)
    assert r.status == "failed" and r.error_stage == "output"
    assert r.output is None
    _assert_no_final_named_file(tmp_path)


def test_success_after_fault_still_respects_existing_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """After an injected failure the placeholder is removed; a later successful
    run on the same input must not skip a name because of a ghost placeholder."""
    import remove_ai_watermarks.metadata as md

    img = tmp_path / "img.png"
    Image.new("RGB", (16, 16), (1, 2, 3)).save(img, format="PNG")

    def boom(*args: object, **kwargs: object) -> tuple[object, object]:
        raise RuntimeError("injected")

    monkeypatch.setattr(md, "strip_and_verify", boom)
    assert _run(img, tmp_path).status == "failed"
    _assert_no_final_named_file(tmp_path)

    monkeypatch.undo()
    r = _run(img, tmp_path)
    assert r.status == "success"
    assert r.output is not None and r.output.name == "img_clean.png"
