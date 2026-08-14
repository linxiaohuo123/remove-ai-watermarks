# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller ONE-FILE spec for the CPU-only GUI build of remove-ai-watermarks.

Build (locked environment):
  uv run --frozen --extra gui pyinstaller --clean --noconfirm --distpath release-onefile --workpath build-tmp-onefile scripts/build_gui_onefile.spec

Produces <distpath>/印消.exe — a SINGLE self-contained executable.

NOTES on onefile specifics:
- ONE FILE: python runtime + all deps embedded. User double-clicks the exe; no
  _internal directory is shipped. Each run unpacks to %TEMP%/_MEIxxxx, so first
  start is slower and SmartScreen/AV scrutiny is higher than onedir. This is the
  deliberate trade-off for a portable single exe.
- Native libs (c2pa_c.dll, OpenCV DLLs) are embedded and extracted at runtime.
- The spec mirrors build_gui.spec (version resource, release files). Keep the
  two in sync.

Adds c2pa_c.dll explicitly: the onedir build does NOT bundle it (only dist-info
is collected), which makes C2PA detection silently unavailable in the exe. The
onefile build must include the native library via `binaries`.
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
# from THIS tree. Keep the installed package at the same version as this checkout.

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

# ── c2pa native library (fixes missing C2PA in the onedir build) ────────────
import c2pa  # noqa: E402

_c2pa_libs = Path(os.path.dirname(c2pa.__file__)) / "libs"
_c2pa_c_dll = _c2pa_libs / "c2pa_c.dll"
BINARIES = [(str(_c2pa_c_dll), "c2pa/libs")] if _c2pa_c_dll.is_file() else []

# ── Heavy or system-coupled stacks NOT in this CPU image build ──────────────
EXCLUDES = [
    "av", "torch", "torchvision", "diffusers", "transformers", "tokenizers",
    "accelerate", "safetensors", "onnxruntime", "onnxruntime_capi", "trustmark",
    "huggingface_hub", "diffsynth",
]

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
    a.binaries,          # onefile: embed binaries inside the exe
    a.datas,             # onefile: embed data (incl. assets) inside the exe
    [],
    name="印消",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,           # deterministic; upx raises AV false positives
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version=VERSION_INFO,
    icon=str(SPEC_DIR / "gui_icon.ico"),
)

# No COLLECT — onefile emits only the single exe.

# ── Release files (PRD 5.9/5.10) written next to the exe ────────────────────
def _dist_infos(release_dir: Path) -> list[Path]:
    return sorted((release_dir / "_internal").glob("*.dist-info")) if (release_dir / "_internal").is_dir() else []


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# Onefile has no _internal; DEPENDENCIES/NOTICES are built from the venv's
# installed metadata (same set the onedir build lists).
def _collect_metadata_files() -> list[Path]:
    import importlib.metadata

    names = {
        "remove-ai-watermarks", "numpy", "opencv-python-headless", "pillow",
        "pywavelets", "piexif", "click", "python-dotenv", "tkinterdnd2",
        "setuptools", "c2pa-python",
    }
    out: list[Path] = []
    for dist in importlib.metadata.distributions():
        nm = (dist.metadata.get("Name") or "").casefold()
        if nm not in names:
            continue
        di = Path(dist.locate_file(f"{nm.replace('-', '_')}-{dist.version}.dist-info"))
        if di.is_dir():
            out.append(di)
    return out


def _dist_license(text: str) -> str:
    lic_m = re.search(r"^License: (.+)$", text, re.MULTILINE)
    if lic_m and lic_m.group(1).strip():
        return lic_m.group(1).strip()
    cls_m = re.search(r"^Classifier: License :: OSI Approved :: (.+)$", text, re.MULTILINE)
    if cls_m:
        return cls_m.group(1).strip()
    return "?"


def _write_release_artifacts(dist: str) -> None:
    # Onefile EXE lands directly at <distpath>/印消.exe (PyInstaller puts a
    # onefile EXE in DISTPATH root, unlike onedir's <distpath>/<name>/), so the
    # release artifacts are written beside it in the same directory.
    # Use an ABSOLUTE path: the spec runs with CWD inside DISTPATH, so a
    # relative dist would resolve to <dist>/<app-name>/ instead of the root
    # where the exe lands.
    release_dir = Path(dist).resolve()
    release_dir.mkdir(parents=True, exist_ok=True)
    exe_path = release_dir / "印消.exe"
    (release_dir / "LICENSE").write_bytes((ROOT / "LICENSE").read_bytes())
    lines = [
        f"# remove-ai-watermarks {_VERSION} GUI build (ONE-FILE)",
        "# name|version|license — resolved from installed metadata",
        f"project|remove-ai-watermarks|{_VERSION}|Apache-2.0",
    ]
    for di in _collect_metadata_files():
        meta = di / "METADATA"
        if not meta.is_file():
            continue
        text = meta.read_text(encoding="utf-8", errors="replace")
        nm_m = re.search(r"^Name: (.+)$", text, re.MULTILINE)
        ver_m = re.search(r"^Version: (.+)$", text, re.MULTILINE)
        lines.append(
            "|".join(
                [
                    (nm_m.group(1) if nm_m else di.name),
                    (ver_m.group(1) if ver_m else "?"),
                    _dist_license(text),
                ]
            )
        )
    (release_dir / "DEPENDENCIES.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    notices = [
        "THIRD-PARTY NOTICES",
        "",
        f"remove-ai-watermarks {_VERSION} bundles the following third-party components",
        "(license texts follow). See DEPENDENCIES.txt for the exact versions.",
        "",
    ]
    for di in _collect_metadata_files():
        meta = di / "METADATA"
        if not meta.is_file():
            continue
        text = meta.read_text(encoding="utf-8", errors="replace")
        nm_m = re.search(r"^Name: (.+)$", text, re.MULTILINE)
        ver_m = re.search(r"^Version: (.+)$", text, re.MULTILINE)
        pkg = (nm_m.group(1) if nm_m else di.name) + (" " + ver_m.group(1) if ver_m else "")
        lic = _dist_license(text)
        notices += ["=" * 72, f"{pkg} — License: {lic}", "=" * 72, ""]
        lic_files = sorted(di.glob("licenses/*"))
        if lic_files:
            for lf in lic_files:
                if lf.is_file():
                    notices += [f"--- {pkg}: {lf.name} ---", lf.read_text(encoding="utf-8", errors="replace").strip(), ""]
        else:
            notices += ["No license file shipped inside the wheel metadata;", f"declared License field: {lic}", ""]
    (release_dir / "THIRD_PARTY_NOTICES.txt").write_text("\n".join(notices) + "\n", encoding="utf-8")

    sum_lines: list[str] = []
    if exe_path.is_file():
        sum_lines.append(f"{_sha256(exe_path)}  {exe_path.name}")
    for artifact in ("LICENSE", "DEPENDENCIES.txt", "THIRD_PARTY_NOTICES.txt"):
        p = release_dir / artifact
        if p.is_file():
            sum_lines.append(f"{_sha256(p)}  {artifact}")
    (release_dir / "SHA256SUMS.txt").write_text("\n".join(sum_lines) + "\n", encoding="utf-8")
    print(f"[build_gui_onefile.spec] release artifacts written for v{_VERSION} in {release_dir}")


# DISTPATH is injected by PyInstaller and points at the dist root; resolve it
# absolutely (the spec's CWD is DISTPATH itself). This runs AFTER the build
# pipeline in the same spec process, so the exe already exists on disk.
_write_release_artifacts(str(Path(DISTPATH).resolve()))
