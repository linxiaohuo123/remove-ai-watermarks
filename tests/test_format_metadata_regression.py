"""Format-fidelity and metadata-preservation regressions for the GUI pipeline.

BUG-02: BMP/TIFF outputs must carry their own magic, not PNG bytes under a
foreign extension.
BUG-05: EXIF Orientation must be normalized exactly once; detection, removal and
output share one pixel truth and the written file must not double-rotate.
BUG-06: ordinary EXIF, Author/Title text, ICC and DPI must survive an AI strip.
"""

from __future__ import annotations

import sys
from pathlib import Path

import piexif
import pytest
from PIL import Image, ImageOps
from PIL.PngImagePlugin import PngInfo

# The GUI under test is a Tk application; CI Python ships no tkinter.
pytest.importorskip("tkinter")

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import gui_app  # noqa: E402

import remove_ai_watermarks.image_io as image_io  # noqa: E402
from remove_ai_watermarks.metadata import strip_and_verify  # noqa: E402

# ── Deterministic fixtures ──────────────────────────────────────────────────


def _exif_dict(*, orientation: int = 1) -> dict[str, dict[int, object]]:
    return {
        "0th": {
            piexif.ImageIFD.Make: b"Test Camera",
            piexif.ImageIFD.Model: b"TestCam X1",
            piexif.ImageIFD.Artist: b"Human Photographer",
            piexif.ImageIFD.Orientation: orientation,
        },
        "Exif": {piexif.ExifIFD.DateTimeOriginal: b"2026:08:12 10:00:00"},
    }


def _make_jpeg_with_metadata(
    path: Path,
    *,
    orientation: int = 1,
    with_ai: bool = True,
    icc: bytes | None = b"\x00" * 256,
    dpi: tuple[int, int] | None = (300, 300),
) -> Path:
    """JPEG carrying EXIF (Make/Model/Artist/DateTimeOriginal/Orientation),
    optional ICC profile, DPI, and an optional AI EXIF marker (Software tag)."""
    img = Image.new("RGB", (96, 64), (90, 140, 190))
    exif = _exif_dict(orientation=orientation)
    if with_ai:
        exif["0th"][piexif.ImageIFD.Software] = b"gpt-image-1 (AI generator)"
    kwargs: dict[str, object] = {"format": "JPEG", "quality": 95, "exif": piexif.dump(exif)}
    if icc is not None:
        kwargs["icc_profile"] = icc
    if dpi is not None:
        kwargs["dpi"] = dpi
    img.save(path, **kwargs)
    return path


def _make_png_with_metadata(path: Path, *, with_ai: bool = True) -> Path:
    img = Image.new("RGB", (64, 48), (10, 120, 200))
    info = PngInfo()
    info.add_text("Author", "Human Artist")
    info.add_text("Title", "Portrait")
    if with_ai:
        info.add_text("parameters", "Steps: 20, Sampler: Euler")
    img.save(path, format="PNG", dpi=(144, 144), icc_profile=b"\x00" * 64, pnginfo=info, exif=piexif.dump(_exif_dict()))
    return path


# ── BUG-02: same-format output magic ────────────────────────────────────────


@pytest.mark.parametrize("fmt", ["BMP", "TIFF"])
def test_strip_output_magic_matches_extension(tmp_path: Path, fmt: str) -> None:
    """BUG-02: stripping a BMP/TIFF with a matching output extension must produce
    BMP/TIFF bytes, never PNG bytes under a foreign name (the pre-fix code fell
    back to PNG whenever the extension was not in the PIL format table)."""
    src = tmp_path / f"src.{fmt.lower()}"
    Image.new("RGB", (32, 24), (5, 60, 120)).save(src, format=fmt)
    out = tmp_path / f"out.{fmt.lower()}"
    stripped, surviving = strip_and_verify(src, out)
    head = stripped.read_bytes()[:8]
    if fmt == "BMP":
        assert head[:2] == b"BM", f"BMP output lost its magic: {head!r}"
    else:
        assert head[:4] in (b"II*\x00", b"MM\x00*"), f"TIFF output lost its magic: {head!r}"
    assert not surviving


@pytest.mark.parametrize("fmt", ["BMP", "TIFF"])
def test_gui_pipeline_output_magic_matches_extension(tmp_path: Path, fmt: str) -> None:
    from queue import Queue

    src = tmp_path / f"src.{fmt.lower()}"
    Image.new("RGB", (32, 24), (5, 60, 120)).save(src, format=fmt)
    r = gui_app.run_pipeline(
        src,
        None,
        do_visible=True,
        do_report=True,
        do_strip=True,
        stop_event=__import__("threading").Event(),
        log_q=Queue(),
    )
    assert r.status == "success", (r.status, r.error, r.messages)
    assert r.output is not None and r.output.exists()
    head = r.output.read_bytes()[:8]
    if fmt == "BMP":
        assert head[:2] == b"BM"
    else:
        assert head[:4] in (b"II*\x00", b"MM\x00*")


# ── BUG-05: orientation normalization ───────────────────────────────────────


@pytest.mark.parametrize("orientation", [1, 3, 6, 8])
def test_read_normalized_applies_orientation_once(tmp_path: Path, orientation: int) -> None:
    src = _make_jpeg_with_metadata(tmp_path / "o.jpg", orientation=orientation, with_ai=False)
    decoded = image_io.read_normalized(src)
    assert decoded is not None
    assert decoded.orientation == orientation
    with Image.open(src) as raw:
        raw.load()
        display = ImageOps.exif_transpose(raw)
        assert display is not None
        expected_size = display.size
    assert (decoded.bgr.shape[1], decoded.bgr.shape[0]) == expected_size, (
        f"orientation {orientation}: pixels must match the display direction "
        f"{(decoded.bgr.shape[1], decoded.bgr.shape[0])} != {expected_size}"
    )
    assert decoded.display_swapped == (orientation in (5, 6, 7, 8))


