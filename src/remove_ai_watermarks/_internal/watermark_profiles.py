"""Project-owned configuration for invisible-watermark regeneration profiles.

Two profiles remain, and both are CUDA-only: ``qwen-zimage`` (the default) and
``sdxl-zimage``. The older ``controlnet``, ``sdxl``, ``qwen`` and ``default`` profiles
were removed rather than kept as a CPU path, because none of them matched the two-stage
recipe's face preservation and keeping them implied a quality this library no longer
offers. Removing invisible watermarks therefore needs a CUDA device; the visible-mark
registry and every identify path still run anywhere.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

# SDXL base is no longer a profile of its own, but it is still the global stage of
# sdxl-zimage, so the checkpoint id stays. Named for what it is rather than
# ``DEFAULT_MODEL_ID``: there is no user-selectable model any more, so "default"
# implied an override that both profiles reject.
SDXL_MODEL_ID = "stabilityai/stable-diffusion-xl-base-1.0"
CONTROLNET_CANNY_MODEL = "xinsir/controlnet-canny-sdxl-1.0"

QWEN_ZIMAGE_PROFILE = "qwen-zimage"
SDXL_ZIMAGE_PROFILE = "sdxl-zimage"
DEFAULT_PROFILE = QWEN_ZIMAGE_PROFILE
PROFILE_CHOICES = (QWEN_ZIMAGE_PROFILE, SDXL_ZIMAGE_PROFILE)

# The modules a real removal run needs, and the extra that installs them. Both live
# here, in the only profile module that imports nothing heavy, because the CLI's
# availability gate and the remover's own precondition must agree: when they drifted,
# the CLI passed on a torch+diffusers environment and the run then died at the
# DiffSynth face stage, telling the user to install an extra that does not contain it.
REMOVAL_MODULES = ("torch", "diffusers", "diffsynth")
# Shell-quoted, because this string is printed as a command the user copy-pastes.
# Bare brackets are a glob in zsh (the macOS default shell): an unquoted
# ``pip install remove-ai-watermarks[qwen-zimage]`` dies with "no matches found"
# before pip ever runs -- another install hint that does not install.
INVISIBLE_EXTRA = "'remove-ai-watermarks[qwen-zimage]'"

# qwen-zimage's output already matches the input's detail level, so polishing it is a
# no-op at best. sdxl-zimage's global pass leaves the softer output the polish exists
# for. This is per-profile data, not a CLI concern: the flag defaults to None so that
# "the user did not choose" stays a value rather than an inference from Click state.
PROFILE_ADAPTIVE_POLISH = {QWEN_ZIMAGE_PROFILE: False, SDXL_ZIMAGE_PROFILE: True}

SDXL_LIGHTNING_MODEL_ID = "ByteDance/SDXL-Lightning"
SDXL_LIGHTNING_PATTERN = "sdxl_lightning_4step_lora.safetensors"

# Both profiles are certified at a fixed seed because SynthID removal near the
# strength floor is seed-dependent. The step count and CFG are not settable at all --
# each stage owns them (``GLOBAL_STEPS`` / ``FACE_STEPS`` in qwen_zimage_pipeline).
PROFILE_SEED = 0

# sdxl-zimage runs the qwen-zimage recipe on an SDXL global stage, and strength is
# architecture-bound: at Qwen's 0.154 an SDXL global pass leaves SynthID on a native
# 2816x1536 Gemini original, while 0.20, 0.25 and 0.30 all read clean in the Gemini
# app. 0.25 keeps a rung of margin over that boundary, which the historical SDXL
# certification argues for -- it recorded 0.20 as DETECTED against Gemini on an
# older SDXL pipeline. OpenAI is the easier oracle: the profile already cleared
# openai.com/verify at 0.1102, so 0.15 sits above what was verified rather than on
# it. Unknown follows Gemini, the stricter of the two.
#
# Unlike qwen-zimage this is a flat vendor policy rather than a resolution curve,
# because flat values are what was measured. Every verdict above comes from a fixed
# strength at one size; no size dependence has been established for this stage.
SDXL_ZIMAGE_OPENAI_STRENGTH = 0.15
SDXL_ZIMAGE_GEMINI_STRENGTH = 0.25
SDXL_ZIMAGE_UNKNOWN_STRENGTH = SDXL_ZIMAGE_GEMINI_STRENGTH


# sdxl-zimage picks its strength from the VENDOR (unlike qwen-zimage, which derives it
# from image area). An unlisted or unknown vendor falls back to the Gemini value.
_SDXL_ZIMAGE_STRENGTH_BY_VENDOR: dict[str, float] = {
    "openai": SDXL_ZIMAGE_OPENAI_STRENGTH,
    "google": SDXL_ZIMAGE_GEMINI_STRENGTH,
}
_ALIASES = {
    "qwen_zimage": QWEN_ZIMAGE_PROFILE,
    "sdxl_zimage": SDXL_ZIMAGE_PROFILE,
}


def normalize_profile(profile: str) -> str:
    """Normalize spelling and resolve the underscore spellings."""
    value = profile.strip().casefold()
    return _ALIASES.get(value, value)


def resolve_seed(seed: int | None) -> int:
    """Keep both profiles reproducible by default."""
    return PROFILE_SEED if seed is None else seed


def resolve_adaptive_polish(adaptive_polish: bool | None, pipeline: str) -> bool:
    """Return an explicit polish choice, or the profile's calibrated default."""
    if adaptive_polish is not None:
        return adaptive_polish
    return PROFILE_ADAPTIVE_POLISH.get(normalize_profile(pipeline), True)


def strength_default_help() -> str:
    """Describe the live default policy without duplicating its values."""
    return (
        "profile-adaptive (qwen-zimage uses resolution-adaptive denoise; sdxl-zimage "
        f"uses OpenAI {SDXL_ZIMAGE_OPENAI_STRENGTH} / Google {SDXL_ZIMAGE_GEMINI_STRENGTH} / "
        f"unknown {SDXL_ZIMAGE_UNKNOWN_STRENGTH}, from the C2PA issuer)"
    )


def resolve_strength(
    strength: float | None,
    vendor: str | None = None,
    pipeline: str | None = None,
    *,
    size: tuple[int, int] | None = None,
) -> float:
    """Resolve a user override or the calibrated policy for a profile and vendor.

    Total by design. qwen-zimage picks its strength from image area rather than from
    the vendor, so it needs ``size``; returning ``None`` for it instead would push that
    branch onto every caller and move one of the two strength policies outside this
    module. ``size`` is required for qwen-zimage without an explicit strength.
    """
    if strength is not None:
        return strength
    if normalize_profile(pipeline or "") == SDXL_ZIMAGE_PROFILE:
        return _SDXL_ZIMAGE_STRENGTH_BY_VENDOR.get((vendor or "").casefold(), SDXL_ZIMAGE_UNKNOWN_STRENGTH)
    if size is None:
        raise ValueError("qwen-zimage resolves strength from image area, so size is required")
    from remove_ai_watermarks._internal.qwen_zimage_pipeline import resolution_adaptive_denoise

    return resolution_adaptive_denoise(*size)


def vendor_for_strength(image_path: Path) -> Literal["openai", "google"] | None:
    """Select the strength cohort using the input's SynthID provenance evidence."""
    try:
        from remove_ai_watermarks.metadata import synthid_source

        evidence = (synthid_source(image_path) or "").casefold()
    except Exception:
        return None
    if "google" in evidence:
        return "google"
    if "openai" in evidence:
        return "openai"
    return None
