"""C2PA inspection through the official reader with a bounded PNG fallback."""

from __future__ import annotations

import contextlib
import functools
import json
import logging
import re
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from remove_ai_watermarks._internal.constants import (
    C2PA_ACTIONS,
    C2PA_AI_TOOLS,
    C2PA_AI_VENDORS,
    C2PA_CHUNK_TYPE,
    C2PA_ISSUERS,
    C2PA_SIGNATURES,
    C2PA_SOFT_BINDINGS,
    PNG_SIGNATURE,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from typing import BinaryIO

_C2paReader: Any = None
_C2paError: Any = None
with contextlib.suppress(Exception):
    from c2pa import C2paError as _C2paError  # pyright: ignore[reportMissingTypeStubs]
    from c2pa import Reader as _C2paReader  # pyright: ignore[reportMissingTypeStubs]

_C2PA_READER_AVAILABLE = _C2paReader is not None
_PNG_HEADER = struct.Struct(">I4s")


@dataclass(frozen=True)
class _PngChunk:
    payload: bytes
    serialized: bytes


def reader_available() -> bool:
    """Return whether the official C2PA reader loaded successfully."""
    return _C2PA_READER_AVAILABLE


def _manifest_json_uncached(path: str) -> str | None:
    """The manifest store as JSON, or None when this file has no readable manifest.

    Two outcomes are routine and stay at debug: a file with no manifest (``try_create``
    returns None) and a container the reader does not support. ANY other failure is
    logged at warning, because the caller cannot tell the difference from the return
    value and the consequence is severe: the verdict silently falls back to the raw
    byte scan and can lose a high-confidence signal. The log line preserves the
    diagnostic context needed to investigate an intermittent reader failure.
    """
    try:
        reader = _C2paReader.try_create(path)
    except _C2paError.NotSupported as error:
        logger.debug("C2PA reader does not support %s: %s", path, error)
        return None
    except Exception as error:
        logger.warning("C2PA reader failed to open %s: %s: %s", path, type(error).__name__, error)
        return None
    if reader is None:
        return None
    try:
        with reader:
            return cast("str", reader.json())
    except Exception as error:
        # The reader opened the file, so a manifest is there; failing to serialize it
        # is never routine.
        logger.warning("C2PA reader could not serialize %s: %s: %s", path, type(error).__name__, error)
        return None


@functools.lru_cache(maxsize=8)
def _manifest_json_cached(path: str, _mtime_ns: int) -> str | None:
    return _manifest_json_uncached(path)


def read_manifest_store_json(image_path: Path) -> str | None:
    """Read the complete manifest-store JSON, caching it until the file changes."""
    if not reader_available():
        return None
    path = str(image_path)
    try:
        return _manifest_json_cached(path, image_path.stat().st_mtime_ns)
    except OSError:
        return _manifest_json_uncached(path)


def _find_c2pa_chunk(path: Path) -> _PngChunk | None:
    """Return the first recognizable C2PA chunk without loading the whole PNG."""
    try:
        stream = path.open("rb")
    except OSError:
        return None
    with stream:
        if stream.read(len(PNG_SIGNATURE)) != PNG_SIGNATURE:
            return None
        file_size = stream.seek(0, 2)
        stream.seek(len(PNG_SIGNATURE))
        while True:
            header = stream.read(_PNG_HEADER.size)
            if len(header) != _PNG_HEADER.size:
                return None
            length, kind = _PNG_HEADER.unpack(header)
            if length + 4 > file_size - stream.tell():
                return None
            if kind == C2PA_CHUNK_TYPE:
                payload = stream.read(length)
                crc = stream.read(4)
                if _looks_like_c2pa(payload):
                    return _PngChunk(payload, header + payload + crc)
            else:
                stream.seek(length + 4, 1)
            if kind == b"IEND":
                return None


def _is_well_formed_png(path: Path) -> bool:
    """Validate PNG chunk bounds with seeks rather than payload allocations."""
    try:
        stream = path.open("rb")
    except OSError:
        return False
    with stream:
        if stream.read(len(PNG_SIGNATURE)) != PNG_SIGNATURE:
            return False
        file_size = stream.seek(0, 2)
        stream.seek(len(PNG_SIGNATURE))
        while True:
            header = stream.read(_PNG_HEADER.size)
            if len(header) != _PNG_HEADER.size:
                return False
            length, kind = _PNG_HEADER.unpack(header)
            if length + 4 > file_size - stream.tell():
                return False
            stream.seek(length + 4, 1)
            if kind == b"IEND":
                return True


def _copy_bytes(source: BinaryIO, target: BinaryIO, byte_count: int) -> None:
    """Copy exactly one bounded chunk without allocating its complete payload."""
    remaining = byte_count
    while remaining:
        block = source.read(min(remaining, 1024 * 1024))
        if not block:
            raise OSError("PNG changed while it was being copied")
        target.write(block)
        remaining -= len(block)


def _looks_like_c2pa(payload: bytes) -> bool:
    lowered = payload.lower()
    return any(signature in payload for signature in C2PA_SIGNATURES) or b"c2pa" in lowered or b"jumb" in lowered


def extract_c2pa_chunk(image_path: Path) -> bytes | None:
    """Return the first complete C2PA PNG chunk, including header and CRC."""
    if image_path.suffix.casefold() != ".png":
        return None
    chunk = _find_c2pa_chunk(image_path)
    return None if chunk is None else chunk.serialized


def has_c2pa_metadata(image_path: Path) -> bool:
    """Return whether a validly bounded PNG contains a recognizable C2PA chunk."""
    return extract_c2pa_chunk(Path(image_path)) is not None


def _active_manifest(store: dict[str, Any]) -> dict[str, Any]:
    manifests = store.get("manifests")
    if not isinstance(manifests, dict):
        return {}
    typed_manifests = cast("dict[object, object]", manifests)
    active = typed_manifests.get(store.get("active_manifest"))
    return cast("dict[str, Any]", active) if isinstance(active, dict) else {}


def _claim_generator_from_store(store: dict[str, Any]) -> str | None:
    active = _active_manifest(store)
    direct = active.get("claim_generator")
    if isinstance(direct, str) and direct.isprintable() and direct:
        return direct
    candidates = active.get("claim_generator_info")
    if isinstance(candidates, list) and candidates and isinstance(candidates[0], dict):
        candidate = cast("dict[object, object]", candidates[0])
        name = candidate.get("name")
        if isinstance(name, str) and name.isprintable() and name:
            return name
    return None


def synthid_verdict(vendors: str) -> str:
    """Describe why supported provenance establishes a SynthID watermark."""
    return f"present according to {vendors} provenance"


def _names_present(buffer: bytes, registry: dict[bytes, str]) -> list[str]:
    return sorted({label for token, label in registry.items() if token in buffer})


def synthid_evidence_vendors_in(buffer: bytes, *, has_watermark_action: bool | None = None) -> list[str]:
    """List issuers whose provenance establishes SynthID for this asset.

    Google applies SynthID to all media generated by its tools, so its AI C2PA
    provenance is sufficient. OpenAI C2PA predates OpenAI's SynthID rollout;
    current manifests distinguish the watermarked generation with the explicit
    ``c2pa.watermarked.*`` action. A legacy OpenAI issuer token alone therefore
    remains provenance evidence, but not SynthID evidence.
    """
    if has_watermark_action is None:
        has_watermark_action = b"c2pa.watermarked" in buffer
    return sorted(
        {
            vendor.org
            for vendor in C2PA_AI_VENDORS
            if vendor.synthid
            and vendor.issuer in buffer
            and (has_watermark_action or not vendor.synthid_requires_watermark_action)
        }
    )


def soft_binding_vendors_in(buffer: bytes) -> list[str]:
    """List the soft-binding algorithms named in manifest bytes."""
    return _names_present(buffer, C2PA_SOFT_BINDINGS)


def _ordered_matches(buffer: bytes, registry: dict[bytes, str]) -> list[str]:
    return list(dict.fromkeys(label for token, label in registry.items() if token in buffer))


def _populate_registry_fields(buffer: bytes, info: dict[str, Any]) -> bool:
    issuers = _ordered_matches(buffer, C2PA_ISSUERS)
    tools = _ordered_matches(buffer, C2PA_AI_TOOLS)
    actions = _ordered_matches(buffer, C2PA_ACTIONS)
    if issuers:
        info["issuer"] = ", ".join(issuers)
    if tools:
        info["ai_tool"] = ", ".join(tools)
    if actions:
        info["actions"] = ", ".join(actions)

    ai_source = False
    if b"trainedAlgorithmicMedia" in buffer:
        info.update(source_type="trainedAlgorithmicMedia (AI-generated)", ai_source_kind="generated")
        ai_source = True
    elif b"compositeWithTrainedAlgorithmicMedia" in buffer:
        info.update(source_type="compositeWithTrainedAlgorithmicMedia (AI-enhanced)", ai_source_kind="enhanced")
        ai_source = True
    elif b"algorithmicMedia" in buffer:
        info["source_type"] = "algorithmicMedia"

    if b"c2pa.watermarked" in buffer:
        info["watermarked"] = True
    synthid = synthid_evidence_vendors_in(buffer, has_watermark_action=info.get("watermarked", False))
    if ai_source and synthid:
        info["synthid_vendors"] = synthid
        info["synthid_watermark"] = synthid_verdict(", ".join(synthid))

    soft_bindings = soft_binding_vendors_in(buffer)
    if soft_bindings:
        info["soft_binding_vendors"] = soft_bindings
        info["soft_binding"] = ", ".join(soft_bindings)
    return ai_source


def _base_info(byte_count: int, *, fallback: bool = False) -> dict[str, Any]:
    container = "C2PA manifest" if fallback else "C2PA manifest store"
    return {
        "has_c2pa": True,
        "type": "C2PA (Coalition for Content Provenance and Authenticity)",
        "c2pa_manifest": f"{container} ({byte_count} bytes)",
    }


def _info_from_store(store: dict[str, Any], encoded: bytes) -> dict[str, Any]:
    info = _base_info(len(encoded))
    _populate_registry_fields(encoded, info)
    generator = _claim_generator_from_store(store)
    if generator is not None:
        info["claim_generator"] = generator
    signature_value = _active_manifest(store).get("signature_info")
    if isinstance(signature_value, dict):
        signature = cast("dict[object, object]", signature_value)
        timestamp = signature.get("time")
        if timestamp:
            info["timestamp"] = str(timestamp)
    return info


def c2pa_info_from_manifest_store(store: str | dict[str, Any]) -> dict[str, Any]:
    """Normalize a manifest store supplied as JSON text or a decoded object."""
    try:
        raw_decoded: object = store if isinstance(store, dict) else json.loads(store)
        decoded = cast("dict[str, Any]", raw_decoded) if isinstance(raw_decoded, dict) else None
        if not isinstance(decoded, dict) or not decoded or decoded.get("error"):
            return {}
        encoded = json.dumps(decoded, ensure_ascii=False).encode() if isinstance(store, dict) else store.encode()
    except (TypeError, ValueError):
        return {}
    return _info_from_store(decoded, encoded)


def cbor_text_after(payload: bytes, key: bytes) -> str | None:
    """Decode a definite-length CBOR text value immediately following ``key``."""
    key_end = payload.find(key)
    if key_end < 0:
        return None
    cursor = key_end + len(key)
    if cursor >= len(payload):
        return None
    initial = payload[cursor]
    if 0x60 <= initial <= 0x77:
        length, cursor = initial & 0x1F, cursor + 1
    elif initial == 0x78 and cursor + 1 < len(payload):
        length, cursor = payload[cursor + 1], cursor + 2
    elif initial == 0x79 and cursor + 2 < len(payload):
        length = int.from_bytes(payload[cursor + 1 : cursor + 3], "big")
        cursor += 3
    else:
        return None
    raw = payload[cursor : cursor + length]
    if len(raw) != length:
        return None
    try:
        return raw.decode()
    except UnicodeDecodeError:
        return raw.decode("latin1", errors="replace")


def _parse_c2pa_chunk(payload: bytes, info: dict[str, Any]) -> None:
    info.update(_base_info(len(payload), fallback=True))
    _populate_registry_fields(payload, info)
    for key, output_key in ((b"name", "claim_generator"), (b"specVersion", "c2pa_spec")):
        value = cbor_text_after(payload, key)
        if value and value.isprintable():
            info[output_key] = value
    timestamps = [item.decode() for item in re.findall(rb"\d{14}Z", payload)]
    if timestamps:
        info["timestamp"] = timestamps[0]
        if len(timestamps) > 1:
            info["timestamps"] = timestamps[:3]


def _extract_c2pa_info_png(image_path: Path) -> dict[str, Any]:
    if image_path.suffix.casefold() != ".png":
        return {}
    chunk = _find_c2pa_chunk(image_path)
    if chunk is None:
        return {}
    info: dict[str, Any] = {}
    _parse_c2pa_chunk(chunk.payload, info)
    return info


def _extract_c2pa_info_impl(image_path: Path) -> dict[str, Any]:
    store = read_manifest_store_json(Path(image_path))
    if store is not None:
        return c2pa_info_from_manifest_store(store)
    return _extract_c2pa_info_png(Path(image_path))


@functools.lru_cache(maxsize=4)
def _extract_c2pa_info_cached(path_str: str, _mtime_ns: int, _size: int, _reader: bool) -> dict[str, Any]:
    """Cache shim: every argument after the path is key-only.

    ``_reader`` is in the key because the answer genuinely depends on it -- with the
    official reader the manifest comes back as a store, without it from the hand-rolled
    PNG chunk parser, and the two produce different ``c2pa_manifest`` labels. Keying on
    the file alone handed a reader-path result to a caller that had disabled the reader.
    """
    return _extract_c2pa_info_impl(Path(path_str))


def extract_c2pa_info(image_path: Path) -> dict[str, Any]:
    """Return normalized C2PA evidence from the official reader or PNG fallback.

    Memoized on ``(path, mtime_ns, size)``: one ``identify`` reaches this twice (once
    directly, once inside ``get_ai_metadata``) and each call re-runs the Rust manifest
    reader and re-parses its JSON. Size joins mtime in the key because this package
    rewrites files in place, and an in-place rewrite can land inside one mtime tick.
    """
    try:
        stat = image_path.stat()
    except OSError:
        # No stat (a pipe, or a race): read uncached rather than fail.
        return _extract_c2pa_info_impl(image_path)
    cached = _extract_c2pa_info_cached(str(image_path), stat.st_mtime_ns, stat.st_size, _C2PA_READER_AVAILABLE)
    # Deep-ish copy: values are scalars plus a few lists, and a caller mutating one of
    # those lists would otherwise poison every later reader of the same file.
    return {key: list(cast("list[Any]", value)) if isinstance(value, list) else value for key, value in cached.items()}


def inject_c2pa_chunk(target_path: Path, output_path: Path, c2pa_chunk: bytes) -> None:
    """Replace any C2PA chunks in a PNG and insert ``c2pa_chunk`` before IDAT."""
    if target_path.suffix.casefold() != ".png" or output_path.suffix.casefold() != ".png":
        raise ValueError("C2PA chunk injection is only supported for PNG files")
    if not _is_well_formed_png(target_path):
        raise ValueError("Target is not a well-formed PNG file")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with target_path.open("rb") as source, output_path.open("wb") as target:
        target.write(source.read(len(PNG_SIGNATURE)))
        inserted = False
        while True:
            header = source.read(_PNG_HEADER.size)
            length, kind = _PNG_HEADER.unpack(header)
            if kind == b"IDAT" and not inserted:
                target.write(c2pa_chunk)
                inserted = True
            if kind == C2PA_CHUNK_TYPE:
                source.seek(length + 4, 1)
            else:
                target.write(header)
                _copy_bytes(source, target, length + 4)
            if kind == b"IEND":
                break
