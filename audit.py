#!/usr/bin/env python3
"""PlexDoViTool — Stage 1 read-only Dolby Vision audit.

Walks each configured library root for .mkv files, probes every video stream
for a Dolby Vision configuration record, and classifies each file by its DV
profile. For Profile 7 it additionally pipes the HEVC stream through
``dovi_tool info`` to determine whether the enhancement layer is FEL (Full) or
MEL (Minimum) — the distinction that drives the eventual Stage 2 conversion.

This tool is strictly READ-ONLY with respect to the library: library files are
only ever passed to ffprobe/ffmpeg as read inputs. The only files opened for
writing live under ``reports_dir``.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

FFPROBE_TIMEOUT_SEC = 60
# FEL vs MEL is a stream-wide property of a Profile 7 file (every RPU is the
# same type), so we only feed dovi_tool the first 30s of HEVC (see the `-t`
# flag in probe_el_type) instead of the whole 60-90 GB stream. With the input
# bounded, 2 minutes is plenty for dovi_tool to process the truncated stream.
DOVI_INFO_TIMEOUT_SEC = 120

# Classifications.
CLASS_NO_DV = "NO_DV"
CLASS_DV_OK = "DV_OK"
CLASS_DV7_FEL = "DV7_FEL"
CLASS_DV7_MEL = "DV7_MEL"
CLASS_DV_UNKNOWN = "DV_UNKNOWN"

CLASSIFICATIONS = [
    CLASS_NO_DV,
    CLASS_DV_OK,
    CLASS_DV7_FEL,
    CLASS_DV7_MEL,
    CLASS_DV_UNKNOWN,
]


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def walk_library(root: Path, extensions: set[str]):
    """Yield files under ``root`` whose suffix matches ``extensions``."""
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in sorted(filenames):
            if Path(name).suffix.lower() in extensions:
                yield Path(dirpath) / name


# --------------------------------------------------------------------------- #
# Probing
# --------------------------------------------------------------------------- #
def run_ffprobe(path: Path) -> Optional[dict]:
    """Return parsed ffprobe JSON, or None on any failure (never raises)."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-print_format", "json",
                "-show_streams", "-show_format",
                str(path),
            ],
            capture_output=True,
            timeout=FFPROBE_TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None
    except FileNotFoundError:
        sys.exit("error: ffprobe not found on PATH (install ffmpeg)")
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


@dataclass
class DoviRecord:
    dv_profile: Optional[int] = None
    dv_level: Optional[int] = None
    dv_compatibility_id: Optional[int] = None
    bl_present: Optional[int] = None
    el_present: Optional[int] = None
    rpu_present: Optional[int] = None


def find_dovi_record(probe: dict) -> Optional[DoviRecord]:
    """Scan video streams' side_data for a DOVI configuration record."""
    for stream in probe.get("streams", []):
        if stream.get("codec_type") != "video":
            continue
        for side in stream.get("side_data_list", []) or []:
            sd_type = str(side.get("side_data_type", "")).lower()
            # ffprobe labels this "DOVI configuration record"; match loosely so a
            # minor ffmpeg wording change still trips the detector.
            if "dovi" in sd_type or "dolby vision" in sd_type:
                return DoviRecord(
                    dv_profile=_as_int(side.get("dv_profile")),
                    dv_level=_as_int(side.get("dv_level")),
                    dv_compatibility_id=_as_int(side.get("dv_bl_signal_compatibility_id")),
                    bl_present=_as_int(side.get("bl_present_flag")),
                    el_present=_as_int(side.get("el_present_flag")),
                    rpu_present=_as_int(side.get("rpu_present_flag")),
                )
    return None


