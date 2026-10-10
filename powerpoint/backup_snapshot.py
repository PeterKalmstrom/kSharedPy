"""
Copy a .pptx (or any file) to a timestamped backup beside the original.

Default: writes `<name>.YYYYMMDD-HHMMSS.pptx` next to the source. Use before
final pre-talk edits, before bulk script runs, or at any "I'd hate to lose
this" moment.

Usage:
    python backup_snapshot.py --file "C:\\path\\to\\deck.pptx"
    python backup_snapshot.py --file deck.pptx --out-dir C:\\backups
    python backup_snapshot.py --file deck.pptx --label pre-rehearsal

Examples of resulting filenames:
    deck.20260427-143005.pptx
    deck.20260427-143005.pre-rehearsal.pptx (with --label)

Notes:
    - Source file must exist; the script does NOT call PowerPoint COM, so
      the file is copied as-is from disk. Save in PowerPoint first if the
      latest changes haven't been written yet.
    - Refuses to overwrite an existing backup with the same timestamp.
"""

import argparse
import io
import shutil
import sys
import time
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")


def make_backup(src: Path, out_dir: Path | None, label: str | None) -> Path:
    if not src.exists():
        raise FileNotFoundError(f"Source not found: {src}")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    suffix = src.suffix
    stem = src.stem
    label_part = f".{label}" if label else ""
    name = f"{stem}.{stamp}{label_part}{suffix}"
    dest = (out_dir or src.parent) / name
    if dest.exists():
        raise FileExistsError(f"Backup already exists: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    return dest


def main():
    p = argparse.ArgumentParser(
        description="Copy a file to a timestamped backup beside the original.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--file", required=True, help="Path to file to back up")
    p.add_argument("--out-dir", help="Backup directory (default: alongside source)")
    p.add_argument("--label", help="Optional label appended after timestamp")
    p.add_argument("--quiet", action="store_true", help="Suppress success output")
    args = p.parse_args()

    src = Path(args.file).resolve()
    out_dir = Path(args.out_dir).resolve() if args.out_dir else None
    try:
        dest = make_backup(src, out_dir, args.label)
    except (FileNotFoundError, FileExistsError) as e:
        sys.stderr.write(f"ERROR: {e}\n")
        sys.exit(1)

    if not args.quiet:
        size_mb = dest.stat().st_size / 1024 / 1024
        print(f"Backed up to {dest} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()
