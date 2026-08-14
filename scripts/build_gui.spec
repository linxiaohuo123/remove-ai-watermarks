# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the CPU-only GUI build of remove-ai-watermarks.

Build (locked environment, PRD BUG-11):
  uv run --frozen --extra gui pyinstaller --clean --noconfirm --distpath release-new --workpath build-tmp scripts/build_gui.spec

Produces <distpath>/印消/ (onedir) with:
  印消.exe                 (x64, console-hidden, Windows version resource)
  _internal/
  LICENSE                  (project Apache-2.0)
  THIRD_PARTY_NOTICES.txt  (third-party components actually bundled)
  DEPENDENCIES.txt         (name|version|license per bundled dist-info)
  SHA256SUMS.txt           (SHA-256 of the exe; written after the build)

Version resource fields are derived from pyproject.toml — no second hand-maintained
version. Excludes the GPU/video/ONNX stack so the bundle stays lean: av, torch,
diffusers, transformers, onnxruntime, trustmark, huggingface_hub... Runtime
guards (optional_deps) turn the missing extras into clear messages instead of
crashes.
"""

import hashlib
import os
import re
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

# ── Paths ───────────────────────────────────────────────────────────────────
SPEC_DIR = Path(SPECPATH)
ROOT = SPEC_DIR.parent
SCRIPT = str(SPEC_DIR / "gui_app.py")
SRC = str(ROOT / "src")
# `pathex` puts the checkout's src/ AHEAD of site-packages, so the bundle is built
# from THIS tree. Keep the installed package at the same version as this checkout
# (or the resolved imports will silently come from the newer src/ while hidden
# imports resolve from the older wheel).

# ── Version (single source: pyproject.toml) ─────────────────────────────────
_PROJECT_META = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
_VERSION = re.search(r'^version = "([^"]+)"', _PROJECT_META, re.MULTILINE).group(1)
_VERSION_PARTS = tuple(int(p) for p in _VERSION.split("."))
_FILE_VERS = (_VERSION_PARTS + (0, 0, 0, 0))[:4]

try:
    from PyInstaller.utils.win32.versioninfo import (
        FixedFileInfo,
        StringFileInfo,
        StringStruct,
        StringTable,
        VarFileInfo,
        VarStruct,
        VSVersionInfo,
    )

    VERSION_INFO = VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=_FILE_VERS,
            prodvers=_FILE_VERS,
            mask=0x3F,
            flags=0x0,
            OS=0x40004,
            fileType=0x1,
            subtype=0x0,
            date=(0, 0),
        ),
        kids=[
            StringFileInfo(
                [
                    StringTable(
                        "040904B0",
                        [
                            StringStruct("CompanyName", "林小伙"),
                            StringStruct("ProductName", "印消"),
                            StringStruct(
                                "FileDescription",
                                "印消 — AI 去水印 GUI 工具 (CPU)",
                            ),
                            StringStruct("FileVersion", _VERSION),
                            StringStruct("ProductVersion", _VERSION),
                            StringStruct(
                                "OriginalFilename", "印消.exe"
                            ),
                            StringStruct(
                                "LegalCopyright",
                                "Copyright (c) 2026 remove-ai-watermarks contributors. Apache-2.0. Windows 分发 by 林小伙",
                            ),
                        ],
                    )
                ]
            ),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )
except ImportError:  # non-Windows build host: version resource unavailable
    VERSION_INFO = None

# ── Dynamic imports that static analysis cannot see ─────────────────────────
#  - watermark_registry._ENGINE_CLASS: 11 visible-mark engines via import_module
#  - _text_mark_engine._RIVAL_MODULES: the same engine modules as rivals
#  - identify's DWT-DCT open-watermark path
HIDDEN_IMPORTS = [
    "remove_ai_watermarks.gemini_engine",
    "remove_ai_watermarks.doubao_engine",
    "remove_ai_watermarks.jimeng_engine",
    "remove_ai_watermarks.qwen_engine",
    "remove_ai_watermarks.kling_engine",
    "remove_ai_watermarks.yuanbao_engine",
    "remove_ai_watermarks.samsung_engine",
    "remove_ai_watermarks.pill_engine",
    "remove_ai_watermarks.runninghub_engine",
    "remove_ai_watermarks.baidu_engine",
    "remove_ai_watermarks.liblib_engine",
    "remove_ai_watermarks.dwt_dct",
    "remove_ai_watermarks.optional_deps",
]

# assets/*.png watermark templates + licenses/ are read via __file__ at runtime.
DATAS = collect_data_files("remove_ai_watermarks")

# dist-info of every runtime dependency that must be listed in DEPENDENCIES.txt /
# THIRD_PARTY_NOTICES.txt. PyInstaller only auto-collects a couple of them, so
# the dist-info directories are added explicitly (PRD 5.10.4 requires the
# release inventory to cover EVERY bundled component).
_RELEASE_METADATA_PACKAGES = frozenset(
    {
        "remove-ai-watermarks",
        "numpy",
        "opencv-python-headless",
        "pillow",
        "pywavelets",
        "piexif",
        "click",
        "python-dotenv",
        "tkinterdnd2",
        "setuptools",
        "c2pa-python",
    }
)


def _collect_dist_info(names: frozenset[str]) -> list[tuple[str, str]]:
    import importlib.metadata

    entries: list[tuple[str, str]] = []
    for dist in importlib.metadata.distributions():
        nm = (dist.metadata.get("Name") or "").casefold()
        if nm not in names:
            continue
        canonical = nm.replace("-", "_")
        prefix = f"{canonical}-{dist.version}.dist-info/"
        for f in dist.files or ():
            p = str(f)
            if p.startswith(prefix):
                abs_path = Path(dist.locate_file(f))
                if abs_path.is_file():
                    entries.append((str(abs_path), p))
    return entries


def _collect_dist_info(names: frozenset[str], skip_targets: set[str]) -> list[tuple[str, str]]:
    """Add each package's whole ``<canonical>-<version>.dist-info/`` directory as a
    data TOC entry, so COLLECT recurses it verbatim (METADATA + licenses/ + RECORD)."""
    import importlib.metadata

    entries: list[tuple[str, str]] = []
    for dist in importlib.metadata.distributions():
        nm = (dist.metadata.get("Name") or "").casefold()
        if nm not in names:
            continue
        if nm == "c2pa-python":
            # PyInstaller's own hooks already bundle this dist-info; re-adding it
            # collides during COLLECT. It still appears in DEPENDENCIES.txt via
            # the _internal scan.
            continue
        canonical = nm.replace("-", "_")
        di_name = f"{canonical}-{dist.version}.dist-info"
        if di_name in skip_targets:
            continue
        di_path = Path(dist.locate_file(di_name))
        if di_path.is_dir():
            entries.append((str(di_path), di_name))
    return entries


DATAS += _collect_dist_info(
    _RELEASE_METADATA_PACKAGES, skip_targets={target for _, target in DATAS}
)

# ── c2pa native library (mirrors build_gui_onefile.spec) ────────────────────
# Without it the bundle ships only c2pa's dist-info and C2PA detection silently
# goes missing in the exe.
import c2pa  # noqa: E402

_c2pa_libs = Path(os.path.dirname(c2pa.__file__)) / "libs"
_c2pa_c_dll = _c2pa_libs / "c2pa_c.dll"
BINARIES = [(str(_c2pa_c_dll), "c2pa/libs")] if _c2pa_c_dll.is_file() else []

# Heavy or system-coupled stacks that are NOT part of this CPU image build.
EXCLUDES = [
    "av",
    "torch",
    "torchvision",
    "diffusers",
    "transformers",
    "tokenizers",
    "accelerate",
    "safetensors",
    "onnxruntime",
    "onnxruntime_capi",
    "trustmark",
    "huggingface_hub",
    "diffsynth",
]

# dist-info of every runtime dependency that must be listed in DEPENDENCIES.txt /
# THIRD_PARTY_NOTICES.txt (added to DATAS above; this list keeps the two in sync).
# COPY_METADATA = [...]  # (unused: PyInstaller's option did not emit the files)

a = Analysis(
    [SCRIPT],
    pathex=[SRC],
    binaries=BINARIES,
    datas=DATAS,
    hiddenimports=HIDDEN_IMPORTS,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=EXCLUDES,
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="印消",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=VERSION_INFO,
    icon=str(SPEC_DIR / "gui_icon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="印消",
)

# ── Release files (PRD 5.9/5.10): LICENSE, DEPENDENCIES, NOTICES, SHA256SUMS ─
# Runs after COLLECT so _internal/*.dist-info exists and reflects what the EXE
# actually bundled.


def _dist_infos(release_dir: Path) -> list[Path]:
    internal = release_dir / "_internal"
    if not internal.is_dir():
        return []
    return sorted(internal.glob("*.dist-info"))


def _dist_license(text: str) -> str:
    lic_m = re.search(r"^License: (.+)$", text, re.MULTILINE)
    if lic_m and lic_m.group(1).strip():
        return lic_m.group(1).strip()
    cls_m = re.search(r"^Classifier: License :: OSI Approved :: (.+)$", text, re.MULTILINE)
    if cls_m:
        return cls_m.group(1).strip()
    return "?"


def _write_release_artifacts(dist, name) -> None:
    release_dir = Path(dist) / name
    exe_path = release_dir / f"{name}.exe"

    # 1) Project LICENSE (Apache-2.0) — single copy, never a stale duplicate.
    (release_dir / "LICENSE").write_bytes((ROOT / "LICENSE").read_bytes())

    # 2) DEPENDENCIES.txt: one line per bundled dist-info, license from metadata.
    lines = [
        f"# remove-ai-watermarks {_VERSION} GUI build",
        "# name|version|license|license-files — resolved from the bundled _internal/*.dist-info",
        "# python=3.12.x target=win_amd64 build=<timestamp>",
        f"# built={os.environ.get('SOURCE_DATE_EPOCH', '')}",
        "project|remove-ai-watermarks|" + _VERSION + "|Apache-2.0|LICENSE",
    ]
    for dist_info in _dist_infos(release_dir):
        meta = dist_info / "METADATA"
        if not meta.is_file():
            continue
        text = meta.read_text(encoding="utf-8", errors="replace")
        name_m = re.search(r"^Name: (.+)$", text, re.MULTILINE)
        ver_m = re.search(r"^Version: (.+)$", text, re.MULTILINE)
        license_files = ", ".join(
            p.name for p in sorted(dist_info.glob("licenses/*")) if p.is_file()
        )
        lines.append(
            "|".join(
                [
                    (name_m.group(1) if name_m else dist_info.name),
                    (ver_m.group(1) if ver_m else "?"),
                    _dist_license(text),
                    license_files,
                ]
            )
        )
    (release_dir / "DEPENDENCIES.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    # 3) THIRD_PARTY_NOTICES.txt: per-component license text embedded.
    notices = [
        "THIRD-PARTY NOTICES",
        "",
        f"remove-ai-watermarks {_VERSION} bundles the following third-party",
        "components (license texts follow). See DEPENDENCIES.txt for the exact",
        "versions bundled in this build.",
        "",
    ]
    for dist_info in _dist_infos(release_dir):
        meta = dist_info / "METADATA"
        if not meta.is_file():
            continue
        text = meta.read_text(encoding="utf-8", errors="replace")
        name_m = re.search(r"^Name: (.+)$", text, re.MULTILINE)
        ver_m = re.search(r"^Version: (.+)$", text, re.MULTILINE)
        pkg = (name_m.group(1) if name_m else dist_info.name) + (
            " " + ver_m.group(1) if ver_m else ""
        )
        lic = _dist_license(text)
        notices += ["=" * 72, f"{pkg} — License: {lic}", "=" * 72, ""]
        license_files = sorted(dist_info.glob("licenses/*"))
        if license_files:
            for lf in license_files:
                if lf.is_file():
                    notices.append(f"--- {pkg}: {lf.name} ---")
                    notices.append(
                        lf.read_text(encoding="utf-8", errors="replace").strip()
                    )
                    notices.append("")
        else:
            notices += [
                "No license file shipped inside the wheel metadata; the",
                f"declared License field is: {lic}",
                "",
            ]
    (release_dir / "THIRD_PARTY_NOTICES.txt").write_text(
        "\n".join(notices) + "\n", encoding="utf-8"
    )

    # 4) SHA256SUMS.txt: EXE first (the only file whose hash the PRD gates on),
    # then the other release artifacts.
    def _sha256(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return h.hexdigest()

    sum_lines: list[str] = []
    if exe_path.is_file():
        sum_lines.append(f"{_sha256(exe_path)}  {exe_path.name}")
    for artifact in ("LICENSE", "DEPENDENCIES.txt", "THIRD_PARTY_NOTICES.txt"):
        p = release_dir / artifact
        if p.is_file():
            sum_lines.append(f"{_sha256(p)}  {artifact}")
    (release_dir / "SHA256SUMS.txt").write_text(
        "\n".join(sum_lines) + "\n", encoding="utf-8"
    )
    print(f"[build_gui.spec] release artifacts written for v{_VERSION} in {release_dir}")


_write_release_artifacts(DISTPATH, "印消")
