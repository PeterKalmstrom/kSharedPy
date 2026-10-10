# kSharedPy

Shared Python utilities for any project. Run with `uv run <script>.py` so no global pip install is required:
every runnable script declares its own dependencies in a PEP 723 header (`# /// script`), and `uv run`
installs them on demand.

## Folders

- **`gemini/`** — Google Gemini API helpers
  - `batch_image_gen.py` — generate one or many images from a prompt or JSON spec
  - `smoke_test.py` — verify the API key, network, and `batch_image_gen.py` work end-to-end
- **`veo/`** — Google Veo 3.1 video generation helpers
  - `batch_video_gen.py` — generate one or many short videos from a prompt or JSON spec
- **`powerpoint/`** — PowerPoint COM utilities (Windows only, requires `pywin32`)
  - `audit_deck.py` — word-budget and font-floor audit across a whole deck
  - `bulk_read.py` — dump slide text and notes to JSON in one COM session
  - `contact_sheet.py` — render a grid PNG of every slide for at-a-glance review
  - `backup_snapshot.py` — copy a `.pptx` to a timestamped backup beside the original
  - `export_pdf.py` — export a deck as PDF (slides / notes pages / handout grid)

## Conventions

- Every script is runnable as `uv run <script.py>` — no virtual env required. Its PEP 723 header
  (`# /// script` ... `# ///`, right after the docstring) is the one place its dependencies are listed;
  `kpy\tests\test_dependencies.py` checks the header against the script's real imports. Optional extras are
  added per run (`uv run --with silero-vad propose.py ...`), as each docstring says.
- A uv without inline-metadata support still works with `uvx --with <deps> python <script.py>` (deps from the header)
- API keys via environment variables (e.g., `GEMINI_API_KEY`)
- Inputs via CLI flags or JSON spec files
- UTF-8 stdout forced (Windows safe)
- Each script's docstring documents flags, environment, and gotchas
- Long-output scripts support `--quiet` for use inside larger pipelines (errors still go to stderr)
- The masters carry type hints (Python 3.10 syntax) and are checked with `mypy` from the repo root (`mypy.ini`
  excludes the generated copies, so each module is checked once, from its master). CI
  (`.github/workflows/check.yml`, Python 3.10 and 3.12 on Linux) runs check_kpattern, `kPyCompiler --action verify`,
  mypy, pyflakes and the kshared, kpy and powerpoint test suites

## Quick start from another script

When you want to call one of these helpers from your own project script
(e.g., a slide builder that needs to generate a backdrop image first), use
`subprocess` to invoke the helper through `uv run`. This avoids the need for
your project to depend on `google-genai`, `pywin32`, etc. directly.

**Single-shot pattern** (one image / one video):

```python
import os, subprocess, sys

HELPER = r"X:\Source\Admin\kSharedPy\gemini\batch_image_gen.py"
OUT = r"C:\path\to\backdrop.png"
PROMPT = "Cinematic dramatic photograph of ... no text, no logos."

sys.exit(subprocess.run([
    "uv", "run", HELPER,
    "--prompt", PROMPT, "--out", OUT, "--aspect", "16:9",
]).returncode)
```

**Batch pattern** (multiple jobs from a spec):

```python
import json, os, subprocess, sys, tempfile

HELPER = r"X:\Source\Admin\kSharedPy\gemini\batch_image_gen.py"
spec = [
    {"prompt": "...", "out": r"C:\images\one.png"},
    {"prompt": "...", "out": r"C:\images\two.png"},
]

with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
    json.dump(spec, f, ensure_ascii=False)
    spec_path = f.name

try:
    rc = subprocess.run([
        "uv", "run", HELPER,
        "--spec", spec_path, "--quiet",
    ]).returncode
finally:
    os.unlink(spec_path)
sys.exit(rc)
```

**Why subprocess (not import):** the helpers depend on `google-genai` /
`pywin32`, which most consumer projects do not want to add as a hard
dependency. `uv run` reads the helper's header and provisions them per call. Subprocess also
keeps API failures (network errors, quota) from crashing your build script.

The same pattern works for `kSharedPy/veo/batch_video_gen.py`.

## Owner decisions 2026-10-10

- **kSharedPy is the MASTER of all shareable Python.** Shareable code in published products (first:
  `AgentInstruction\skills\building-powerpoint-decks\scripts\`, incl. `kShared.py` and the drifted
  `backup_snapshot` / `contact_sheet` / `export_pdf`) moves here. Every file, class, method and line is
  reviewed on the way in for the `kShared.py` error pattern and object orientation.
- **Delivery uses a kPS-style compiler** (option D): published products get self-contained output
  compiled from parts here, never hand-edited copies. More Python products are planned.
- **A `writing-python` skill** in `AgentInstruction\skills\` documents the master, the compiler and the rules.

## The master and the compiler (kpy)

- `kshared\kShared.py` - the MASTER of the error pattern (`kS`, `kRun`, `kErrorReport`, the tool exceptions).
  Product-specific values are lines marked `# kpy:setting <Name>` (`Product`, `VersionFile`, `ReportFilePrefix`,
  `ErrorLogVariable`).
- `kpy\kPyCompiler.py` - writes a product's generated modules from its `kpy.manifest.json`:

  ```
  python kpy\kPyCompiler.py --manifest <product>\kpy.manifest.json                    # check (read-only)
  python kpy\kPyCompiler.py --manifest ... --action compile [--adopt]                 # write
  python kpy\kPyCompiler.py --manifest ... --action verify [--standalone]             # CI, exit 5 if off
  python -m unittest discover -s kpy\tests                                             # compiler tests
  ```

  A generated file starts with `# GENERATED by kPyCompiler`, names its master parts and carries the SHA-256 of
  its body, so a hand edit is caught even without the master (`--standalone`). Never edit a generated file.
- First product: `AgentInstruction\skills\building-powerpoint-decks\kpy.manifest.json` -> `scripts\kShared.py`
  (adopted 2026-10-10; the skill's CI and self-test 197/197 passed on the generated file).