def test_gui_visible_removal_uses_normalized_pixels(tmp_path: Path) -> None:
    """BUG-05/07: an orientation=6 image whose sparkle sits in the display-
    direction bottom-right corner must be detected AND removed on the SAME
    normalized pixels, with output no longer double-rotating."""
    import numpy as np

    from remove_ai_watermarks.gemini_engine import GeminiEngine, get_watermark_config

    engine = GeminiEngine()
    size = 512
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    base = 80 + 18 * np.sin(xx / 40.0) + 14 * np.cos(yy / 55.0)
    bgr = np.clip(np.stack([base, base * 0.97, base * 1.03], axis=-1), 0, 255).astype(np.uint8)
    config = get_watermark_config(size, size)
    x, y = config.get_position(size, size)
    alpha = engine.get_interpolated_alpha(config.logo_size)
    ah, aw = alpha.shape[:2]
    a = alpha[:, :, None]
    roi = bgr[y : y + ah, x : x + aw]
    bgr[y : y + ah, x : x + aw] = np.clip(a * 255.0 + (1.0 - a) * roi, 0, 255)

    # Physical 6x8 storage: the display image is the 8x6 (rotated 90deg) version.
    rotated = np.rot90(bgr, k=3)  # 90deg counterclockwise = display for orientation 6
    display_h, display_w = rotated.shape[:2]
    pil = Image.fromarray(rotated[:, :, ::-1])  # BGR -> RGB
    src = tmp_path / "o6.jpg"
    pil.save(src, format="JPEG", quality=95, exif=piexif.dump(_exif_dict(orientation=6)))

    from queue import Queue

    r = gui_app.run_pipeline(
        src,
        None,
        do_visible=True,
        do_report=True,
        do_strip=True,
        stop_event=__import__("threading").Event(),
        log_q=Queue(),
    )
    assert r.status == "success", (r.status, r.error, r.messages)
    assert any("Gemini" in m or "gemini" in m.lower() for m in r.messages)
    assert any("移除" in m for m in r.messages)

    # Output must not double-rotate: reopen it; its pixels must already be the
    # display direction AND its EXIF orientation must be 1/missing.
    with Image.open(r.output) as out:
        out.load()
        assert (out.height, out.width) == (display_h, display_w), "output not display-oriented"
        orientation_after = out.getexif().get(0x0112, 1)
        assert orientation_after in (1, None), f"orientation not cleared after physical rotation: {orientation_after}"


# ── BUG-06: protected metadata survives ─────────────────────────────────────


def test_jpeg_strip_preserves_exif_icc_dpi(tmp_path: Path) -> None:
    src = _make_jpeg_with_metadata(tmp_path / "a.jpg", orientation=1, with_ai=True)
    out, surviving = strip_and_verify(src, tmp_path / "b.jpg")
    assert not surviving, surviving
    with Image.open(out) as img:
        info = img.info
        exif = img.getexif()
        assert exif.get(piexif.ImageIFD.Make) in (b"Test Camera", "Test Camera")
        assert exif.get(piexif.ImageIFD.Model) in (b"TestCam X1", "TestCam X1")
        assert exif.get(piexif.ImageIFD.Artist) in (b"Human Photographer", "Human Photographer")
        exif_ifd = exif.get_ifd(0x8769)
        assert exif_ifd.get(piexif.ExifIFD.DateTimeOriginal) in (
            b"2026:08:12 10:00:00",
            "2026:08:12 10:00:00",
        )
        assert exif.get(piexif.ImageIFD.Orientation, 1) == 1
        assert exif.get(piexif.ImageIFD.Software) in (None, b"", "")
        assert "icc_profile" in info or "icc" in {k.lower() for k in info}
        dpi = info.get("dpi")
        assert dpi is not None and round(dpi[0]) == 300


def test_png_strip_preserves_exif_text_dpi(tmp_path: Path) -> None:
    src = _make_png_with_metadata(tmp_path / "a.png", with_ai=True)
    out, surviving = strip_and_verify(src, tmp_path / "b.png")
    assert not surviving, surviving
    with Image.open(out) as img:
        info = img.info
        assert info.get("Author") == "Human Artist"
        assert info.get("Title") == "Portrait"
        assert "parameters" not in info  # AI chunk gone
        assert "icc_profile" in info or "icc" in {k.lower() for k in info}
        dpi = info.get("dpi")
        assert dpi is not None and round(dpi[0]) == 144
        exif = img.getexif()
        assert exif.get(piexif.ImageIFD.Make) in (b"Test Camera", "Test Camera")
        assert exif.get(piexif.ImageIFD.Artist) in (b"Human Photographer", "Human Photographer")


def test_metadata_only_path_keeps_original_orientation(tmp_path: Path) -> None:
    """BUG-05: stripping AI metadata WITHOUT a pixel stage must NOT rotate the
    image or rewrite the (legal) orientation value."""
    src = _make_jpeg_with_metadata(tmp_path / "o6.jpg", orientation=6, with_ai=True)
    out, surviving = strip_and_verify(src, tmp_path / "o6_clean.jpg")
    assert not surviving, surviving
    with Image.open(out) as img:
        img.load()
        assert img.getexif().get(piexif.ImageIFD.Orientation, 1) == 6
        assert (img.width, img.height) == (96, 64)
