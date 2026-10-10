"""Apply an EDL to a Camtasia .tscproj file (cursor-preserving workflow).

For each track in the project timeline, all StitchedMedia clips that reference
the source recording are collapsed into a single StitchedMedia containing one
child clip per EDL keep segment.  The output .tscproj can be opened directly in
Camtasia for review and export — all cursor data, zoom-and-pan, and annotations
from the original recording are preserved because the .trec is never re-encoded.

Why this matters: Camtasia stores cursor position and style in proprietary MP4
metadata boxes inside the .trec.  ffmpeg (used by apply.py) drops these boxes
during re-encode, so the edited .mp4 has no cursor.  This script avoids that by
expressing cuts as a Camtasia timeline edit instead.

Usage (give it a FRESH .tscproj — before any manual edits — OR Kate's edited
version; either works because the script replaces all existing source clips):

    uvx python X:\\Source\\Admin\\kSharedPy\\vedit\\edl_to_tscproj.py \\
        --edl edl.json \\
        --tscproj "path/to/Project.tscproj" \\
        --out "path/to/Project.edited.tscproj"

The .tscproj path may be either the bundle directory (Project.tscproj/) or the
JSON file inside it (Project.tscproj/Project.tscproj).

When the input is a bundle directory the output is also written as a bundle
directory; media files are hard-linked (or symlinked) in so Camtasia can find
them without copying gigabytes.

Flags:
    --edl PATH      EDL JSON produced by propose.py. Required.
    --tscproj PATH  Input Camtasia project (bundle dir or inner .tscproj file).
    --out PATH      Output .tscproj path.  Written as a bundle if input was one.
    --src NAME      Filter: only patch tracks whose source bin entry matches this
                    filename substring.  Default: first .trec entry found.
    --quiet         Suppress progress output.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path

EDIT_RATE = 705_600_000  # Camtasia project ticks per second


def to_ticks(seconds: float) -> int:
    return round(seconds * EDIT_RATE)


def resolve_tscproj(path_arg: str) -> Path:
    """Accept either the bundle directory or the inner JSON file."""
    p = Path(path_arg)
    if p.is_dir():
        candidates = list(p.glob("*.tscproj"))
        if not candidates:
            raise FileNotFoundError(f"No .tscproj file found inside {p}")
        return candidates[0]
    return p


def find_src_id(proj: dict, src_hint: str | None) -> int:
    """Return source bin ID for the target recording."""
    for entry in proj.get("sourceBin", []):
        src = entry.get("src", "")
        if src_hint:
            if src_hint.lower() in src.lower():
                return entry["id"]
        elif src.lower().endswith(".trec"):
            return entry["id"]
    if proj.get("sourceBin"):
        return proj["sourceBin"][0]["id"]
    raise ValueError("No matching source found in project sourceBin")


def max_id_in(node: object) -> int:
    m = 0
    if isinstance(node, dict):
        if isinstance(node.get("id"), int):
            m = max(m, node["id"])
        for v in node.values():
            m = max(m, max_id_in(v))
    elif isinstance(node, list):
        for item in node:
            m = max(m, max_id_in(item))
    return m


def make_segment(
    template: dict,
    new_id: int,
    start_t: int,
    dur_t: int,
    media_start_t: int,
    speed: float = 1.0,
) -> dict:
    """Build a clip child (ScreenVMFile or AMFile) from a template.

    For speed > 1: the source mediaDuration stays the same (we consume the same
    amount of source), but the timeline duration shrinks by 1/speed so the clip
    plays faster.  scalar is set to speed.  clipSpeedAttribute is enabled.
    """
    seg = copy.deepcopy(template)
    seg["id"] = new_id
    seg["start"] = start_t
    seg["mediaDuration"] = dur_t  # source ticks consumed (unchanged)
    seg["mediaStart"] = media_start_t
    if speed != 1.0:
        tl_dur = round(dur_t / speed)
        seg["duration"] = tl_dur
        seg["scalar"] = speed
        if "metadata" in seg and isinstance(seg["metadata"], dict):
            seg["metadata"]["clipSpeedAttribute"] = True
    else:
        seg["duration"] = dur_t
    return seg


def patch_tracks(proj: dict, src_id: int, keep_segs: list[dict], next_id: list[int]) -> int:
    """For each timeline track, collapse all StitchedMedia clips that reference
    src_id into a single new StitchedMedia with one child per EDL keep segment.

    Works for both video tracks (ScreenVMFile children) and audio tracks (AMFile
    children) — the child type is preserved from the template.

    Returns the number of tracks that were patched.
    """
    try:
        tracks = proj["timeline"]["sceneTrack"]["scenes"][0]["csml"]["tracks"]
    except (KeyError, IndexError):
        return 0

    patched = 0
    for track in tracks:
        medias = track.get("medias", [])

        # Find StitchedMedia nodes whose children reference src_id (any child type)
        src_stitched = [
            m for m in medias
            if m.get("_type") == "StitchedMedia"
            and any(c.get("src") == src_id for c in m.get("medias", []))
        ]
        if not src_stitched:
            continue

        # Use the first found as a structural template
        template_sm = src_stitched[0]
        template_child = template_sm["medias"][0]  # ScreenVMFile or AMFile

        # Build one child clip per EDL keep segment
        new_children: list[dict] = []
        timeline_pos = 0
        for seg in keep_segs:
            dur_t = to_ticks(seg["end"] - seg["start"])
            media_start_t = to_ticks(seg["start"])
            speed = float(seg.get("speed", 1.0))
            child = make_segment(
                template_child, next_id[0], timeline_pos, dur_t, media_start_t, speed,
            )
            new_children.append(child)
            next_id[0] += 1
            timeline_pos += child["duration"]  # use actual timeline duration (affected by speed)

        # Build the replacement StitchedMedia
        new_sm = copy.deepcopy(template_sm)
        new_sm["id"] = next_id[0]
        next_id[0] += 1
        new_sm["medias"] = new_children
        new_sm["minMediaStart"] = new_children[0]["mediaStart"] if new_children else 0
        new_sm["start"] = 0
        new_sm["duration"] = timeline_pos
        new_sm["mediaStart"] = 0
        new_sm["mediaDuration"] = timeline_pos

        # Preserve any non-source media on the same track (callouts, titles, etc.)
        other_medias = [m for m in medias if m not in src_stitched]
        track["medias"] = [new_sm] + other_medias
        patched += 1

    return patched


def setup_output_bundle(
    input_was_bundle: bool, src_bundle_dir: Path, out_path: Path, proj: dict, quiet: bool
) -> Path:
    """Create a bundle directory at out_path and hard-link (or symlink) media files
    from the source bundle into it.  Returns the path of the JSON file to write.

    If the input was a bare JSON file (not a bundle dir) we do nothing special.
    """
    if not input_was_bundle:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        return out_path

    # Output is a bundle directory: create it, JSON lives inside with the same name.
    out_path.mkdir(parents=True, exist_ok=True)
    out_json = out_path / out_path.name  # e.g. Foo.edited.tscproj/Foo.edited.tscproj

    for entry in proj.get("sourceBin", []):
        src_name = entry.get("src", "")
        if not src_name or Path(src_name).is_absolute():
            continue
        src_file = src_bundle_dir / src_name
        dest_file = out_path / src_name
        if not src_file.exists() or dest_file.exists():
            continue
        try:
            os.link(src_file, dest_file)
            if not quiet:
                print(f"[vedit/edl_to_tscproj] hard-linked {src_name}", file=sys.stderr)
        except OSError:
            try:
                dest_file.symlink_to(src_file.resolve())
                if not quiet:
                    print(f"[vedit/edl_to_tscproj] symlinked {src_name}", file=sys.stderr)
            except OSError as exc:
                sys.stderr.write(
                    f"WARNING: could not link {src_name}: {exc}\n"
                    f"  Copy it manually: {src_file} → {dest_file}\n"
                )

    return out_json


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Apply an EDL to a Camtasia .tscproj (cursor-preserving)."
    )
    ap.add_argument("--edl", required=True)
    ap.add_argument("--tscproj", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--src", default=None, help="Source filename substring to target (default: first .trec)")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    input_was_bundle = Path(args.tscproj).is_dir()

    try:
        tscproj_file = resolve_tscproj(args.tscproj)
    except FileNotFoundError as e:
        sys.stderr.write(f"ERROR: {e}\n")
        return 2

    src_bundle_dir = tscproj_file.parent

    edl = json.loads(Path(args.edl).read_text(encoding="utf-8"))
    proj = json.loads(tscproj_file.read_text(encoding="utf-8"))

    keep_segs = [s for s in edl["segments"] if s["keep"]]
    if not keep_segs:
        sys.stderr.write("ERROR: no keep segments in EDL\n")
        return 1

    try:
        src_id = find_src_id(proj, args.src)
    except ValueError as e:
        sys.stderr.write(f"ERROR: {e}\n")
        return 2

    if not args.quiet:
        src_name = next(
            (e["src"] for e in proj.get("sourceBin", []) if e["id"] == src_id), str(src_id)
        )
        total_keep_s = sum(s["end"] - s["start"] for s in keep_segs)
        print(
            f"[vedit/edl_to_tscproj] source bin id={src_id} ({src_name}), "
            f"{len(keep_segs)} keep segments, {total_keep_s:.1f}s total",
            file=sys.stderr,
        )

    next_id = [max_id_in(proj) + 1]
    n_patched = patch_tracks(proj, src_id, keep_segs, next_id)

    if n_patched == 0:
        sys.stderr.write("WARNING: no matching tracks found — output is unchanged\n")
    elif not args.quiet:
        print(f"[vedit/edl_to_tscproj] patched {n_patched} track(s)", file=sys.stderr)

    out_path = Path(args.out)
    out_json = setup_output_bundle(input_was_bundle, src_bundle_dir, out_path, proj, args.quiet)
    out_json.write_text(json.dumps(proj, indent=2, ensure_ascii=False), encoding="utf-8")

    if not args.quiet:
        print(f"[vedit/edl_to_tscproj] wrote {out_json}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
