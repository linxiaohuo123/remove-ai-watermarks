# Pipeline fidelity evaluation

This evaluation compares diffusion pipelines with
`scripts/fidelity_metrics.py`. The images themselves have one canonical home
in `data/synthid/originals/`; this directory stores only evaluation-specific
ground truth and instructions.

| Original | Provider | Content | Exercises |
| --- | --- | --- | --- |
| `ChatGPT Image May 31, 2026, 02_03_55 PM.png` | OpenAI | Multilingual typography | Text preservation |
| `Gemini_Generated_Image_633uuy633uuy633u.png` | Google | Landscape with a Chinese sign | CJK text preservation |
| `Gemini_Generated_Image_y48j3cy48j3cy48j.png` | Google | Portrait grid | Face identity and skin texture |

## Text ground truth

`ground-truth.json` contains hand-verified OCR for the two text-bearing
originals. To regenerate an OCR seed:

```bash
uv run scripts/fidelity_metrics.py ocr \
  "data/synthid/originals/ChatGPT Image May 31, 2026, 02_03_55 PM.png" \
  data/synthid/originals/Gemini_Generated_Image_633uuy633uuy633u.png \
  --langs en,ru,ch \
  --out data/evaluations/fidelity/ground-truth.json
```

Verify and correct the generated text by hand before using it as ground truth.

## Compare

```bash
uv run scripts/fidelity_metrics.py compare \
  --original data/synthid/originals/Gemini_Generated_Image_y48j3cy48j3cy48j.png \
  --variant controlnet=<out>.png \
  --variant qwen=<out>.png \
  --ocr-langs ""
```
