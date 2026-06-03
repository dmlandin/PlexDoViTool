#!/usr/bin/env python3
"""PlexDoViTool — Stage 2: lossless Dolby Vision Profile 7 -> Profile 8.1.

For each input MKV this:
  0. Confirms the file is still DV Profile 7 (ffprobe).
  1. Re-determines FEL vs MEL (same logic as Stage 1's audit.py).
  2. Detects whether an audio default-flag reflag is needed (a default
     TrueHD track with an English AC-3/E-AC-3 sibling).
  3. Plans: extract HEVC -> extract RPU -> convert RPU to 8.1 -> inject RPU
     -> remux into a NEW "<name>.dovi8.mkv" alongside the original.
  4. Executes (or, with --dry-run, only prints the planned commands).
  5. Verifies the output reports dv_profile=8 and is within +/-5% of the
     original size.

The ORIGINAL file is never modified, moved, or deleted. All intermediates
live in a per-file temp dir that is auto-cleaned even on error. A failure on
one file is logged and skipped; the batch continues.

This script shares the Stage 1 Docker image (ffmpeg, mkvtoolnix, dovi_tool).
It is NOT the image entrypoint (that is audit.py); run it by overriding the
entrypoint, e.g. `docker run --entrypoint python ... plex-dovi-tool
/app/convert.py --file ...`.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import shlex
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

# Reuse Stage 1's identical ffprobe / DV-record / FEL-MEL logic. Importing
# (rather than duplicating) guarantees the classification matches the audit and
# does not modify audit.py. /app is on sys.path because this script lives there.
import audit

DEFAULT_CONFIG = "/app/config.yaml"
LOG_FILENAME = "convert.log"
SIZE_TOLERANCE = 0.05  # +/-5% output-vs-original size sanity check

DV7_CLASSES = {"DV7_FEL", "DV7_MEL"}

# Heavy steps walk the whole (tens-of-GB) stream; quick steps are metadata only.
HEAVY_TIMEOUT_SEC = 7200
QUICK_TIMEOUT_SEC = 120

log = logging.getLogger("stage2")


# --------------------------------------------------------------------------- #
# Command execution helpers
# --------------------------------------------------------------------------- #
def _fmt(cmd: list[str]) -> str:
    return " ".join(shlex.quote(str(c)) for c in cmd)


def run_step(cmd: list[str], *, dry_run: bool, timeout: int) -> bool:
    """Run one external command. Logs it, never raises. Returns success.

    Per the error-handling rules this uses check=False and inspects the
    return code manually, logging stderr on failure.
    """
    log.info("    $ %s", _fmt(cmd))
    if dry_run:
        return True
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        log.error("    command not found: %s", cmd[0])
        return False
    except subprocess.TimeoutExpired:
        log.error("    timed out after %ss: %s", timeout, cmd[0])
        return False
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        log.error("    failed (rc=%s): %s", result.returncode, cmd[0])
        if stderr:
            log.error("    stderr: %s", stderr)
        return False
    return True


# --------------------------------------------------------------------------- #
# Probing
# --------------------------------------------------------------------------- #
def mkvmerge_identify(path: Path) -> Optional[dict]:
    """Return `mkvmerge -J` JSON for authoritative track IDs, or None."""
    try:
        r = subprocess.run(
            ["mkvmerge", "-J", str(path)],
            capture_output=True, timeout=QUICK_TIMEOUT_SEC, check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    # mkvmerge: 0 = ok, 1 = warnings (still valid JSON), 2 = error.
    if r.returncode >= 2:
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def _codec_id(track: dict) -> str:
    return str((track.get("properties") or {}).get("codec_id") or "").upper()


def _language(track: dict) -> str:
    return str((track.get("properties") or {}).get("language") or "").lower()


def _is_default(track: dict) -> bool:
    return bool((track.get("properties") or {}).get("default_track"))


@dataclass
class ReflagPlan:
    needed: bool
    truehd_id: Optional[int] = None
    ac3_id: Optional[int] = None


def detect_audio_reflag(ident: dict) -> ReflagPlan:
    """Reflag when a default TrueHD track has an English AC-3/E-AC-3 sibling.

    Track IDs come straight from `mkvmerge -J` because mkvmerge's
    --default-track-flag addresses tracks by those (global) IDs.
    """
    audio = [t for t in ident.get("tracks", []) if t.get("type") == "audio"]
    truehd_default = next(
        (t for t in audio if _codec_id(t).startswith("A_TRUEHD") and _is_default(t)),
        None,
    )
    eng_ac3 = next(
        (t for t in audio
         if _codec_id(t) in {"A_AC3", "A_EAC3"} and _language(t) == "eng"),
        None,
    )
    if truehd_default and eng_ac3:
        return ReflagPlan(True, truehd_default.get("id"), eng_ac3.get("id"))
    return ReflagPlan(False)


# --------------------------------------------------------------------------- #
# Per-file processing
# --------------------------------------------------------------------------- #
@dataclass
class Result:
    path: str
    status: str          # success | failed | skipped | dry-run
    reason: str = ""
    el_type: str = ""
    reflag: bool = False


def _output_path(input_path: Path) -> Path:
    # movie.mkv -> movie.dovi8.mkv (built manually; with_suffix dislikes the
    # double extension).
    return input_path.with_name(input_path.stem + ".dovi8" + input_path.suffix)


def process_file(input_path: Path, dry_run: bool) -> Result:
    label = str(input_path)
    log.info("------------------------------------------------------------")
    log.info("file: %s", label)

    if not input_path.is_file():
        log.warning("  not found, skipping")
        return Result(label, "skipped", "file_not_found")

    # Step 0: confirm DV Profile 7.
    probe = audit.run_ffprobe(input_path)
    if probe is None:
        log.warning("  ffprobe failed, skipping")
        return Result(label, "skipped", "ffprobe_failed")
    record = audit.find_dovi_record(probe)
    if record is None or record.dv_profile != 7:
        prof = None if record is None else record.dv_profile
        log.warning("  not DV Profile 7 (profile=%s), skipping", prof)
        return Result(label, "skipped", "not_profile_7")

    # Step 1: FEL vs MEL (re-probed every time so --file works without a CSV).
    el_type, note = audit.probe_el_type(input_path)
    if el_type not in {"FEL", "MEL"}:
        log.warning("  could not determine FEL/MEL (%s), skipping", note)
        return Result(label, "skipped", "el_type_undetermined")
    log.info("  enhancement layer: %s", el_type)

    # Step 2: track identification + audio reflag decision.
    ident = mkvmerge_identify(input_path)
    if ident is None:
        log.warning("  mkvmerge identify failed, skipping")
        return Result(label, "skipped", "identify_failed", el_type)
    video_tracks = [t for t in ident.get("tracks", []) if t.get("type") == "video"]
    if not video_tracks:
        log.warning("  no video track found, skipping")
        return Result(label, "skipped", "no_video_track", el_type)
    video_id = video_tracks[0].get("id")
    reflag = detect_audio_reflag(ident)
    if reflag.needed:
        log.info("  audio reflag: TrueHD #%s -> off, AC-3(eng) #%s -> default",
                 reflag.truehd_id, reflag.ac3_id)
    else:
        log.info("  audio reflag: not needed (source flags preserved)")

    output_path = _output_path(input_path)
    if output_path.exists() and not dry_run:
        log.warning("  output already exists, skipping: %s", output_path)
        return Result(label, "skipped", "output_exists", el_type, reflag.needed)

    # Step 3 + 4: plan and run inside an auto-cleaned temp dir.
    with tempfile.TemporaryDirectory(prefix="dovi8_") as tmp:
        tmpd = Path(tmp)
        hevc = tmpd / "video.hevc"
        converted_hevc = tmpd / "video_p81.hevc"

        # dovi_tool convert does the RPU work end-to-end on the HEVC stream.
        # FEL sources add --discard to drop the (real) enhancement layer; MEL
        # sources convert without it.
        convert_cmd = ["dovi_tool", "convert"]
        if el_type == "FEL":
            convert_cmd.append("--discard")
        convert_cmd += ["-i", str(hevc), "-o", str(converted_hevc)]

        steps: list[tuple[str, list[str], int]] = [
            ("extract HEVC",
             ["mkvextract", str(input_path), "tracks", f"{video_id}:{hevc}"],
             HEAVY_TIMEOUT_SEC),
            ("convert HEVC -> 8.1",
             convert_cmd,
             HEAVY_TIMEOUT_SEC),
            ("remux", _build_remux_cmd(input_path, converted_hevc, output_path, reflag),
             HEAVY_TIMEOUT_SEC),
        ]

        if dry_run:
            log.info("  [dry-run] planned commands:")
            for name, cmd, _ in steps:
                log.info("  - %s", name)
                log.info("    $ %s", _fmt(cmd))
            return Result(label, "dry-run", "", el_type, reflag.needed)

        for name, cmd, timeout in steps:
            log.info("  %s ...", name)
            if not run_step(cmd, dry_run=False, timeout=timeout):
                _cleanup_partial(output_path)
                return Result(label, "failed", f"{name}_failed", el_type, reflag.needed)

    # Step 5: verify (real run only).
    ok, reason = _verify_output(input_path, output_path)
    if not ok:
        # Leave the output in place for inspection; just flag loudly.
        log.error("  VERIFICATION FAILED (%s): %s", reason, output_path)
        return Result(label, "failed", f"verify_{reason}", el_type, reflag.needed)

    log.info("  OK -> %s", output_path)
    return Result(label, "success", "", el_type, reflag.needed)


def _build_remux_cmd(input_path: Path, converted_hevc: Path, output_path: Path,
                     reflag: ReflagPlan) -> list[str]:
    cmd = ["mkvmerge", "-o", str(output_path)]
    if reflag.needed:
        # Flags apply to the next input file (the original MKV).
        cmd += [
            "--default-track-flag", f"{reflag.truehd_id}:0",
            "--default-track-flag", f"{reflag.ac3_id}:1",
        ]
    # Everything except video from the original; only video from the new HEVC.
    cmd += ["--no-video", str(input_path)]
    cmd += [
        "--no-audio", "--no-subtitles", "--no-chapters",
        "--no-attachments", "--no-track-tags", "--no-global-tags",
        str(converted_hevc),
    ]
    return cmd


def _cleanup_partial(output_path: Path) -> None:
    try:
        if output_path.exists():
            output_path.unlink()
            log.info("  removed partial output: %s", output_path)
    except OSError as e:
        log.warning("  could not remove partial output %s: %s", output_path, e)


def _verify_output(input_path: Path, output_path: Path) -> tuple[bool, str]:
    if not output_path.is_file():
        return False, "missing"
    probe = audit.run_ffprobe(output_path)
    if probe is None:
        return False, "ffprobe_failed"
    record = audit.find_dovi_record(probe)
    if record is None or record.dv_profile != 8:
        prof = None if record is None else record.dv_profile
        log.error("  expected dv_profile=8, got %s", prof)
        return False, "profile_not_8"
    try:
        orig = input_path.stat().st_size
        new = output_path.stat().st_size
    except OSError:
        return False, "stat_failed"
    if orig > 0 and abs(new - orig) / orig > SIZE_TOLERANCE:
        log.error("  size drift %.1f%% (orig=%d new=%d)",
                  100 * (new - orig) / orig, orig, new)
        return False, "size_drift"
    return True, ""


# --------------------------------------------------------------------------- #
# Input gathering
# --------------------------------------------------------------------------- #
def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def gather_from_worklist(csv_path: Path, config: dict) -> list[Path]:
    roots = {
        str(r.get("name")): Path(str(r.get("path")))
        for r in (config.get("library_roots") or [])
    }
    inputs: list[Path] = []
    skipped = 0
    skip_reasons: dict[str, int] = {}

    def note_skip(reason: str) -> None:
        nonlocal skipped
        skipped += 1
        skip_reasons[reason] = skip_reasons.get(reason, 0) + 1

    with csv_path.open("r", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            classification = (row.get("classification") or "").strip()
            if classification not in DV7_CLASSES:
                note_skip(classification or "blank")
                continue
            library = (row.get("library") or "").strip()
            rel = (row.get("relative_path") or "").strip()
            root = roots.get(library)
            if root is None:
                log.warning("worklist row has unknown library %r (not in config); skipping",
                            library)
                note_skip("unknown_library")
                continue
            inputs.append(root / rel)

    log.info("worklist: %d file(s) to process, %d skipped", len(inputs), skipped)
    for reason, count in sorted(skip_reasons.items()):
        log.info("  skipped %s: %d", reason, count)
    return inputs


# --------------------------------------------------------------------------- #
# Logging + main
# --------------------------------------------------------------------------- #
def setup_logging(reports_dir: Path) -> None:
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s",
                            datefmt="%Y-%m-%dT%H:%M:%S")
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(fmt)
    log.addHandler(stream)
    try:
        reports_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(reports_dir / LOG_FILENAME, encoding="utf-8")
        file_handler.setFormatter(fmt)
        log.addHandler(file_handler)
    except OSError as e:
        log.warning("could not open log file in %s: %s", reports_dir, e)


def print_summary(results: list[Result], dry_run: bool) -> None:
    log.info("============================================================")
    log.info("Stage 2 summary%s", " (dry-run)" if dry_run else "")
    succeeded = [r for r in results if r.status == "success"]
    failed = [r for r in results if r.status == "failed"]
    skipped = [r for r in results if r.status == "skipped"]
    planned = [r for r in results if r.status == "dry-run"]
    log.info("  total:     %d", len(results))
    if planned:
        log.info("  planned:   %d", len(planned))
    log.info("  succeeded: %d", len(succeeded))
    log.info("  failed:    %d", len(failed))
    log.info("  skipped:   %d", len(skipped))
    for r in results:
        extra = f" [{r.el_type}]" if r.el_type else ""
        reason = f" ({r.reason})" if r.reason else ""
        log.info("  %-9s%s %s%s", r.status, extra, r.path, reason)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Stage 2 lossless DV Profile 7 -> 8.1 conversion",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", type=Path, help="single .mkv (in-container path)")
    group.add_argument("--worklist", type=Path, help="Stage 1 worklist CSV")
    parser.add_argument("--config", type=Path, default=Path(DEFAULT_CONFIG),
                        help=f"config.yaml (default: {DEFAULT_CONFIG})")
    parser.add_argument("--dry-run", action="store_true",
                        help="print planned commands but run nothing")
    args = parser.parse_args()

    config = {}
    if args.config.is_file():
        config = load_config(args.config)
    reports_dir = Path(config.get("reports_dir") or "/reports")
    setup_logging(reports_dir)

    if args.dry_run:
        log.info("DRY RUN — no files will be written")

    if args.file is not None:
        inputs = [args.file]
    else:
        if not args.worklist.is_file():
            sys.exit(f"error: worklist not found: {args.worklist}")
        inputs = gather_from_worklist(args.worklist, config)

    results: list[Result] = []
    for input_path in inputs:
        try:
            results.append(process_file(input_path, args.dry_run))
        except Exception as e:  # never let one file abort the batch
            log.exception("  unexpected error, skipping: %s", e)
            results.append(Result(str(input_path), "failed", "unexpected_error"))

    print_summary(results, args.dry_run)


if __name__ == "__main__":
    main()
