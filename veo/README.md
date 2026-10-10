# kSharedPy/veo

Google Veo 3.1 video-generation helpers. Wraps the polling-and-download
boilerplate behind a single CLI / JSON-spec interface.

## Tools

### `batch_video_gen.py` — Batch video generation

Generates one or many short MP4 videos from text prompts. Polls the Veo API
until the video is ready (typically ~60 s), downloads it, and saves to disk.

**Quick start:**
```bash
# Single video, inline
GEMINI_API_KEY="..." uvx --with google-genai python batch_video_gen.py \
    --prompt "Cinematic side view of a leaf falling onto still water" \
    --out C:\path\to\leaf.mp4

# Batch from JSON spec
GEMINI_API_KEY="..." uvx --with google-genai python batch_video_gen.py \
    --spec videos.json
```

**Spec file format:**
```json
[
  {"prompt": "...", "out": "C:\\videos\\intro.mp4"},
  {"prompt": "...", "out": "C:\\videos\\outro.mp4", "model": "veo-3.1-fast-generate-preview"}
]
```

**Flags:**
| Flag | Default | Meaning |
|---|---|---|
| `--prompt TEXT` | — | Inline prompt (single-video mode) |
| `--out PATH` | — | Output MP4 path (required with `--prompt`) |
| `--model NAME` | `veo-3.1-generate-preview` | Veo model id |
| `--spec PATH` | — | JSON spec file with list of jobs |

## Models

| Model | Speed | Cost | Quality |
|---|---|---|---|
| `veo-3.1-fast-generate-preview` | fastest | cheapest | acceptable |
| `veo-3.1-generate-preview` (default) | medium | mid | good |
| `veo-3.1-pro-generate-preview` | slow | expensive | best |

## Output

- 8-second 720p MP4 with native audio
- Typically 1–10 MB per clip
- Generation time: ~60 seconds per clip including polling

## Requires a PAID key — a free-tier key 429s

**Runtime-confirmed 2026-07-27** (google-genai 2.14.0, Developer API).

Veo needs billing enabled on the key's Google Cloud project. A free-tier key
fails with **429 RESOURCE_EXHAUSTED** — and the trap is that it looks like a
transient rate limit when it is a hard tier gate that never clears by waiting.

The free-tier signature, all three true at once on the same key:

| Probe | Free-tier result |
|---|---|
| `client.models.generate_content(...)` on a text model | ✅ succeeds |
| `client.models.list()` | ✅ lists all three `veo-3.1-*` ids |
| `client.models.generate_videos(...)` | ❌ 429 RESOURCE_EXHAUSTED |

**Model visibility is not evidence of access.** Free → Tier 1 is "link an active
billing account", and takes effect instantly.

### Two config fields the SDK accepts but the Developer API rejects

Both are valid in `types.GenerateVideosConfig` and fail only when sent:

- `generate_audio=True` → client-side `ValueError`, "only supported in Gemini
  Enterprise Agent Platform mode". Veo 3.1 emits native audio without it.
- `person_generation="dont_allow"` → **400**, "dont_allow for personGeneration
  is currently not supported". Excluding people rests on `negative_prompt`.

## Known limits — don't waste reprompts

Diffusion video models reliably fail at certain physics scenarios. Don't burn
budget retrying these — switch to stock footage instead:

- **Sequential rigid-body chain reactions** (dominoes, Newton's cradle, pool break)
- **Counting** (exactly N objects, ordered sequences)
- **Text in any language** (use overlay text in the consuming app instead)
- **Complex multi-step sequences** beyond ~3 actions

For these: search Pexels/Mixkit for free stock, or `yt-dlp` a Creative
Commons clip and trim with `ffmpeg`. Faster and better than reprompting.

## Image-to-video

When direction or starting state matters, generate a still image first via
`kSharedPy/gemini/batch_image_gen.py` and feed it as the starting frame.
Anchoring the first frame dramatically reduces variance in framing,
direction, and consistency.

## Related

- [`kSharedPy/gemini/batch_image_gen.py`](../gemini/batch_image_gen.py) —
  companion image helper, same API style.
- [`configuring-powerpoint-mcp` SKILL.md](../../AgentInstruction/skills/configuring-powerpoint-mcp/SKILL.md)
  `## Veo Integration` section — full prompting guide and PowerPoint
  embedding examples.
