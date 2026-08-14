"""The qwen-zimage recipe with an SDXL global stage.

Only the global regeneration model changes. The face stage is inherited verbatim
from :class:`QwenZImagePipeline` -- same YuNet detection, same SAM masks, same
Z-Image Turbo repair of the original crops, same feathered compositing -- so a
change there cannot silently diverge between the two profiles.

Three pieces cannot be shared, because they are bound to the architecture: the
ControlNet, the four-step distillation LoRA, and the sampler. Strength is bound to
it too, which is the part that is easy to miss: an SDXL global pass leaves SynthID
at the strength Qwen needs. See ``watermark_profiles.SDXL_ZIMAGE_OPENAI_STRENGTH``.
"""

# Diffusers and torch expose mostly untyped tensor APIs. Keep the relaxation local
# to this optional ML boundary.
# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnknownParameterType=false, reportMissingTypeArgument=false, reportMissingTypeStubs=false, reportMissingImports=false, reportArgumentType=false, reportAssignmentType=false, reportReturnType=false, reportCallIssue=false, reportAttributeAccessIssue=false, reportPrivateUsage=false, reportPrivateImportUsage=false
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Any

from PIL import Image

from remove_ai_watermarks._internal.qwen_zimage_pipeline import (
    GLOBAL_STEPS,
    QwenZImagePipeline,
    build_canny_control_image,
)
from remove_ai_watermarks._internal.watermark_profiles import (
    CONTROLNET_CANNY_MODEL,
    SDXL_LIGHTNING_MODEL_ID,
    SDXL_LIGHTNING_PATTERN,
    SDXL_MODEL_ID,
)

log = logging.getLogger(__name__)

SDXL_VAE_MODEL_ID = "madebyollin/sdxl-vae-fp16-fix"
# SDXL aligns to an 8-pixel latent grid, against Qwen's 16.
_LATENT_GRID = 8


def sdxl_target_size(width: int, height: int) -> tuple[int, int]:
    """Floor dimensions to SDXL's latent grid without changing aspect."""
    return max(_LATENT_GRID, (width // _LATENT_GRID) * _LATENT_GRID), max(
        _LATENT_GRID, (height // _LATENT_GRID) * _LATENT_GRID
    )


def requested_steps(effective_steps: int, strength: float) -> int:
    """Translate "spend N denoising steps" into what Diffusers has to be asked for.

    The two runtimes truncate differently and it is easy to port this wrong.
    DiffSynth sets ``sigma_start = denoising_strength`` and then runs *every*
    requested step across the shortened sigma range. Diffusers img2img instead
    truncates the step *count* (``init_timestep = int(steps * strength)``), so
    asking it for four steps at strength 0.15 runs **zero** and returns nothing but
    a VAE round-trip. Ask for enough that ``effective_steps`` actually execute.
    """
    return max(1, math.ceil(effective_steps / max(float(strength), 1e-6)))


@dataclass
class SdxlZImagePipeline(QwenZImagePipeline):
    """Lazy runtime for the SDXL global stage plus the inherited face stage."""

    def __post_init__(self) -> None:
        super().__post_init__()
        self._sdxl_pipe: Any = None

    def _load_sdxl(self) -> Any:
        if self._sdxl_pipe is not None:
            return self._sdxl_pipe
        self._require_cuda()
        import torch
        from diffusers import (
            AutoencoderKL,
            ControlNetModel,
            EulerDiscreteScheduler,
            StableDiffusionXLControlNetImg2ImgPipeline,
        )
        from huggingface_hub import hf_hub_download

        self._progress("Loading SDXL, Lightning LoRA, and Canny ControlNet...")
        token = {"token": self.hf_token} if self.hf_token else {}
        controlnet = ControlNetModel.from_pretrained(CONTROLNET_CANNY_MODEL, torch_dtype=torch.float16, **token)
        vae = AutoencoderKL.from_pretrained(SDXL_VAE_MODEL_ID, torch_dtype=torch.float16, **token)
        pipe = StableDiffusionXLControlNetImg2ImgPipeline.from_pretrained(
            SDXL_MODEL_ID,
            controlnet=controlnet,
            vae=vae,
            torch_dtype=torch.float16,
            variant="fp16",
            add_watermarker=False,
            **token,
        ).to(self.device)
        # SDXL's own four-step distillation, at the strength its authors document.
        # The reference graph loads the Qwen LoRA at 0.8; carrying that number to a
        # different LoRA on a different architecture would be imitation, not parity.
        pipe.load_lora_weights(hf_hub_download(SDXL_LIGHTNING_MODEL_ID, SDXL_LIGHTNING_PATTERN, **token))
        pipe.fuse_lora()
        # SDXL-Lightning is distilled against trailing timestep spacing.
        pipe.scheduler = EulerDiscreteScheduler.from_config(pipe.scheduler.config, timestep_spacing="trailing")
        self._sdxl_pipe = pipe
        return pipe

    def preload(self, *, global_only: bool = False) -> None:
        """Eagerly load the mandatory stage and, by default, the face stack."""
        from remove_ai_watermarks._internal.qwen_zimage_pipeline import _yunet_model_path

        self._load_sdxl()
        _yunet_model_path()
        if not global_only:
            self._load_zimage()
            self._load_sam()

    def _run_global(self, image: Image.Image, strength: float, seed: int | None) -> Image.Image:
        import torch

        pipe = self._load_sdxl()
        target = sdxl_target_size(image.width, image.height)
        prepared = image if image.size == target else image.resize(target, Image.Resampling.LANCZOS)
        control = build_canny_control_image(prepared)
        steps = requested_steps(GLOBAL_STEPS, strength)
        self._progress(f"Running SDXL Canny pass: strength={strength:.4f}, steps={GLOBAL_STEPS} of {steps}...")
        generator = torch.Generator(device=self.device).manual_seed(seed) if seed is not None else None
        result = pipe(
            prompt=self._global_prompt(),
            negative_prompt=self._global_negative(),
            image=prepared,
            control_image=control,
            controlnet_conditioning_scale=float(self.controlnet_conditioning_scale),
            strength=float(strength),
            num_inference_steps=steps,
            guidance_scale=1.0,
            generator=generator,
        ).images[0]
        if result.size != image.size:
            result = result.resize(image.size, Image.Resampling.LANCZOS)
        return result.convert("RGB")

    @staticmethod
    def _global_prompt() -> str:
        from remove_ai_watermarks._internal.qwen_zimage_pipeline import _GLOBAL_PROMPT

        return _GLOBAL_PROMPT

    @staticmethod
    def _global_negative() -> str:
        from remove_ai_watermarks._internal.qwen_zimage_pipeline import _GLOBAL_NEGATIVE

        return _GLOBAL_NEGATIVE
