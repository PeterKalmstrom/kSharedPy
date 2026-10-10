# vedit

A small pipeline of composable CLIs that turns a raw screen recording into a clean, cut, subtitled video. Each stage writes a JSON or text file you can inspect or hand-edit between steps.

```
record.trec ──► audio_check.py  → PASS/FAIL  (MANDATORY gate; refuse if FAIL)
            │
            ├─► transcribe.py ──► transcript.json
            │
            ├─► propose.py    ──► edl.json   (uses transcript.json)
            │
            │   review.py    ──► review.md   (cuts + transcript context, read-only)
            │   --- edit edl.json by hand to flip keep flags ---
            │
            ├─► apply.py      ──► record.edited.mp4        (ffmpeg path — no cursor)
            │   OR
            ├─► edl_to_tscproj.py ──► record.edited.tscproj  (Camtasia path — cursor preserved)
            │
            ├─► subtitles.py  ──► record.edited.srt    (LLM-polished by default)
            │
            └─► srt_to_script.py ──► record.script.txt (re-record audio from this)

(polish_script.py — standalone: LLM-polish any text file to clean reading prose)
```

The full procedure, calibration findings, and decision rationale live in the **`editing-screen-recordings` skill** at `X:\Source\Admin\AgentInstruction\skills\editing-screen-recordings\SKILL.md`. This README is just the flag/script reference.

## Pipeline

| Stage | Script | What it does |
|---|---|---|
| 0   | `audio_check.py`     | **MANDATORY** audio quality gate. Refuses to proceed if audio doesn't match the calibrated profile (mic-change detector). |
| 1   | `transcribe.py`      | Local Whisper (`faster-whisper`, CPU, int8) → word-level transcript JSON |
| 2   | `propose.py`         | ffmpeg silence detection + Gemini retake-finder + Gemini filler-trim → EDL JSON. Word-snaps cut boundaries to silence gaps. Optional `--validate-retakes` multimodal frame check, `--static` freeze detection, `--trim-audio-events` vocalization detection. |
| 2.5 | `review.py`          | Render EDL + transcript context as markdown for human review. With `--restore CATEGORY` writes a new EDL with one cut class flipped back to `keep:true`. |
| 3a  | `apply.py`           | **ffmpeg path.** Cut + concat the `keep` ranges → output `.mp4`. Re-encodes to `yuv420p` + 30 fps CFR. **Drops Camtasia cursor data** (see 3b). |
| 3b  | `edl_to_tscproj.py`  | **Camtasia path.** Apply EDL cuts to a `.tscproj` project file instead of re-encoding. Cursor, zoom-and-pan, and annotations from the `.trec` are all preserved. Open the output project in Camtasia to export. |
| 4   | `subtitles.py`       | Emit `.srt`/`.vtt`, re-mapped to cut timeline if `--edl` given. **Polishes cue text via Gemini by default** — readability over verbatim accuracy. |
| 5   | `srt_to_script.py`   | Strip timestamps from `.srt` for re-recording audio. Inherits any polish already applied to the .srt. |
| —   | `polish_script.py`   | Standalone LLM polish of any text file (script/outline). Same prompt as subtitles.py. |

## EDL format

A flat list of consecutive segments covering the whole video. Each has a `keep` flag and a `reason`. Segments do not overlap, and their starts/ends chain (segment N's `end` equals segment N+1's `start`).

```json
{
  "source": "F:/path/to/input.mp4",
  "duration_s": 2143.7,
  "version": 1,
  "segments": [
    {"start":   0.00, "end":  12.34, "keep": true,  "reason": ""},
    {"start":  12.34, "end":  18.71, "keep": false, "reason": "silence>3s"},
    {"start":  18.71, "end":  45.20, "keep": true,  "reason": ""},
    {"start":  45.20, "end":  47.80, "keep": false, "reason": "retake: 'wait, let me start over' [frame ok]"},
    {"start":  47.80, "end":  85.10, "keep": true,  "reason": ""},
    {"start":  85.10, "end":  87.40, "keep": false, "reason": "filler: or something like that"}
  ]
}
```

