"""Windows CPU GUI front-end for remove-ai-watermarks (PRD-remediated).

Pipeline contract (see docs/gui-exe-remediation-prd.md):

- Only static single-frame PNG/JPEG/WebP/AVIF/BMP/TIFF are accepted; GIF, APNG,
  animated WebP and multi-page TIFF are rejected at precheck with a Chinese
  reason and produce no output.
- Detection, localization and removal share ONE EXIF-normalized decode.
- Ordinary EXIF, Author/Title text, ICC and DPI are preserved; only AI markers
  are stripped.
- Outputs are written through a same-directory temp file, verified
  (magic/frames/dimensions/alpha/metadata) and atomically published to an
  O_EXCL-reserved name that never collides with an existing file.
- Batch results are structured (FileProcessResult), never text-scanned.
- Folder scanning and processing run off the Tk thread; closing the window
  finishes the current file, marks the rest cancelled, then exits.

Everything testable lives below `class App`; the Tk shell is a thin adapter.
"""

# piexif and tkinterdnd2 ship no type stubs; numpy mirrors the src/ convention.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false, reportAttributeAccessIssue=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportMissingImports=false

from __future__ import annotations

import contextlib
import itertools
import os
import queue
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from tkinter import END as TK_END
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable

    from numpy.typing import NDArray

FileStatus = Literal["success", "warning", "failed", "cancelled"]


@dataclass(frozen=True)
class FileProcessResult:
    """One file's structured outcome. Status is authoritative; messages are display-only."""

    source: Path
    status: FileStatus
    output: Path | None
    messages: tuple[str, ...]
    error_stage: str | None  # "input" | "pixels" | "metadata" | "output" | "internal" | None
    error: str | None


# ── Static format admission (5.1) ───────────────────────────────────────────

# Extension -> PIL format name. GIF is deliberately absent: the write path cannot
# reliably keep GIF container/palette semantics, so it is rejected outright.
SUPPORTED_EXT_FORMATS: dict[str, str] = {
    ".png": "PNG",
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".webp": "WEBP",
    ".avif": "AVIF",
    ".bmp": "BMP",
    ".tif": "TIFF",
    ".tiff": "TIFF",
    ".heic": "HEIF",
    ".heif": "HEIF",
}

# AI text-chunk keys reused by the pixel-stage write-back (mirrors the library's
# AI_METADATA_KEYS / AI_KEYWORDS so a chunk dropped here would also be dropped by
# the metadata strip; kept local to avoid pulling library internals into the GUI).
_AI_TEXT_MARKERS = (
    "parameters",
    "prompt",
    "negative_prompt",
    "postprocessing",
    "extras",
    "workflow",
    "sampler",
    "cfg_scale",
    "seed",
    "dream",
    "comfy",
    "invokeai",
    "stable_diffusion",
    "stablediffusion",
    "dall-e",
    "dalle",
    "midjourney",
    "firefly",
    "imagen",
    "gpt-",
    "gpt-image",
    "chatgpt",
    "openai",
    "lora",
)


def _is_ai_text_key(key: str) -> bool:
    folded = key.casefold()
    return any(marker in folded for marker in _AI_TEXT_MARKERS)