def _as_int(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def probe_el_type(path: Path) -> tuple[Optional[str], str]:
    """Determine FEL vs MEL for a Profile 7 file via an ffmpeg->dovi_tool pipe.

    Returns (el_type, note) where el_type is "FEL", "MEL", or None. Never
    raises and never writes to the library — the HEVC stream is piped, not
    extracted to disk.
    """
    ffmpeg_cmd = [
        "ffmpeg", "-loglevel", "error",
        # -t before -i limits how much of the INPUT ffmpeg reads. FEL/MEL is
        # stream-wide, so the first 30s carries more than enough RPU data and
        # we avoid reading the entire 60-90 GB file. If a clearly-Profile-7
        # file comes back DV_UNKNOWN, dovi_tool needed more frames: bump to 60.
        "-t", "30",
        "-i", str(path),
        "-map", "0:v:0", "-c", "copy",
        "-bsf:v", "hevc_mp4toannexb",
        "-f", "hevc", "-",
    ]
    dovi_cmd = ["dovi_tool", "info", "-i", "-", "-s"]

    try:
        ff = subprocess.Popen(
            ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
    except FileNotFoundError:
        return None, "ffmpeg_not_found"

    try:
        dv = subprocess.Popen(
            dovi_cmd,
            stdin=ff.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
    except FileNotFoundError:
        if ff.stdout:
            ff.stdout.close()
        ff.kill()
        ff.wait()
        return None, "dovi_tool_not_found"

    # Let ffmpeg receive SIGPIPE if dovi_tool exits early.
    if ff.stdout:
        ff.stdout.close()

    try:
        out, _ = dv.communicate(timeout=DOVI_INFO_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        dv.kill()
        ff.kill()
        dv.wait()
        ff.wait()
        return None, "dovi_tool_timeout"
    finally:
        ff.wait()

    text = (out or b"").decode("utf-8", errors="replace").lower()
    if "fel" in text or "full enhancement" in text:
        return "FEL", ""
    if "mel" in text or "minimum enhancement" in text or "minimal enhancement" in text:
        return "MEL", ""
    return None, "el_type_undetermined"


# --------------------------------------------------------------------------- #
# Classification
# --------------------------------------------------------------------------- #
@dataclass
class FileResult:
    library: str
    relative_path: str
    classification: str
    dv_profile: Optional[int]
    dv_compatibility_id: Optional[int]
    bl_present: Optional[int]
    el_present: Optional[int]
    rpu_present: Optional[int]
    dv_level: Optional[int]
    el_type: str  # FEL / MEL / n/a
    notes: str = ""


def audit_file(path: Path, library: str, root: Path) -> FileResult:
    rel = _relative_to(path, root)

    probe = run_ffprobe(path)
    if probe is None:
        return FileResult(
            library, rel, CLASS_DV_UNKNOWN,
            None, None, None, None, None, None, "n/a",
            notes="ffprobe_failed",
        )

    dovi = find_dovi_record(probe)
    if dovi is None:
        return FileResult(
            library, rel, CLASS_NO_DV,
            None, None, None, None, None, None, "n/a",
        )

    # DV present but not Profile 7 -> nothing for this tool to convert.
    if dovi.dv_profile != 7:
        classification = CLASS_DV_OK if dovi.dv_profile is not None else CLASS_DV_UNKNOWN
        notes = "" if dovi.dv_profile is not None else "dv_present_no_profile"
        return FileResult(
            library, rel, classification,
            dovi.dv_profile, dovi.dv_compatibility_id,
            dovi.bl_present, dovi.el_present, dovi.rpu_present, dovi.dv_level,
            "n/a", notes=notes,
        )

    # Profile 7: determine FEL vs MEL.
    el_type, note = probe_el_type(path)
    if el_type == "FEL":
        classification = CLASS_DV7_FEL
    elif el_type == "MEL":
        classification = CLASS_DV7_MEL
    else:
        classification = CLASS_DV_UNKNOWN

    return FileResult(
        library, rel, classification,
        dovi.dv_profile, dovi.dv_compatibility_id,
        dovi.bl_present, dovi.el_present, dovi.rpu_present, dovi.dv_level,
        el_type or "n/a", notes=note,
    )


def _relative_to(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #
CSV_HEADER = [
    "library", "relative_path", "classification", "dv_profile",
    "dv_compatibility_id", "bl_present", "el_present", "rpu_present",
    "dv_level", "el_type", "notes",
]


def write_csv(results: list[FileResult], dest: Path) -> None:
    with dest.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADER)
        for r in results:
            writer.writerow([
                r.library,
                r.relative_path,
                r.classification,
                _blank(r.dv_profile),
                _blank(r.dv_compatibility_id),
                _blank(r.bl_present),
                _blank(r.el_present),
                _blank(r.rpu_present),
                _blank(r.dv_level),
                r.el_type,
                r.notes,
            ])


def _blank(value) -> str:
    return "" if value is None else str(value)


def build_summary(results: list[FileResult], library_names: list[str]) -> str:
    per_lib: dict[str, Counter] = defaultdict(Counter)
    for r in results:
        per_lib[r.library][r.classification] += 1

    lines = ["PlexDoViTool — Stage 1 Summary", "=" * 40, ""]
    for lib in library_names:
        counts = per_lib.get(lib, Counter())
        total = sum(counts.values())
        lines.append(f"[{lib}]  total files: {total}")
        for cls in CLASSIFICATIONS:
            lines.append(f"  {cls:<12} {counts.get(cls, 0)}")
        lines.append("")

    grand = Counter()
    for c in per_lib.values():
        grand.update(c)
    lines.append(f"[ALL]   total files: {sum(grand.values())}")
    for cls in CLASSIFICATIONS:
        lines.append(f"  {cls:<12} {grand.get(cls, 0)}")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stage 1 read-only Dolby Vision Profile 7 audit",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.yaml"),
        help="Path to config.yaml (default: ./config.yaml)",
    )
    args = parser.parse_args()

    if not args.config.is_file():
        sys.exit(f"error: config file not found: {args.config}")

    config = load_config(args.config)

    extensions = {str(e).lower() for e in (config.get("scan_extensions") or [])}
    if not extensions:
        sys.exit("error: config has no scan_extensions")

    reports_dir = Path(config.get("reports_dir") or "/reports")
    reports_dir.mkdir(parents=True, exist_ok=True)

    library_roots = config.get("library_roots") or []
    library_names: list[str] = []
    results: list[FileResult] = []

    for entry in library_roots:
        name = str(entry.get("name") or "unnamed")
        root = Path(str(entry.get("path") or ""))
        library_names.append(name)

        if not root.is_dir():
            print(f"warning: library root not found, skipping: [{name}] {root}",
                  file=sys.stderr)
            continue

        print(f"scanning [{name}] {root} ...", file=sys.stderr)
        scanned = 0
        for video_path in walk_library(root, extensions):
            print(f"  probing: {video_path}", file=sys.stderr)
            results.append(audit_file(video_path, name, root))
            scanned += 1
        print(f"  scanned {scanned} file(s) in [{name}]", file=sys.stderr)

    csv_path = reports_dir / "worklist.csv"
    summary_path = reports_dir / "summary.txt"

    write_csv(results, csv_path)
    summary = build_summary(results, library_names)
    summary_path.write_text(summary, encoding="utf-8")

    print()
    print(summary)
    print(f"worklist: {csv_path}")
    print(f"summary:  {summary_path}")


if __name__ == "__main__":
    main()