To hand-edit: flip `keep` between true/false, or split a segment into two by inserting a new boundary. The tool does NOT require segments to start/end on keyframes — `apply.py` re-encodes by default.

**Reason categories** emitted by `propose.py`:
- `silence>Ns` — pure silence longer than `--silence-min-s`
- `static+silent` — freeze + silence overlap (only with `--static`, off by default)
- `retake: '<phrase>' [frame ok]` — text-LLM retake; `[frame ok]` means `--validate-retakes` confirmed it
- `filler: <phrase>` — Gemini filler-trim (only with `--trim-filler`)
- `audio_event: throat_clear|filler_sound|mouth_sound` — audible non-speech vocalization in an inter-word gap (only with `--trim-audio-events`)

## Quick start (validated config)

```powershell
# 0. Audio gate — MANDATORY. Stops everything if audio doesn't match the profile.
uv run X:\Source\Admin\kSharedPy\vedit\audio_check.py `
  --in "F:\path\input.trec" `
  --profile F:\Dropbox\Source\Recording\.audio_profile.json
# exit 1 = fail → STOP. Re-record with the correct mic.

# 1. Transcribe (CPU ~3x realtime with small.en)
uv run X:\Source\Admin\kSharedPy\vedit\transcribe.py `
  --in "F:\path\input.trec" --out transcript.json

# 2. Propose cuts — validated config (82% precision / 87% recall vs a hand edit)
uv run X:\Source\Admin\kSharedPy\vedit\propose.py `
  --in "F:\path\input.trec" --transcript transcript.json --out edl.json `
  --vad-engine transcript --validate-retakes --trim-filler

# 2.5. Review proposed cuts (markdown, read-only)
uv run X:\Source\Admin\kSharedPy\vedit\review.py `
  --edl edl.json --transcript transcript.json --only retake --out review.md
# Open review.md; if a cut looks wrong, edit edl.json to flip "keep": false → true.

# 3a. Apply (ffmpeg path — plain .mp4, cursor NOT preserved)
uv run X:\Source\Admin\kSharedPy\vedit\apply.py `
  --in "F:\path\input.trec" --edl edl.json --out edited.mp4

# 3b. Apply (Camtasia path — cursor, zoom-and-pan, annotations preserved)
#     Use a FRESH .tscproj (before any manual Camtasia edits). Then open the
#     output in Camtasia and export from there.
uv run X:\Source\Admin\kSharedPy\vedit\edl_to_tscproj.py `
  --edl edl.json `
  --tscproj "F:\path\Project.tscproj" `
  --out "F:\path\Project.edited.tscproj"

# 4. (Optional) Subtitles
uv run X:\Source\Admin\kSharedPy\vedit\subtitles.py `
  --transcript transcript.json --edl edl.json --out edited.srt

# 5. (Optional) Reading script for re-recording the audio
uv run X:\Source\Admin\kSharedPy\vedit\srt_to_script.py `
  --in edited.srt --out script.txt --paragraphs
```

## One-time audio profile calibration

```powershell
uv run X:\Source\Admin\kSharedPy\vedit\audio_check.py `
  --in good_known_recording.trec --calibrate `
  --profile-out F:\Dropbox\Source\Recording\.audio_profile.json
```

Five metrics are checked: `mean_volume_db`, `max_volume_db`, `integrated_lufs`, `true_peak_db`, `loudness_range_lu`. Default tolerances are tight (±2–3 dB / LUFS / LU) — designed to catch a mic change, not minor speaking-loudness variation. Recalibrate when your setup genuinely changes.

## Subtitle / script readability rule

`.srt`/`.vtt` and reading scripts are **LLM-polished by default** — verbal restarts, filler, and grammar artifacts are removed. Readability is more important than verbatim accuracy. Pass `--no-polish` to `subtitles.py` if you genuinely need a verbatim research transcript.

## Conventions

