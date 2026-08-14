"""Published dependency boundaries."""

from __future__ import annotations

from importlib.metadata import metadata, requires

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


def _requirement_names(extra: str | None = None) -> set[str]:
    selected_extra = extra or ""
    parsed = (Requirement(value) for value in requires("remove-ai-watermarks") or [])
    return {
        canonicalize_name(requirement.name)
        for requirement in parsed
        if requirement.marker is None or requirement.marker.evaluate({"extra": selected_extra})
    }


def test_default_install_is_metadata_focused():
    default = _requirement_names()

    assert {
        "c2pa-python",
        "click",
        "piexif",
        "pillow",
        "python-dotenv",
    } <= default
    assert {
        "av",
        "invisible-watermark",
        "numpy",
        "opencv-python-headless",
        "pillow-heif",
        "torch",
        "trustmark",
    }.isdisjoint(default)


def test_pixels_extra_owns_shared_numeric_dependencies():
    assert {
        "numpy",
        "opencv-python-headless",
    } <= _requirement_names("pixels")


def test_video_extra_owns_timestamp_dependency():
    assert "av" in _requirement_names("video")


def test_file_format_and_detector_dependencies_are_independent():
    assert "pillow-heif" in _requirement_names("heif")
    assert "pywavelets" in _requirement_names("detect")


def test_extras_use_capability_names_without_legacy_aliases():
    extras = set(metadata("remove-ai-watermarks").get_all("Provides-Extra") or [])

    assert {"pixels", "heif", "visible", "video", "detect", "diffusion"} <= extras
    assert {"gpu", "remove", "detect-pywavelets"}.isdisjoint(extras)


def test_production_all_does_not_include_development_tools():
    assert {
        "pyright",
        "pytest",
        "pytest-cov",
        "pytest-xdist",
        "ruff",
    }.isdisjoint(_requirement_names("all"))
