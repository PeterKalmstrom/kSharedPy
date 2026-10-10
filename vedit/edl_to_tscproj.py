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

    uv run X:\\Source\\Admin\\kSharedPy\\vedit\\edl_to_tscproj.py \\
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
    --quiet         No progress lines on stderr (warnings and errors still show).

Output: the project file only - stdout stays empty; progress goes to stderr.
Standard library only (plain `python edl_to_tscproj.py ...` works too).
"""
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///

import argparse
import copy
import os
from pathlib import Path
from typing import Any

from _vedit_data import kVeditData
from kConsole import kConsole
from kJsonFile import kJsonFile
from kShared import ToolInputException, ToolReportableException, kRun, kS, kToolException

EDIT_RATE = 705_600_000  # Camtasia project ticks per second


class kTscprojPatcher:
    """Applies the keep segments of an EDL to the timeline of one Camtasia project."""

    @staticmethod
    def ToTicks(Seconds: float) -> int:
        """Seconds as Camtasia project ticks."""
        if kS.ErrorMode:
            return 0
        try:
            return round(Seconds * EDIT_RATE)
        except Exception as e:
            kS.GlobalErrorHandler(e, "kTscprojPatcher.ToTicks")
            return 0

    @staticmethod
    def ResolveTscproj(PathArg: str) -> Path | None:
        """Accept either the bundle directory or the inner JSON file."""
        if kS.ErrorMode:
            return None
        try:
            Candidate = Path(PathArg)
            if Candidate.is_dir():
                Found = list(Candidate.glob("*.tscproj"))
                if not Found:
                    raise ToolInputException(f"No .tscproj file found inside {Candidate}")
                return Found[0]
            if not Candidate.exists():
                raise ToolInputException(f"tscproj not found: {Candidate}")
            return Candidate
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kTscprojPatcher.ResolveTscproj(path={PathArg})")
            return None

    @staticmethod
    def FindSourceId(Project: dict[str, Any], SourceHint: str | None) -> int:
        """Return the source bin ID of the target recording."""
        if kS.ErrorMode:
            return 0
        try:
            for Entry in Project.get("sourceBin", []):
                Source = Entry.get("src", "")
                if SourceHint:
                    if SourceHint.lower() in Source.lower():
                        return Entry["id"]
                elif Source.lower().endswith(".trec"):
                    return Entry["id"]
            if Project.get("sourceBin"):
                return Project["sourceBin"][0]["id"]
            raise ToolInputException("No matching source found in project sourceBin")
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, "kTscprojPatcher.FindSourceId")
            return 0

    @staticmethod
    def MaxIdIn(Node: Any) -> int:
        """The largest integer "id" anywhere in Node."""
        if kS.ErrorMode:
            return 0
        try:
            Largest = 0
            if isinstance(Node, dict):
                if isinstance(Node.get("id"), int):
                    Largest = max(Largest, Node["id"])
                for Value in Node.values():
                    Largest = max(Largest, kTscprojPatcher.MaxIdIn(Value))
            elif isinstance(Node, list):
                for Item in Node:
                    Largest = max(Largest, kTscprojPatcher.MaxIdIn(Item))
            return Largest
        except Exception as e:
            kS.GlobalErrorHandler(e, "kTscprojPatcher.MaxIdIn")
            return 0

    @staticmethod
    def MakeSegment(Template: dict[str, Any], NewId: int, StartTicks: int, DurationTicks: int, MediaStartTicks: int,
                    Speed: float = 1.0) -> dict[str, Any]:
        """Build a clip child (ScreenVMFile or AMFile) from a template.

        For Speed > 1: the source mediaDuration stays the same (we consume the same
        amount of source), but the timeline duration shrinks by 1/Speed so the clip
        plays faster.  scalar is set to Speed.  clipSpeedAttribute is enabled.
        """
        if kS.ErrorMode:
            return {}
        try:
            Segment = copy.deepcopy(Template)
            Segment["id"] = NewId
            Segment["start"] = StartTicks
            Segment["mediaDuration"] = DurationTicks  # source ticks consumed (unchanged)
            Segment["mediaStart"] = MediaStartTicks
            if Speed != 1.0:
                Segment["duration"] = round(DurationTicks / Speed)
                Segment["scalar"] = Speed
                if "metadata" in Segment and isinstance(Segment["metadata"], dict):
                    Segment["metadata"]["clipSpeedAttribute"] = True
            else:
                Segment["duration"] = DurationTicks
            return Segment
        except Exception as e:
            kS.GlobalErrorHandler(e, "kTscprojPatcher.MakeSegment")
            return {}

    @staticmethod
    def PatchTracks(Project: dict[str, Any], SourceId: int, KeepSegments: list[dict[str, Any]],
                    NextId: list[int]) -> int:
        """For each timeline track, collapse all StitchedMedia clips that reference
        SourceId into a single new StitchedMedia with one child per EDL keep segment.

        Works for both video tracks (ScreenVMFile children) and audio tracks (AMFile
        children) — the child type is preserved from the template. NextId is a
        one-item list holding the next free id; it is advanced.

        Returns the number of tracks that were patched.
        """
        if kS.ErrorMode:
            return 0
        try:
            Scenes = Project.get("timeline", {}).get("sceneTrack", {}).get("scenes", [])
            if not Scenes or "tracks" not in Scenes[0].get("csml", {}):
                return 0  # a project without a timeline: nothing to patch (reported as a warning by the caller)
            Tracks = Scenes[0]["csml"]["tracks"]
            Patched = 0
            for Track in Tracks:
                Medias = Track.get("medias", [])
                # StitchedMedia nodes whose children reference SourceId (any child type)
                SourceStitched = [
                    Media for Media in Medias
                    if Media.get("_type") == "StitchedMedia"
                    and any(Child.get("src") == SourceId for Child in Media.get("medias", []))
                ]
                if not SourceStitched:
                    continue
                TemplateStitched = SourceStitched[0]  # the first found is the structural template
                TemplateChild = TemplateStitched["medias"][0]  # ScreenVMFile or AMFile
                NewChildren = []
                TimelinePos = 0
                for Segment in KeepSegments:
                    Child = kTscprojPatcher.MakeSegment(
                        TemplateChild, NextId[0], TimelinePos,
                        kTscprojPatcher.ToTicks(Segment["end"] - Segment["start"]),
                        kTscprojPatcher.ToTicks(Segment["start"]), float(Segment.get("speed", 1.0)),
                    )
                    NewChildren.append(Child)
                    NextId[0] += 1
                    TimelinePos += Child["duration"]  # actual timeline duration (affected by speed)
                NewStitched = copy.deepcopy(TemplateStitched)
                NewStitched["id"] = NextId[0]
                NextId[0] += 1
                NewStitched["medias"] = NewChildren
                NewStitched["minMediaStart"] = NewChildren[0]["mediaStart"] if NewChildren else 0
                NewStitched["start"] = 0
                NewStitched["duration"] = TimelinePos
                NewStitched["mediaStart"] = 0
                NewStitched["mediaDuration"] = TimelinePos
                # Preserve any non-source media on the same track (callouts, titles, etc.)
                OtherMedias = [Media for Media in Medias if Media not in SourceStitched]
                Track["medias"] = [NewStitched] + OtherMedias
                Patched += 1
            return Patched
        except Exception as e:
            kS.GlobalErrorHandler(e, "kTscprojPatcher.PatchTracks")
            return 0

    @staticmethod
    def LinkMedia(SourceFile: Path, DestFile: Path, Name: str) -> None:
        """Hard-link (or else symlink) one media file into the output bundle. When neither works the file is not
        needed for the edit itself: a warning tells the person to copy it."""
        if kS.ErrorMode:
            return
        try:
            try:
                os.link(SourceFile, DestFile)
                kConsole.Progress(f"[vedit/edl_to_tscproj] hard-linked {Name}")
                return
            except OSError:  # ERROR-SUPPRESSED-JUSTIFIED: no hard links here (other volume, FAT) - a symlink is the corrected path
                pass
            try:
                DestFile.symlink_to(SourceFile.resolve())
                kConsole.Progress(f"[vedit/edl_to_tscproj] symlinked {Name}")
            except OSError as Failure:  # ERROR-SUPPRESSED-JUSTIFIED: the project is still written; the warning tells the person to copy the media by hand
                kConsole.Warn(f"WARNING: could not link {Name}: {Failure}\n"
                              f"  Copy it manually: {SourceFile} → {DestFile}")
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kTscprojPatcher.LinkMedia(file={SourceFile})")
            return

    @staticmethod
    def SetupOutputBundle(InputWasBundle: bool, SourceBundleDir: Path, OutPath: Path,
                          Project: dict[str, Any]) -> Path | None:
        """Create a bundle directory at OutPath and hard-link (or symlink) media files
        from the source bundle into it.  Returns the path of the JSON file to write.

        If the input was a bare JSON file (not a bundle dir) we do nothing special.
        """
        if kS.ErrorMode:
            return None
        try:
            if not InputWasBundle:
                OutPath.parent.mkdir(parents=True, exist_ok=True)
                return OutPath
            # Output is a bundle directory: create it, JSON lives inside with the same name.
            OutPath.mkdir(parents=True, exist_ok=True)
            OutJson = OutPath / OutPath.name  # e.g. Foo.edited.tscproj/Foo.edited.tscproj
            for Entry in Project.get("sourceBin", []):
                SourceName = Entry.get("src", "")
                if not SourceName or Path(SourceName).is_absolute():
                    continue
                SourceFile = SourceBundleDir / SourceName
                DestFile = OutPath / SourceName
                if not SourceFile.exists() or DestFile.exists():
                    continue
                kTscprojPatcher.LinkMedia(SourceFile, DestFile, SourceName)
            return OutJson
        except Exception as e:
            kS.GlobalErrorHandler(e, f"kTscprojPatcher.SetupOutputBundle(out={OutPath})")
            return None


class kEdlToTscprojApp:
    """Command line."""

    def Run(self) -> int:
        if kS.ErrorMode:
            return 1
        try:
            Parser = argparse.ArgumentParser(
                description="Apply an EDL to a Camtasia .tscproj (cursor-preserving)."
            )
            Parser.add_argument("--edl", required=True)
            Parser.add_argument("--tscproj", required=True)
            Parser.add_argument("--out", required=True)
            Parser.add_argument("--src", default=None,
                                help="Source filename substring to target (default: first .trec)")
            Parser.add_argument("--quiet", action="store_true")
            Args = Parser.parse_args()
            kConsole.Quiet = Args.quiet

            InputWasBundle = Path(Args.tscproj).is_dir()
            TscprojFile = kTscprojPatcher.ResolveTscproj(Args.tscproj)
            if kS.ErrorMode or TscprojFile is None:
                return 1
            SourceBundleDir = TscprojFile.parent
            Edl = kVeditData.LoadEdl(Args.edl)
            Project = kJsonFile.Load(TscprojFile, "project")
            if kS.ErrorMode:
                return 1
            KeepSegments = [Segment for Segment in Edl["segments"] if Segment["keep"]]
            if not KeepSegments:
                raise ToolReportableException("no keep segments in EDL")
            SourceId = kTscprojPatcher.FindSourceId(Project, Args.src)
            if kS.ErrorMode:
                return 1
            SourceName = str(SourceId)
            for Entry in Project.get("sourceBin", []):
                if Entry["id"] == SourceId:
                    SourceName = Entry["src"]
                    break
            TotalKeep = sum(Segment["end"] - Segment["start"] for Segment in KeepSegments)
            kConsole.Progress(f"[vedit/edl_to_tscproj] source bin id={SourceId} ({SourceName}), "
                              f"{len(KeepSegments)} keep segments, {TotalKeep:.1f}s total")
            NextId = [kTscprojPatcher.MaxIdIn(Project) + 1]
            Patched = kTscprojPatcher.PatchTracks(Project, SourceId, KeepSegments, NextId)
            if kS.ErrorMode:
                return 1
            if Patched == 0:
                kConsole.Warn("WARNING: no matching tracks found — output is unchanged")
            else:
                kConsole.Progress(f"[vedit/edl_to_tscproj] patched {Patched} track(s)")
            OutJson = kTscprojPatcher.SetupOutputBundle(InputWasBundle, SourceBundleDir, Path(Args.out), Project)
            if kS.ErrorMode or OutJson is None:
                return 1
            if not kJsonFile.Save(OutJson, Project):
                return 1
            kConsole.Progress(f"[vedit/edl_to_tscproj] wrote {OutJson}")
            return 0
        except kToolException:
            raise
        except Exception as e:
            kS.GlobalErrorHandler(e, "kEdlToTscprojApp.Run")
            return 1


if __name__ == "__main__":
    kRun.Main(kEdlToTscprojApp)