- Run with `uv run <script>.py` — no venv required. Each script declares its dependencies in a PEP 723
  header (`# /// script`): `transcribe.py` faster-whisper + static-ffmpeg, `propose.py` google-genai +
  static-ffmpeg, `apply.py` / `audio_check.py` static-ffmpeg, `subtitles.py` / `polish_script.py`
  google-genai, the rest none. Optional: `uv run --with silero-vad propose.py --vad-engine silero ...`.
  Without inline-metadata support: `uvx --with <deps> python <script>.py`.
- ffmpeg/ffprobe are bundled via the `static-ffmpeg` PyPI package — no system install needed. First run downloads ~80MB to the uv cache.
- `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) must be set in the environment for any LLM step: `propose.py` (retake-finder, filler-trim, multimodal validate), `subtitles.py` (polish, by default), `polish_script.py` (always).
- All scripts force UTF-8 stdout for Windows safety.
- All scripts accept `--quiet` for use in larger pipelines (errors still go to stderr).

## Camtasia .trec files

Camtasia 2018+ `.trec` files are MP4 containers with custom metadata. ffmpeg reads them directly — point any vedit script at a `.trec` and it just works. Output extension should still be `.mp4`.

**Four gotchas:**

1. **Cursor data is not in the video stream.** Camtasia stores cursor position and appearance in proprietary MP4 metadata boxes inside the `.trec` (referenced by GUIDs in the `.tscproj` `metaData` field). ffmpeg drops these boxes during re-encode. **If cursor preservation matters, use `edl_to_tscproj.py` (stage 3b) instead of `apply.py`.** The `.tscproj` approach expresses cuts in Camtasia's timeline JSON; Camtasia itself renders the cursor on export.
2. **Two audio streams.** Camtasia records mic + system audio as separate AAC streams. `apply.py` defaults to keeping stream 0 (mic). Use `--audio-stream 1` to keep system audio, or `--mix-audio` to combine. (Not relevant for the `.tscproj` path — Camtasia handles audio mixing natively.)
3. **tscc2 video codec.** Camtasia uses TechSmith's tscc2 codec. The default re-encode handles it; do NOT pass `--copy` on tscc2 input (most players can't decode tscc2 directly).
4. **yuv444p source + VFR timing.** tscc2 records yuv444p with variable frame timing. `apply.py` defaults to `--pix-fmt yuv420p` and `--fps 30` (CFR) for player compatibility. Symptom of regression: Windows Explorer reports the height as 1072 instead of 1080 (chroma plane mis-read), or as ~6 fps.

## Hardware notes

- Tuned for **CPU-only** Whisper (no Nvidia GPU on the dev box). Default model is `small.en` with `int8` quantization — measured ~3.28x realtime steady-state on the dev box (15-min recording → ~4:38 wall time). Short clips look slower because model-load overhead (~10s) dominates.
- For higher accuracy at the cost of speed, pass `--model medium.en` (~2x slower) or `--model large-v3` (~5x slower).
- For Swedish or mixed languages, switch to `--model large-v3 --language auto`. Expect ~0.1–0.2x realtime on CPU.
- If you later add an Nvidia GPU, pass `--device cuda --compute-type float16` for 5–20x realtime.

## Calibration findings (2026-05)

Validated against two hand-edited recordings (Azure + Fortnox tutorials):

- **Light edit** (15-min source, ~33% cut): vedit matched the hand edit at **82% precision / 87% recall**, final duration within 15 seconds.
- **Heavy edit** (41-min source, ~60% cut): precision stayed at **83%** but recall dropped to **34%** because the human editor was making content-level cuts (whole 30-70s technical passages) that vedit can't make.

**The lesson**: vedit does line-level cleanup. For heavy editorial cuts (removing whole sections), its output is a starting point — the user still has to make section-level cuts that require knowing what's redundant for the final viewer.

See the SKILL doc for full calibration tables and the calibration-drift findings (`Azure mean -30.4 dB`, `Fortnox mean -32.0 dB`, `wrong-mic recording mean -25.4 dB` failing the gate).