def _magic_matches(ext: str, head: bytes, pil_format: str | None) -> bool:
    if ext in (".jpg", ".jpeg"):
        return head.startswith(b"\xff\xd8")
    if ext == ".png":
        return head.startswith(b"\x89PNG\r\n\x1a\n")
    if ext == ".webp":
        return head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if ext == ".avif":
        return head[4:8] == b"ftyp" and head[8:12] in (b"avif", b"avis")
    if ext in (".heic", ".heif"):
        # HEIF brand: ftyp + (heic|heix|hevc|hevx|heim|heis|mif1) — distinct from AVIF.
        return head[4:8] == b"ftyp" and head[8:12] in (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1")
    if ext == ".bmp":
        return head[:2] == b"BM"
    if ext in (".tif", ".tiff"):
        return head[:4] in (b"II*\x00", b"MM\x00*")
    return pil_format is not None  # extension/format agreement already checked


def precheck_image(path: Path) -> tuple[bool, str | None, str | None, int | None]:
    """Admission precheck (5.1): (ok, format, chinese_reason, n_frames).

    Fails on: missing path, non-file, unsupported extension, unreadable /
    truncated file, extension-vs-actual-format mismatch, GIF, animation / multi-
    page input. A rejected input must never produce any output.
    """
    from PIL import Image

    # Register HEIF/AVIF opener (pillow-heif) so precheck can read .heic/.heif;
    # best-effort: absent plugin still falls back to the format gate below.
    try:
        import pillow_heif  # pyright: ignore[reportMissingImports]

        pillow_heif.register_heif_opener()
    except Exception:  # noqa: S110 - optional plugin
        pass

    if not path.exists():
        return False, None, "文件不存在", None
    if not path.is_file():
        return False, None, "路径不是文件", None
    ext = path.suffix.lower()
    # Decode FIRST so a rejected extension still reports the actual format
    # ("暂不支持 GIF" rather than a generic unsupported-extension message).
    try:
        with Image.open(path) as im:
            fmt = im.format
            n_frames = int(getattr(im, "n_frames", 1))
            im.load()  # full decode, not just the header
    except Exception as exc:  # truncated / corrupt / plugin failure
        if ext not in SUPPORTED_EXT_FORMATS:
            return False, None, f"不支持的图片格式: {ext or '(无扩展名)'}", None
        return False, None, f"图片无法完整解码: {type(exc).__name__}", None
    if fmt == "GIF":
        return False, "GIF", "暂不支持 GIF（单帧也可能损坏调色板语义）", n_frames
    if ext not in SUPPORTED_EXT_FORMATS:
        return False, fmt, f"不支持的图片格式: {ext or '(无扩展名)'}（实际为 {fmt}）", n_frames
    if fmt is None:
        return False, None, "无法识别图片的实际格式", n_frames
    if SUPPORTED_EXT_FORMATS[ext] != fmt:
        return False, fmt, f"扩展名 {ext} 与实际格式 {fmt} 不一致", n_frames
    if n_frames != 1:
        label = "动画" if fmt in ("PNG", "WEBP") else "多页"
        return False, fmt, f"暂不支持{label} {fmt}，共 {n_frames} 帧/页", n_frames
    try:
        head = path.read_bytes()[:16]
    except OSError:
        return False, fmt, "文件不可读", n_frames
    if not _magic_matches(ext, head, fmt):
        return False, fmt, f"文件头与实际格式 {fmt} 不一致", n_frames
    return True, fmt, None, n_frames


# ── Exclusive naming & atomic publish (5.5, BUG-04) ─────────────────────────


def next_exclusive_name(directory: Path, stem: str, suffix: str) -> Path:
    """Reserve the next free ``<stem>_clean[._n]<suffix>`` name via O_EXCL.

    The reservation IS created (an empty placeholder owned by this process), so
    two racing threads can never pick the same name, the counter is unlimited
    (no 999 cap, no ``_clean_final`` fallback) and pre-existing files are never
    touched. The final publish atomically replaces this placeholder.
    """
    directory.mkdir(parents=True, exist_ok=True)
    for i in itertools.count():
        name = f"{stem}_clean{suffix}" if i == 0 else f"{stem}_clean_{i}{suffix}"
        target = directory / name
        try:
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return target
        except FileExistsError:
            continue
    raise RuntimeError("unreachable: itertools.count is unbounded")  # pragma: no cover


def _temp_sibling(reserved: Path) -> Path:
    """Same-directory temp file whose extension matches the final format (5.5.1)."""
    suffix = reserved.suffix.lower()
    return reserved.parent / f".{reserved.name}.{os.getpid()}{os.urandom(3).hex()}{suffix}"


# ── Background scanning (5.7, BUG-09/BUG-10) ────────────────────────────────


def _walk_images(root: Path, onerror: Any = None) -> Iterable[Path]:
    for dirpath, dirnames, filenames in os.walk(root, onerror=onerror):
        dirnames.sort(key=str.casefold)
        for name in sorted(filenames, key=str.casefold):
            p = Path(dirpath) / name
            if p.suffix.lower() in SUPPORTED_EXT_FORMATS:
                yield p


def scan_paths(inputs: Iterable[str | Path]) -> list[Path]:
    """Resolve, de-duplicate (case-insensitively on Windows) and order inputs.

    Top-level input order wins; inside a directory the walk is sorted. A
    directory whose NAME ends in ``.png`` is walked as a directory, never
    treated as a file. ``*_clean*.png`` user files are legal inputs and are NOT
    filtered. Missing / unreadable inputs are skipped.
    """
    return scan_paths_with_warnings(inputs)[0]


def scan_paths_with_warnings(inputs: Iterable[str | Path]) -> tuple[list[Path], list[str]]:
    """Like :func:`scan_paths`, but also returns human-readable warnings for
    directories that could not be enumerated (permission errors, broken links).

    Runs on a background thread (the GUI never blocks the Tk main thread on it).
    """
    seen: set[str] = set()
    result: list[Path] = []
    warnings: list[str] = []

    def add(p: Path) -> None:
        try:
            resolved = p.resolve()
        except OSError as exc:
            warnings.append(f"路径不可访问: {p} ({exc})")
            return
        key = os.path.normcase(str(resolved))
        if key in seen:
            return
        seen.add(key)
        result.append(resolved)

    def on_walk_error(exc: OSError) -> None:
        warnings.append(f"目录扫描失败: {getattr(exc, 'filename', exc)} ({exc})")

    for inp in inputs:
        p = Path(inp)
        if p.is_dir():
            for q in _walk_images(p, onerror=on_walk_error):
                add(q)
        elif p.is_file():
            add(p)
        elif not p.exists():
            warnings.append(f"路径不存在: {p}")
    return result, warnings


def parse_region(text: str) -> tuple[int, int, int, int] | None:
    """Parse ``x,y,width,height`` from the GUI field; None when empty/invalid.

    Invalid input raises ValueError so the caller can show a message instead of
    silently ignoring the user's region.
    """
    text = (text or "").strip()
    if not text:
        return None
    parts = [p.strip() for p in text.replace("，", ",").split(",")]
    if len(parts) != 4:
        raise ValueError("区域格式应为: x,y,宽,高（4 个数字）")
    try:
        x, y, w, h = (int(p) for p in parts)
    except ValueError as exc:
        raise ValueError("区域必须是整数坐标") from exc
    if x < 0 or y < 0 or w <= 0 or h <= 0:
        raise ValueError("区域坐标必须为正（x,y>=0，宽高>0）")
    return x, y, w, h


# ── Structured result helpers (5.6, BUG-08) ────────────────────────────────


def selftest_exit_code(result: FileProcessResult) -> int:
    """0 for success/warning, non-zero for failed — from the structured status only."""
    return 0 if result.status in ("success", "warning") else 1


def format_batch_summary(results: Iterable[FileProcessResult]) -> str:
    counts = {"success": 0, "warning": 0, "failed": 0, "cancelled": 0}
    for r in results:
        counts[r.status] += 1
    return (
        f"处理结束：成功 {counts['success']}，警告 {counts['warning']}，"
        f"失败 {counts['failed']}，取消 {counts['cancelled']}"
    )


# ── Protected-metadata snapshot & write-back (5.3/5.4) ───────────────────────


# PIL adds codec-internal keys to Image.info (JFIF unit/density, TIFF resolution,
# quantization tables, ...) that are regenerated on re-encode and are NOT user
# metadata; they must not be treated as protected text and fail the verification.
_PIL_INTERNAL_INFO_KEYS = frozenset(
    {
        "exif",
        "icc_profile",
        "dpi",
        "gamma",
        "progression",
        "saved",
        "jfif",
        "jfif_version",
        "jfif_unit",
        "jfif_density",
        "resolution",
        "thumbnail",
        "adobe",
        "adobe_transform",
        "frames",
        "loop",
        "duration",
        "transparency",
        "icc",
        "progressive",
        "quality",
        "optimize",
    }
)

# IFD pointer tags that piexif.dump rewrites on encode; comparing them between the
# pre-snapshot and the output would always fail.
_EXIF_POINTER_TAGS = frozenset({0x8769, 0x8825, 0xA005})  # ExifIFD / GPSInfo / Interop

# XMP chunks are the AI generators' primary metadata carrier (C2PA pointers,
# generation parameters). The tool's job is to strip AI provenance, so XMP is
# treated as AI-related: not snapshotted, not written back, not verified. A
# hand-written non-AI XMP block is lost on a pixel pass — accepted trade-off.
_XMP_TEXT_KEYS = frozenset({"xmp", "XML:com.adobe.xmp"})


def _collect_protected_metadata(source: Path, *, exclude_ai_exif: bool) -> dict[str, Any]:
    """Snapshot the protected fields BEFORE processing, for later equivalence check.

    Shape: ``{"dpi", "icc_profile", "text": {..}, "exif": {"0th"/"Exif"/"GPS"/"Interop"}}``.
    Only string/bytes text chunks count as protected text (codec-internal fields
    like ``jfif_density``/``resolution`` are excluded). The EXIF snapshot skips the
    Orientation tag (physical rotation may legally change it to 1), the IFD pointer
    tags (piexif rewrites them on encode) and, when ``exclude_ai_exif``, any tag
    whose value names an AI generator (the strip is allowed to delete those).
    GPS and Interop IFD payloads are kept so camera locations survive a pixel pass.
    """
    from PIL import Image

    snapshot: dict[str, Any] = {"dpi": None, "icc_profile": None, "text": {}, "exif": {}}
    with Image.open(source) as src:
        src.load()
        info = src.info
        snapshot["dpi"] = info.get("dpi")
        snapshot["icc_profile"] = info.get("icc_profile")
        text: dict[str, Any] = snapshot["text"]
        for k, v in info.items():
            if (
                isinstance(k, str)
                and k not in _PIL_INTERNAL_INFO_KEYS
                and k not in _XMP_TEXT_KEYS
                and isinstance(v, (str, bytes))
                and not _is_ai_text_key(k)
            ):
                text[k] = v
        exif_bytes = info.get("exif")
    if isinstance(exif_bytes, bytes):
        import piexif

        try:
            exif_dict = piexif.load(exif_bytes)
        except Exception:
            exif_dict = None
        if exif_dict is not None:
            exif_snapshot: dict[str, Any] = {"0th": {}, "Exif": {}, "GPS": {}, "Interop": {}}
            snapshot["exif"] = exif_snapshot
            for ifd_key in ("0th", "Exif", "GPS", "Interop"):
                ifd: dict[int, Any] = exif_dict.get(ifd_key) or {}
                for tag, value in ifd.items():
                    if tag == piexif.ImageIFD.Orientation and ifd_key == "0th":
                        continue
                    if tag in _EXIF_POINTER_TAGS:
                        # IFD pointers (ExifIFD/GPSInfo/Interop) are rewritten by
                        # piexif.dump; comparing them would always fail.
                        continue
                    if exclude_ai_exif and _exif_value_is_ai(value):
                        continue
                    exif_snapshot[ifd_key][tag] = value
    return snapshot


def _exif_value_is_ai(value: object) -> bool:
    if not isinstance(value, (bytes, str)):
        return False
    lowered = value.decode(errors="ignore").casefold() if isinstance(value, bytes) else value.casefold()
    return any(marker in lowered for marker in _AI_TEXT_MARKERS)


def _save_pixel_output(
    tmp: Path,
    source: Path,
    bgr: NDArray[Any],
    alpha: NDArray[Any] | None,
    fmt: str,
    orientation: int,
    protected: dict[str, Any] | None = None,
) -> None:
    """Encode the (display-direction) result to ``tmp`` keeping protected metadata.

    Orientation is written as 1 (or dropped) because the pixels are already
    physically rotated. Everything else — ordinary EXIF, standard text chunks,
    ICC profile, DPI, alpha — is carried over unchanged. ``protected`` is the
    pre-processing snapshot (skipped re-reading when provided).
    """
    import numpy as np
    import piexif
    from PIL import Image
    from PIL.PngImagePlugin import PngInfo

    if protected is None:
        protected = _collect_protected_metadata(source, exclude_ai_exif=False)
    dpi = protected.get("dpi")
    icc = protected.get("icc_profile")
    text_items: dict[str, Any] = protected.get("text") or {}
    exif_snapshot: dict[str, Any] = protected.get("exif") or {}
    exif_bytes = None
    if exif_snapshot:
        import piexif

        exif_dict: dict[str, Any] = {
            "0th": dict(exif_snapshot.get("0th") or {}),
            "Exif": dict(exif_snapshot.get("Exif") or {}),
            "GPS": dict(exif_snapshot.get("GPS") or {}),
            "1st": {},
            "Interop": dict(exif_snapshot.get("Interop") or {}),
            "thumbnail": None,
        }
        try:
            exif_dict["0th"][piexif.ImageIFD.Orientation] = 1
            exif_bytes = piexif.dump(exif_dict)
        except Exception:
            exif_bytes = None  # unparseable EXIF: drop it (orientation == 1 semantics)

    rgb = bgr[..., ::-1]
    pil = Image.fromarray(np.dstack([rgb, alpha]), "RGBA") if alpha is not None else Image.fromarray(rgb, "RGB")

    kwargs: dict[str, Any] = {"format": fmt}
    if fmt == "JPEG":
        kwargs.update(quality=100, subsampling=0)
        if dpi:
            kwargs["dpi"] = dpi
        if icc:
            kwargs["icc_profile"] = icc
        if exif_bytes:
            kwargs["exif"] = exif_bytes
        comment = text_items.get("comment")
        if isinstance(comment, (str, bytes)):
            kwargs["comment"] = comment
        if pil.mode == "RGBA":
            pil = pil.convert("RGB")
    elif fmt == "PNG":
        if dpi:
            kwargs["dpi"] = dpi
        if icc:
            kwargs["icc_profile"] = icc
        if exif_bytes:
            kwargs["exif"] = exif_bytes
        if text_items:
            pnginfo = PngInfo()
            for k, v in text_items.items():
                if isinstance(v, str):
                    pnginfo.add_text(k, v)
            kwargs["pnginfo"] = pnginfo
    elif fmt == "WEBP":
        kwargs.update(quality=100, method=6)
        if icc:
            kwargs["icc_profile"] = icc
        if exif_bytes:
            kwargs["exif"] = exif_bytes
    elif fmt == "AVIF":
        kwargs["quality"] = 100
        if exif_bytes:
            kwargs["exif"] = exif_bytes
    elif fmt == "HEIF":
        kwargs["quality"] = 100
        if icc:
            kwargs["icc_profile"] = icc
        if exif_bytes:
            kwargs["exif"] = exif_bytes
    elif fmt == "TIFF":
        if dpi:
            kwargs["dpi"] = dpi
        if exif_bytes:
            kwargs["exif"] = exif_bytes
    # BMP: no standard metadata fields to carry.
    pil.save(tmp, **kwargs)


# ── Output verification (5.4) ───────────────────────────────────────────────


def verify_output(
    tmp: Path,
    fmt: str,
    expected_size: tuple[int, int] | None,
    expected_alpha: bool,
) -> list[str]:
    """Re-read the temp output and list every verification failure (empty = pass)."""
    from PIL import Image

    problems: list[str] = []
    try:
        data = tmp.read_bytes()
    except OSError as exc:
        return [f"输出不可读: {exc}"]
    if not data:
        problems.append("输出为空")
    if not _magic_matches(tmp.suffix.lower(), data[:16], fmt):
        problems.append("输出魔数与扩展名不一致")
    try:
        with Image.open(tmp) as im:
            im.load()
            n_frames = int(getattr(im, "n_frames", 1))
            if n_frames != 1:
                problems.append(f"输出帧数 != 1（实际 {n_frames}）")
            if im.format != fmt:
                problems.append(f"输出实际格式 {im.format} != 预期 {fmt}")
            if expected_size is not None and (im.width, im.height) != expected_size:
                problems.append(f"输出尺寸 {(im.width, im.height)} != 预期 {expected_size}")
            if expected_alpha and im.mode not in ("RGBA", "LA", "PA"):
                problems.append("透明通道丢失")
    except Exception as exc:
        problems.append(f"输出无法完整解码: {exc}")
    return problems


def _verify_protected_metadata(tmp: Path, expected: dict[str, Any]) -> list[str]:
    """BUG-06: compare the preserved protected fields on the output (empty = pass)."""
    from PIL import Image

    problems: list[str] = []
    with Image.open(tmp) as im:
        im.load()
        info = im.info
        exif = im.getexif()
        expected_dpi = expected.get("dpi")
        if expected_dpi is not None:
            dpi = info.get("dpi")
            if dpi is None or tuple(round(float(x)) for x in dpi) != tuple(round(float(x)) for x in expected_dpi):
                problems.append(f"DPI 丢失/变化: {dpi} != {expected_dpi}")
        if expected.get("icc_profile") is not None and "icc_profile" not in info:
            problems.append("ICC 配置丢失")
        for k, v in (expected.get("text") or {}).items():
            if info.get(k) != v:
                problems.append(f"文本字段 {k!r} 丢失/变化")
        exif_snapshot: dict[str, Any] = expected.get("exif") or {}
        for tag, v in (exif_snapshot.get("0th") or {}).items():
            if not _exif_value_equal(exif.get(tag), v):
                problems.append(f"EXIF 0th 字段 0x{tag:04X} 丢失/变化")
        exif_ifd_snap = exif_snapshot.get("Exif") or {}
        if exif_ifd_snap:
            try:
                exif_ifd = exif.get_ifd(0x8769)
            except KeyError:
                exif_ifd = {}
            for tag, v in exif_ifd_snap.items():
                if not _exif_value_equal(exif_ifd.get(tag), v):
                    problems.append(f"EXIF Exif 字段 0x{tag:04X} 丢失/变化")
        gps_snap = exif_snapshot.get("GPS") or {}
        if gps_snap:
            try:
                gps_ifd = exif.get_ifd(0x8825)
            except KeyError:
                gps_ifd = {}
            for tag, v in gps_snap.items():
                if not _exif_value_equal(gps_ifd.get(tag), v):
                    problems.append(f"EXIF GPS 字段 0x{tag:04X} 丢失/变化")
        interop_snap = exif_snapshot.get("Interop") or {}
        if interop_snap:
            try:
                interop_ifd = exif.get_ifd(0xA005)
            except KeyError:
                interop_ifd = {}
            for tag, v in interop_snap.items():
                if not _exif_value_equal(interop_ifd.get(tag), v):
                    problems.append(f"EXIF Interop 字段 0x{tag:04X} 丢失/变化")
    return problems


def _exif_value_equal(got: object, expected: object) -> bool:
    if got == expected:
        return True
    # PIL returns ASCII bytes as str; piexif values are bytes — normalize both sides.
    if isinstance(expected, bytes) and isinstance(got, str):
        return got == expected.decode(errors="replace")
    if isinstance(expected, str) and isinstance(got, bytes):
        return got.decode(errors="replace") == expected
    # Rational arrays: piexif keeps ((31, 1), (13, 1), (0, 1)); PIL reads them
    # back as (31.0, 13.0, 0.0). Compare element-wise on the normalized values.
    if isinstance(expected, (tuple, list)) and isinstance(got, (tuple, list)):
        try:
            return [_rational_flat(x) for x in expected] == [_rational_flat(x) for x in got]
        except Exception:
            return False
    return False


def _rational_flat(value: object) -> object:
    """Reduce a piexif rational ``(num, den)`` pair to its float value; pass others through."""
    if isinstance(value, (tuple, list)) and len(value) == 2 and all(isinstance(x, (int, float)) for x in value):
        return value[0] / value[1] if value[1] else 0.0
    return value


# ── Per-file pipeline (5.1-5.6) ─────────────────────────────────────────────


class _PipelineError(Exception):
    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage
        self.message = message


def _report_text(report: Any) -> str:
    verdict = {True: "AI 生成", False: "非 AI", None: "无法判定"}[report.is_ai_generated]
    parts = [f"检测: {verdict}  (置信度 {report.confidence})"]
    if report.platform:
        parts.append(f"平台: {report.platform}")
    parts.extend(f"标记: {wm}" for wm in report.watermarks)
    return "\n".join(parts)


def run_pipeline(
    source: Path,
    out_dir: Path | None,
    *,
    do_visible: bool,
    do_report: bool,
    do_strip: bool,
    stop_event: threading.Event,
    log_q: queue.Queue[str],
    erase_region: tuple[int, int, int, int] | None = None,
    strict: bool = False,
    remove_all_metadata: bool = False,
) -> FileProcessResult:
    """Process one image through precheck -> (pixels) -> metadata -> verify -> publish.

    The final name is only produced after every verification passed; on any
    failure the placeholder and temp file are removed and nothing is published.
    """
    from remove_ai_watermarks import image_io

    ok, fmt, reason, _ = precheck_image(source)
    if not ok:
        msg = f"预检失败: {reason or '未知原因'}"
        return FileProcessResult(source, "failed", None, (msg,), "input", reason)
    if not (do_visible or do_strip or do_report or erase_region):
        return FileProcessResult(source, "failed", None, ("未选择任何处理步骤",), "input", "no-op")

    messages: list[str] = []
    reserved: Path | None = None
    tmp: Path | None = None
    published = False
    report_failed = False
    protected: dict[str, Any] | None = None
    # The single EXIF-normalized pixel decode: detection, removal and the
    # output-size/alpha check all consume THIS array (PRD: one decode per file).
    decoded: Any | None = None
    try:
        if do_visible or do_strip or erase_region:
            assert fmt is not None
            reserved = next_exclusive_name(out_dir or source.parent, source.stem, source.suffix.lower())
            if do_visible or erase_region:
                tmp = _temp_sibling(reserved)
            # Snapshot BEFORE any processing; the strip is allowed to delete AI
            # EXIF fields and the pixel stage to normalize Orientation.
            protected = _collect_protected_metadata(source, exclude_ai_exif=True)

        if do_visible or erase_region:
            decoded = image_io.read_normalized(source)
            if decoded is None:
                raise _PipelineError("pixels", "统一像素解码失败")
            if do_report:
                from remove_ai_watermarks.identify import identify

                try:
                    report = identify(source, check_visible=True, check_invisible=False, pixels=decoded.bgr)
                    messages.append(_report_text(report))
                except Exception as exc:
                    report_failed = True
                    messages.append(f"检测失败: {exc}")
            result_bgr = decoded.bgr
            if do_visible:
                from remove_ai_watermarks.api import remove_visible, visible_provenance

                provenance = visible_provenance(source)
                result_bgr, removed = remove_visible(
                    decoded.bgr,
                    None,
                    alpha=decoded.alpha,
                    provenance=provenance,
                    sensitivity="strict" if strict else "auto",
                    backend="cv2",
                    strip_metadata=False,
                    write_noop=True,
                )
                if removed:
                    messages.append(f"可见水印移除: {', '.join(removed)}")
                else:
                    messages.append("可见水印: 未检测到")
            if erase_region is not None:
                from remove_ai_watermarks.region_eraser import erase

                result_bgr = erase(result_bgr, boxes=[erase_region], backend="cv2")
                messages.append(f"区域擦除: {erase_region[0]},{erase_region[1]} {erase_region[2]}x{erase_region[3]}")
            assert tmp is not None and fmt is not None
            _save_pixel_output(tmp, source, result_bgr, decoded.alpha, fmt, decoded.orientation, protected=protected)
        elif do_report:
            from remove_ai_watermarks.identify import identify

            try:
                report = identify(source, check_visible=True, check_invisible=False)
                messages.append(_report_text(report))
            except Exception as exc:
                report_failed = True
                messages.append(f"检测失败: {exc}")

        if do_strip:
            from remove_ai_watermarks.metadata import strip_and_verify

            if tmp is not None:
                work, target = tmp, tmp
            else:
                assert reserved is not None
                work, target = source, reserved  # overwrites this process's placeholder
            _out_path, surviving = strip_and_verify(work, target, keep_standard=not remove_all_metadata)
            if surviving:
                raise _PipelineError("metadata", f"AI 元数据残留，无法发布: {', '.join(sorted(surviving))}")
            messages.append("AI 元数据已清理")

        if fmt is not None and (tmp is not None or reserved is not None):
            expected_size: tuple[int, int] | None
            expected_alpha = False
            verify_target = tmp if tmp is not None else reserved
            assert verify_target is not None
            if do_visible or erase_region:
                # `decoded` is already guaranteed non-None on this branch (it raised
                # otherwise), and the write goes to tmp, never back to source — so
                # re-decoding just to learn the size/alpha is pure waste.
                assert decoded is not None
                expected_size = (decoded.bgr.shape[1], decoded.bgr.shape[0])
                expected_alpha = decoded.alpha is not None
            else:
                from PIL import Image as _PILImage

                with _PILImage.open(source) as im:
                    expected_size = (im.width, im.height)
            problems = verify_output(verify_target, fmt, expected_size, expected_alpha)
            if protected is not None:
                problems.extend(_verify_protected_metadata(verify_target, protected))
            if problems:
                raise _PipelineError("output", "输出验证失败: " + "；".join(problems))
            if tmp is not None:
                assert reserved is not None
                os.replace(tmp, reserved)
                tmp = None
            published = True
            assert reserved is not None
            messages.append(f"已保存 -> {reserved.name}")

        status: FileStatus = "warning" if report_failed else "success"
        return FileProcessResult(source, status, reserved, tuple(messages), None, None)
    except _PipelineError as exc:
        messages.append(exc.message)
        return FileProcessResult(source, "failed", None, tuple(messages), exc.stage, exc.message)
    except Exception as exc:
        msg = f"处理异常: {type(exc).__name__}: {exc}"
        messages.append(msg)
        return FileProcessResult(source, "failed", None, tuple(messages), "internal", msg)
    finally:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        if reserved is not None and not published:
            with contextlib.suppress(OSError):
                os.unlink(reserved)


# ── Batch runner (5.8, BUG-03) ──────────────────────────────────────────────


class BatchRunner:
    """Non-daemon worker that processes the queue; the Tk thread never blocks on it.

    ``stop_event`` is only checked at file boundaries: the current file finishes
    atomically, everything not started becomes ``cancelled``.
    """

    def __init__(
        self,
        files: list[Path],
        out_dir: Path | None,
        *,
        do_visible: bool,
        do_report: bool,
        do_strip: bool,
        stop_event: threading.Event,
        log_q: queue.Queue[str],
        epoch: int = 0,
        erase_region: tuple[int, int, int, int] | None = None,
        strict: bool = False,
        remove_all_metadata: bool = False,
    ) -> None:
        self.files = list(files)
        self.out_dir = out_dir
        self.do_visible = do_visible
        self.do_report = do_report
        self.do_strip = do_strip
        self.stop_event = stop_event
        self.log_q = log_q
        self.epoch = epoch
        self.erase_region = erase_region
        self.strict = strict
        self.remove_all_metadata = remove_all_metadata
        self._results: list[FileProcessResult] | None = None
        self._lock = threading.Lock()
        self.thread = threading.Thread(target=self.run, name="gui-batch", daemon=False)

    def start(self) -> None:
        self.thread.start()

    def run(self) -> None:
        results: list[FileProcessResult] = []
        total = len(self.files)
        for i, src in enumerate(self.files, start=1):
            if self.stop_event.is_set():
                results.append(FileProcessResult(src, "cancelled", None, ("窗口已关闭，任务未开始",), None, None))
                continue
            self.log_q.put(f"===== [{i}/{total}] {src.name} =====")
            self.log_q.put(f"!STATUS:{self.epoch}:({i}/{total}) {src.name}")
            result = run_pipeline(
                src,
                self.out_dir,
                do_visible=self.do_visible,
                do_report=self.do_report,
                do_strip=self.do_strip,
                stop_event=self.stop_event,
                log_q=self.log_q,
                erase_region=self.erase_region,
                strict=self.strict,
                remove_all_metadata=self.remove_all_metadata,
            )
            results.append(result)
            self.log_q.put(f"!RESULT:{self.epoch}:{i - 1}:{result.status}")
            self.log_q.put(f"!SUMMARYLINE:{_result_line(result)}")
        with self._lock:
            self._results = results
        self.log_q.put(f"!DONE:{self.epoch}")

    def join_result(self) -> list[FileProcessResult]:
        self.thread.join()
        with self._lock:
            assert self._results is not None
            return list(self._results)

    @classmethod
    def run_sync(
        cls,
        *,
        files: list[Path],
        out_dir: Path | None,
        do_visible: bool,
        do_report: bool,
        do_strip: bool,
        stop_event: threading.Event,
        log_q: queue.Queue[str],
        epoch: int = 0,
        erase_region: tuple[int, int, int, int] | None = None,
        strict: bool = False,
        remove_all_metadata: bool = False,
    ) -> list[FileProcessResult]:
        runner = cls(
            files,
            out_dir,
            do_visible=do_visible,
            do_report=do_report,
            do_strip=do_strip,
            stop_event=stop_event,
            log_q=log_q,
            epoch=epoch,
            erase_region=erase_region,
            strict=strict,
            remove_all_metadata=remove_all_metadata,
        )
        runner.run()
        with runner._lock:
            assert runner._results is not None
            return list(runner._results)


def _parse_epoch(token: str) -> int | None:
    """Parse a leading integer epoch from a message token; None when malformed."""
    try:
        return int(token)
    except ValueError:
        return None


def _split_epoch(token: str) -> tuple[int | None, str]:
    """Split ``<epoch>:<rest>``; a malformed epoch yields (None, token)."""
    if ":" in token:
        head, _, rest = token.partition(":")
        epoch = _parse_epoch(head)
        if epoch is not None:
            return epoch, rest
    return None, token


def _result_line(result: FileProcessResult) -> str:
    out = f" -> {result.output.name}" if result.output is not None else ""
    if result.error:
        return f"    [{result.status}] {result.source.name}{out}: {result.error}"
    return f"    [{result.status}] {result.source.name}{out}"


# ── Tk shell (thin adapter) ─────────────────────────────────────────────────


def _apply_window_icon(root: Any) -> None:
    """Set the Tk window/taskbar icon (not just the EXE file icon).

    Packaged runs extract the icon from the frozen executable itself (Windows
    supports an .exe path in ``iconbitmap``); source runs use the checked-in
    .ico next to this script. Best-effort: a missing icon must never crash.
    """
    try:
        if getattr(sys, "frozen", False):
            root.iconbitmap(sys.executable)
        else:
            ico = Path(__file__).resolve().parent / "gui_icon.ico"
            if ico.is_file():
                root.iconbitmap(str(ico))
    except Exception:  # noqa: S110 - icon is cosmetic
        pass


class App:
    def __init__(self, root: Any, initial_paths: list[str], dnd: bool) -> None:
        import queue as _queue
        from tkinter import (
            BOTH,
            BOTTOM,
            LEFT,
            RIGHT,
            StringVar,
            Text,
            X,
            ttk,
        )

        self._ttk = ttk
        _apply_window_icon(root)
        self.root = root
        root.title("印消 by 林小伙")
        w, h = 820, 640
        x = (root.winfo_screenwidth() - w) // 2
        y = (root.winfo_screenheight() - h) // 2
        root.geometry(f"{w}x{h}+{x}+{y}")
        root.minsize(640, 480)

        self.files: list[Path] = []
        self.scan_q: queue.Queue[tuple[str, list[Path], list[str]]] = _queue.Queue()
        self.log_q: queue.Queue[str] = _queue.Queue()
        self.scan_busy = False
        self.batch_epoch = 0
        self.batch_runner: BatchRunner | None = None
        self.stop_event = threading.Event()
        self.closing = False
        self._counts: dict[str, int] = {"success": 0, "warning": 0, "failed": 0, "cancelled": 0}
        self._total = 0

        toolbar = ttk.Frame(root)
        toolbar.pack(fill=X, padx=8, pady=(8, 4))
        self.btn_add_files = ttk.Button(toolbar, text="添加文件", command=self.add_files)
        self.btn_add_files.pack(side=LEFT, padx=(0, 4))
        self.btn_add_folder = ttk.Button(toolbar, text="添加文件夹", command=self.add_folder)
        self.btn_add_folder.pack(side=LEFT, padx=4)
        self.btn_remove = ttk.Button(toolbar, text="移除选中", command=self.remove_selected)
        self.btn_remove.pack(side=LEFT, padx=4)
        self.btn_clear = ttk.Button(toolbar, text="清空", command=self.clear_files)
        self.btn_clear.pack(side=LEFT, padx=4)
        ttk.Label(toolbar, text="支持拖拽文件/文件夹到窗口").pack(side=RIGHT)
        ttk.Button(toolbar, text="关于", command=self.show_about).pack(side=RIGHT, padx=4)

        self.tree = ttk.Treeview(root, columns=("path", "status"), show="headings", height=10)
        self.tree.heading("path", text="文件")
        self.tree.heading("status", text="状态")
        self.tree.column("path", width=560, anchor="w")
        self.tree.column("status", width=180, anchor="w")
        self.tree.pack(fill=BOTH, expand=True, padx=8, pady=4)

        opts = ttk.LabelFrame(root, text="处理选项")
        opts.pack(fill=X, padx=8, pady=4)
        self.var_strip = StringVar(value="1")
        self.var_visible = StringVar(value="0")
        self.var_report = StringVar(value="1")
        ttk.Checkbutton(opts, text="清除 AI 元数据（推荐）", variable=self.var_strip, onvalue="1", offvalue="0").pack(
            side=LEFT, padx=8
        )
        ttk.Checkbutton(opts, text="去除可见 AI 水印", variable=self.var_visible, onvalue="1", offvalue="0").pack(
            side=LEFT, padx=8
        )
        ttk.Checkbutton(opts, text="处理前显示检测报告", variable=self.var_report, onvalue="1", offvalue="0").pack(
            side=LEFT, padx=8
        )
        self.var_strict = StringVar(value="0")
        ttk.Checkbutton(opts, text="严格模式（少误删）", variable=self.var_strict, onvalue="1", offvalue="0").pack(
            side=LEFT, padx=8
        )
        self.var_remove_all_meta = StringVar(value="0")
        ttk.Checkbutton(
            opts,
            text="清除普通元数据(作者/标题等)",
            variable=self.var_remove_all_meta,
            onvalue="1",
            offvalue="0",
        ).pack(side=LEFT, padx=8)

        # 区域擦除: 固定区域批量擦除 (x,y,w,h 逗号分隔). 留空=不擦除.
        region_row = ttk.Frame(root)
        region_row.pack(fill=X, padx=8, pady=(2, 4))
        ttk.Label(region_row, text="区域擦除 x,y,宽,高:").pack(side=LEFT)
        self.var_region = StringVar(value="")
        ttk.Entry(region_row, textvariable=self.var_region, width=24).pack(side=LEFT, padx=8)
        ttk.Label(region_row, text="（应用到所有文件同一位置，留空则不擦除）").pack(side=LEFT)

        out_row = ttk.Frame(root)
        out_row.pack(fill=X, padx=8, pady=4)
        ttk.Label(out_row, text="输出目录:").pack(side=LEFT)
        self.var_outdir = StringVar(value="")
        ttk.Entry(out_row, textvariable=self.var_outdir).pack(side=LEFT, fill=X, expand=True, padx=8)
        ttk.Button(out_row, text="选择", command=self.pick_outdir).pack(side=RIGHT)

        self.run_btn = ttk.Button(root, text="开始处理", command=self.start)
        self.run_btn.pack(fill=X, padx=8, pady=4)

        prog_row = ttk.Frame(root)
        prog_row.pack(fill=X, padx=8, pady=(2, 4))
        self.progress = ttk.Progressbar(prog_row, mode="determinate", maximum=100)
        self.progress.pack(side=LEFT, fill=X, expand=True)
        self.counts_label = ttk.Label(prog_row, text="")
        self.counts_label.pack(side=RIGHT, padx=(8, 0))

        self.log = Text(root, height=12, state="disabled", wrap="word")
        self.log.pack(fill=BOTH, expand=True, padx=8, pady=(4, 8))

        self.status = StringVar(value="就绪")
        ttk.Label(root, textvariable=self.status, relief="sunken", anchor="w").pack(
            fill=X, side=BOTTOM, padx=8, pady=(0, 8)
        )

        if dnd:
            try:
                from tkinterdnd2 import DND_FILES

                root.drop_target_register(DND_FILES)
                root.dnd_bind("<<Drop>>", self._on_drop)
            except Exception:  # noqa: S110 - DND failing must not kill the app
                pass

        for p in initial_paths:
            self._enqueue_scan([Path(p)])
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self._poll_log)
        root.after(100, self._poll_scan)

    # ── input / scanning ──

    def _enqueue_scan(self, inputs: list[Path]) -> None:
        if self.scan_busy or self.busy:
            return
        self._set_scan_busy(True)
        self.status.set("正在扫描...")
        # Scan is daemon: it only collects file lists (no atomic writes), so a
        # window close must not keep the process alive on a huge directory.
        # The BATCH thread stays non-daemon by design (PRD 5.8).
        self.scan_thread = threading.Thread(
            target=self._scan_worker, args=(list(inputs),), name="gui-scan", daemon=True
        )
        self.scan_thread.start()

    def _scan_worker(self, inputs: list[Path]) -> None:
        files, warnings = scan_paths_with_warnings(inputs)
        self.scan_q.put(("FILES", files, warnings))

    def _poll_scan(self) -> None:
        try:
            while True:
                kind, payload, warnings = self.scan_q.get_nowait()
                if kind == "FILES":
                    self._add_scanned(payload)
                    self._set_scan_busy(False)
                    self.status.set("就绪")
                    for w in warnings:
                        self._append_log(f"扫描警告: {w}")
                    if not payload:
                        from tkinter import messagebox

                        messagebox.showinfo("提示", "没有找到支持的图片文件")
        except queue.Empty:
            pass
        with contextlib.suppress(Exception):
            self.root.after(100, self._poll_scan)

    def _add_scanned(self, found: list[Path]) -> None:
        seen = {os.path.normcase(str(p.resolve())) for p in self.files}
        for p in found:
            key = os.path.normcase(str(p.resolve()))
            if key not in seen:
                seen.add(key)
                self.files.append(p)
        self._refresh_tree()

    def _on_drop(self, event: Any) -> None:
        if self.busy or self.scan_busy:
            return
        raw = getattr(event, "data", "") or ""
        # Tk's native splitter: spaces/quotes/braces handled by Tcl, not a regex.
        paths = [Path(p) for p in self.root.tk.splitlist(raw)]
        self._enqueue_scan(paths)

    def add_files(self) -> None:
        from tkinter import filedialog

        chosen = filedialog.askopenfilenames(title="选择图片")
        if chosen:
            self._enqueue_scan([Path(p) for p in chosen])

    def add_folder(self) -> None:
        from tkinter import filedialog

        chosen = filedialog.askdirectory(title="选择文件夹")
        if chosen:
            self._enqueue_scan([Path(chosen)])

    def remove_selected(self) -> None:
        indexes = sorted((self.tree.index(i) for i in self.tree.selection()), reverse=True)
        for idx in indexes:
            if 0 <= idx < len(self.files):
                del self.files[idx]
        self._refresh_tree()

    def clear_files(self) -> None:
        self.files.clear()
        self._refresh_tree()

    def _refresh_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for p in self.files:
            self.tree.insert("", TK_END, values=(str(p), "等待"))

    def pick_outdir(self) -> None:
        from tkinter import filedialog

        chosen = filedialog.askdirectory(title="选择输出目录")
        if chosen:
            self.var_outdir.set(chosen)

    def show_about(self) -> None:
        from tkinter import messagebox

        try:
            from importlib import metadata

            ver = metadata.version("remove-ai-watermarks")
        except Exception:
            ver = "0.26.3"
        messagebox.showinfo(
            "关于 印消",
            f"印消 v{ver}\n\n"
            "AI 图片去水印工具：清除 AI 生成元数据、去除可见 AI 水印。\n\n"
            "作者：林小伙\n\n"
            "开源协议：Apache-2.0\n"
            "（基于 remove-ai-watermarks 社区项目）",
        )

    # ── batch lifecycle ──

    @property
    def busy(self) -> bool:
        return self.batch_runner is not None and self.batch_runner.thread.is_alive()

    def _set_busy(self, busy: bool) -> None:
        """Freeze list-editing + start buttons while a batch runs or a scan is
        in flight, so a click is never silently ignored (UX: disabled buttons
        show why)."""
        self._buttons_enabled = not busy and not self.scan_busy
        state = "normal" if self._buttons_enabled else "disabled"
        for btn in (self.btn_add_files, self.btn_add_folder, self.btn_remove, self.btn_clear, self.run_btn):
            btn.configure(state=state)

    def _set_scan_busy(self, scan_busy: bool) -> None:
        self.scan_busy = scan_busy
        self._set_busy(self.busy or scan_busy)

    def start(self) -> None:
        if self.busy or self.scan_busy:
            return
        if not self.files:
            from tkinter import messagebox

            messagebox.showwarning("提示", "请先添加图片")
            return
        try:
            erase_region = parse_region(self.var_region.get())
        except ValueError as exc:
            from tkinter import messagebox

            messagebox.showerror("区域格式错误", str(exc))
            return
        self._set_busy(True)
        self.status.set("处理中...")
        self._total = len(self.files)
        self._counts = {"success": 0, "warning": 0, "failed": 0, "cancelled": 0}
        self.progress.configure(maximum=max(self._total, 1), value=0)
        self._update_counts_label()
        out_dir = Path(self.var_outdir.get().strip()) if self.var_outdir.get().strip() else None
        self.stop_event.clear()
        self.batch_epoch += 1
        self.batch_runner = BatchRunner(
            list(self.files),
            out_dir,
            do_visible=self.var_visible.get() == "1",
            do_report=self.var_report.get() == "1",
            do_strip=self.var_strip.get() == "1",
            stop_event=self.stop_event,
            log_q=self.log_q,
            epoch=self.batch_epoch,
            erase_region=erase_region,
            strict=self.var_strict.get() == "1",
            remove_all_metadata=self.var_remove_all_meta.get() == "1",
        )
        self.batch_runner.start()

    def on_close(self) -> None:
        if self.busy:
            # Finish the current file, cancel the rest, then exit when !DONE lands.
            self.closing = True
            self.status.set("正在完成当前文件后退出...")
            self.stop_event.set()
            self.run_btn.configure(state="disabled")
            return
        self.root.destroy()

    def _poll_log(self) -> None:
        try:
            while True:
                msg = self.log_q.get_nowait()
                if not self.consume_batch_message(self, msg):
                    self._append_log(msg)
        except queue.Empty:
            pass
        with contextlib.suppress(Exception):
            self.root.after(100, self._poll_log)

    @classmethod
    def consume_batch_message(cls, owner: Any, msg: str) -> bool:
        """Consume one queue message with batch-epoch filtering.

        Control messages carry ``!PREFIX:<epoch>:...``; a message whose epoch is
        not the CURRENT batch's is dropped (a previous batch's straggler must
        never flip buttons, join the live runner or touch the tree). Returns
        True when the message was a control message (handled or dropped), False
        for plain log lines.
        """
        if msg.startswith("!STATUS:"):
            epoch, rest = _split_epoch(msg[len("!STATUS:") :])
            if epoch is not None and owner._is_current_epoch(epoch):
                owner.status.set(rest)
            return True
        if msg.startswith("!DONE:"):
            epoch = _parse_epoch(msg[len("!DONE:") :])
            if epoch is not None and owner._is_current_epoch(epoch):
                owner._on_batch_done(epoch)
            return True
        if msg.startswith("!RESULT:"):
            epoch, idx_str, status = msg[len("!RESULT:") :].split(":", 2)
            if owner._is_current_epoch(_parse_epoch(epoch)):
                with contextlib.suppress(ValueError):
                    owner._apply_result_line(int(idx_str), status)
            return True
        if msg.startswith("!SUMMARYLINE:"):
            owner._append_log(msg[len("!SUMMARYLINE:") :])
            return True
        return False

    def _is_current_epoch(self, epoch: int) -> bool:
        return epoch == self.batch_epoch

    def _on_batch_done(self, epoch: int) -> None:
        self._set_busy(False)
        if self.batch_runner is not None:
            summary = format_batch_summary(self.batch_runner.join_result())
            self.log_q.put(summary)
            self.status.set(summary)
        if self.closing:
            self.root.destroy()

    def _update_counts_label(self) -> None:
        c = self._counts
        text = f"成功 {c['success']} · 警告 {c['warning']} · 失败 {c['failed']} · 取消 {c['cancelled']} / {self._total}"
        self.counts_label.configure(text=text)

    def _apply_result_line(self, idx: int, status: str) -> None:
        items = self.tree.get_children()
        if 0 <= idx < len(items) and 0 <= idx < len(self.files):
            label = {
                "success": "成功",
                "warning": "警告",
                "failed": "失败",
                "cancelled": "已取消",
            }.get(status, status)
            self.tree.item(
                items[idx],
                values=(str(self.files[idx]), label),
            )
            if status in self._counts:
                self._counts[status] += 1
            self.progress.configure(value=min(idx + 1, self._total))
            self._update_counts_label()

    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert(TK_END, text + "\n")
        self._trim_log()
        self.log.see(TK_END)
        self.log.configure(state="disabled")

    def _trim_log(self) -> None:
        lines = int(self.log.index("end-1c").split(".")[0])
        if lines > 2000:
            self.log.delete("1.0", f"{lines - 2000}.0")


def _selftest(target: str) -> int:
    result = run_pipeline(
        Path(target),
        None,
        do_visible=True,
        do_report=True,
        do_strip=True,
        stop_event=threading.Event(),
        log_q=queue.Queue(),
    )
    lines = [
        f"TARGET: {target}",
        f"STATUS: {result.status}",
        f"OUTPUT: {result.output}",
        *(result.messages),
    ]
    Path("selftest.log").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return selftest_exit_code(result)


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "--selftest" and len(args) == 2:
        raise SystemExit(_selftest(args[1]))
    try:
        from tkinterdnd2 import TkinterDnD

        root = TkinterDnD.Tk()
        dnd = True
    except Exception:
        from tkinter import Tk

        root = Tk()
        dnd = False
    # Build the full UI while the root window is hidden, then show it once the
    # App geometry (centered) is applied — avoids the flash of a small default
    # window at the top-left that Tk maps at Tk() time.
    root.withdraw()
    App(root, args, dnd=dnd)
    root.update_idletasks()
    root.deiconify()
    root.mainloop()


if __name__ == "__main__":
    main()
