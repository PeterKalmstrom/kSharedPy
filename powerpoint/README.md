# kSharedPy/powerpoint

Standalone Python utilities for PowerPoint decks via COM automation. Reusable
from any project that needs to read, audit, or analyze .pptx files
programmatically — no MCP server required.

These scripts pair naturally with `building-powerpoint-decks` and `auditing-decks`
skills, but stand alone for use in pipelines, CI, or one-off analysis.

**Related skills (under `X:\Source\Admin\AgentInstruction\skills\`):**
- `auditing-decks` — full triage procedure that uses `audit_deck.py` (below)
  plus the `auditing-taste` codes to produce a punch list of slide defects.
- `presenting-prep` — pre-delivery prep (timing markers, panic sheet, handout,
  contact sheet) that runs after the audit returns clean.
- `building-powerpoint-decks` — the deck builder these helpers come from (public:
  PeterKalmstrom/claude-powerpoint-skill); its `reference/COM.md` holds the COM patterns.

## Requirements

- Windows
- Microsoft PowerPoint installed
- `pywin32` (installed on demand by `uv run <script>.py` - each COM script declares it in its PEP 723
  header; `contact_sheet.py` also declares `pillow`). Without inline-metadata support:
  `uvx --with pywin32 python <script>.py`.

## Tools

### `bulk_read.py` — Dump slide text and notes to JSON

Reads all text content and speaker notes from a deck in a single call. Much
faster than calling `slide_snapshot` for each slide individually.

**Quick start:**
```bash
# Active presentation, whole deck, pretty-printed to stdout
uv run bulk_read.py --pretty

# Specific file, slide range, write to file
uv run bulk_read.py \
  --file "C:\path\to\deck.pptx" \
  --start 10 --end 20 \
  --out dump.json --pretty

# Skip speaker notes (text only)
uv run bulk_read.py --no-notes
```

**Output format:**
```json
[
  {
    "slide": 1,
    "title": "Slide Title",
    "body": "body text | pipe-separated shapes",
    "notes": "full speaker notes..."
  }
]
```

**Flags:**
| Flag | Default | Meaning |
|---|---|---|
| `--file PATH` | active presentation | Open a specific .pptx (read-only, no window) |
| `--start N` | 1 | First slide to read |
| `--end N` | last slide | Last slide to read |
| `--out PATH` | stdout | Write JSON to file instead of stdout |
| `--no-notes` | (notes included) | Skip speaker notes |
| `--pretty` | compact | Indent JSON output |

**Gotchas:**
- If PowerPoint is not running, pass `--file` to open the deck directly.
- UTF-8 output is forced regardless of Windows console code page — safe for `CO₂`, `→`, `°` etc.
- When PowerPoint has multiple decks open and `--file` is omitted, the active one is used.

## Why this exists

The PowerPoint MCP's `slide_snapshot` tool reads one slide per round trip. For a
60-slide deck, analyzing the whole thing means 60 calls and minutes of latency.
`bulk_read.py` does it in a single COM session.

Pairs naturally with:
- Relationship-map builders (extract themes, claims, contradictions across a deck)
- Citation verifiers (pull every numerical claim and fact-check against sources)
- Timing analyzers (count words in notes, estimate talk duration)
- Text density linters (flag slides that break the ~10-word on-screen rule)

---

### `audit_deck.py` — Word-budget and font-floor audit

One-shot quality check across a whole deck. For each slide, computes visible
body word count and minimum body font size, then classifies each slide as
`OK`, `ok-tight`, `** AUDIT`, `[skip]`, or `[vote]`.

**Quick start:**
```bash
# Audit the active presentation
uv run audit_deck.py

# Audit a specific file (read-only, no UI)
uv run audit_deck.py --file "C:\path\to\deck.pptx"

# JSON output (machine-readable, for piping into other tools)
uv run audit_deck.py --json

# Show skipped slides too (covers, votes, opponent slides)
uv run audit_deck.py --all
```

**Output format (table mode):**
```
  # words  min_pt  status       title
----------------------------------------------------------------------------------------------------
  8    20    16pt   ** AUDIT     Some content slide with too many words
  9     7    28pt   OK           Clean hero slide
 12    13    28pt   ok-tight     Just over budget (1 word over)
 ...
  78 slides total: 14 need audit, 6 tight, 21 ok, 8 vote, 29 skipped
```

**Flags:**
| Flag | Default | Meaning |
|---|---|---|
| `--file PATH` | active presentation | Open a specific .pptx (read-only, no window) |
| `--skip "X,Y"` | (built-in defaults) | Extra title-substrings to skip in addition to defaults |
| `--all` | hide skipped | Include `[skip]` and `[vote]` rows in output |
| `--json` | tab-aligned table | Emit JSON list of `{slide, title, body_words, min_font_pt, status}` |

**What counts as a defect:**
- `** AUDIT` if body word count > 12 OR any body text is below 18pt (the slide floor)
- `ok-tight` if body word count is 9–12 (within budget but close)
- `[skip]` if slide number ≤5 (cover, agenda, rules) or title matches a skip pattern
- `[vote]` if slide is a voting / template slide

**When to use:** before declaring a deck done, and after any bulk edit. The
audit catches font-floor violations and word-count creep that visual review
tends to miss.

**Known limit — stale PowerPoint locks:** `audit_deck.py` opens the target
file via `Presentations.Open(..., ReadOnly=True, WithWindow=False)`. If
PowerPoint has held a stale lock on the file (e.g. the file was open in a
prior session that crashed), the COM `Open` call fails with a generic
`pywintypes.com_error: (-2147352567, ...)` and no useful detail.

**Workaround:** open the deck via the PowerPoint MCP first (or just open it
manually in PowerPoint), then re-run the audit. The script will match the
already-open presentation by name and skip the failing `Open` call.

```python
# In an MCP session
manage_presentation(action="open", file_path="C:\\path\\to\\deck.pptx")
# Then
uv run audit_deck.py --file "C:\\path\\to\\deck.pptx"
```
