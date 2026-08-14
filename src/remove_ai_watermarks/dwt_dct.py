"""DWT-DCT decoder compatible with invisible-watermark's ``dwtDct`` path.

Derived from ShieldMnt/invisible-watermark ``imwatermark/maxDct.py`` (MIT),
trimmed to the matrix path used by Stable Diffusion, SDXL, and FLUX.

Copyright (c) 2021 ShieldMnt

The complete upstream license is distributed in
``licenses/invisible-watermark-MIT.txt``.
"""

# pyright: reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportMissingTypeStubs=false

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import cv2
import numpy as np
import pywt

if TYPE_CHECKING:
    from numpy.typing import NDArray

_DEFAULT_SCALES = (0, 36, 36)
_DEFAULT_BLOCK = 4


class _DecodeMaxDct:
    """Extract frequency-domain bits using the upstream matrix algorithm."""

    def __init__(
        self,
        wm_lengths: tuple[int, ...],
        scales: tuple[int, int, int] = _DEFAULT_SCALES,
        block: int = _DEFAULT_BLOCK,
    ) -> None:
        self._wm_lengths = wm_lengths
        self._scales = scales
        self._block = block

    def decode(self, bgr: NDArray[Any]) -> dict[int, NDArray[Any]]:
        row, col, _channels = bgr.shape
        yuv = cv2.cvtColor(bgr, cv2.COLOR_BGR2YUV)

        scores_by_length = {wm_len: ([0] * wm_len, [0] * wm_len) for wm_len in self._wm_lengths}
        for channel in range(2):
            if self._scales[channel] <= 0:
                continue
            ca1, _detail = pywt.dwt2(yuv[: row // 4 * 4, : col // 4 * 4, channel], "haar")
            self._decode_frame(ca1, self._scales[channel], scores_by_length)

        return {
            wm_len: np.asarray(sums) * 255 > np.asarray(counts) * 127
            for wm_len, (sums, counts) in scores_by_length.items()
        }

    def _decode_frame(
        self,
        frame: NDArray[Any],
        scale: int,
        scores_by_length: dict[int, tuple[list[int], list[int]]],
    ) -> None:
        row, col = frame.shape
        bit_index = 0
        for i in range(row // self._block):
            for j in range(col // self._block):
                block = frame[
                    i * self._block : i * self._block + self._block,
                    j * self._block : j * self._block + self._block,
                ]
                inferred = self._infer_bit(block, scale)
                for wm_len, (sums, counts) in scores_by_length.items():
                    bucket = bit_index % wm_len
                    sums[bucket] += inferred
                    counts[bucket] += 1
                bit_index += 1

    def _infer_bit(self, block: NDArray[Any], scale: int) -> int:
        position = int(np.argmax(np.abs(block.flatten()[1:]))) + 1
        i, j = position // self._block, position % self._block
        value = abs(float(block[i][j]))
        return int((value % scale) > 0.5 * scale)


def decode_dwt_dct(bgr: NDArray[Any], wm_len: int) -> NDArray[Any]:
    """Extract ``wm_len`` watermark bits from a BGR image."""
    return decode_dwt_dct_lengths(bgr, (wm_len,))[wm_len]


def decode_dwt_dct_lengths(bgr: NDArray[Any], wm_lengths: tuple[int, ...]) -> dict[int, NDArray[Any]]:
    """Extract several watermark lengths with one DWT and block scan."""
    if bgr.size == 0 or min(bgr.shape[:2]) * max(bgr.shape[:2]) < 256 * 256:
        raise RuntimeError("image too small, should be larger than 256x256")
    if not wm_lengths or any(wm_len <= 0 for wm_len in wm_lengths):
        raise ValueError("watermark lengths must be positive")
    return _DecodeMaxDct(wm_lengths=tuple(dict.fromkeys(wm_lengths))).decode(bgr)
