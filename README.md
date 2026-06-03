# PlexDoViTool — Stage 1 (Dolby Vision Profile 7 Audit)

A **read-only** audit tool that scans a Plex library for Dolby Vision files and
reports their DV configuration. Its purpose is to find **DV Profile 7** titles
(the dual-layer BL+EL format) and determine, for each, whether the enhancement
layer is **FEL** (Full) or **MEL** (Minimum) — the distinction that drives the
eventual lossless Profile 7 → Profile 8.1 conversion in Stage 2.

All the heavy lifting (`ffprobe`, `ffmpeg`, `dovi_tool`, `mkvtoolnix`) runs
inside a single Docker image built from this repo. Nothing is installed on the
host, and **the library is mounted read-only** — the tool never writes anything
into your media.

## What Stage 1 does

For every `.mkv` under each configured library root it:

1. Runs `ffprobe` and inspects each video stream for a Dolby Vision
   configuration record (`dv_profile`, compatibility id, BL/EL/RPU flags, level).
2. For Profile 7 files only, pipes the HEVC stream through
   `dovi_tool info -s` to read the enhancement-layer type (FEL vs MEL).
3. Classifies the file and writes a CSV worklist + a summary. It modifies
   nothing.

### Classifications

| Class         | Meaning                                                           |
|---------------|-------------------------------------------------------------------|
| `NO_DV`       | No Dolby Vision present — ignored by this tool.                   |
| `DV_OK`       | DV present but already Profile 5 or 8.x — no conversion needed.   |
| `DV7_FEL`     | DV Profile 7 with a **Full** Enhancement Layer.                  |
| `DV7_MEL`     | DV Profile 7 with a **Minimum** Enhancement Layer.              |
| `DV_UNKNOWN`  | DV present but details couldn't be determined (probe failed etc). |

## Build

```sh
docker build -t plex-dovi-tool .
```

The image pins `dovi_tool` to a specific release (currently **2.3.2**) so builds
are reproducible.

## Run

Mount your media **read-only** (`:ro`), a writable `reports` directory, and your
config:

```sh
docker run --rm \
  -v /mnt/user/Movies:/media/movies:ro \
  -v $(pwd)/reports:/reports \
  -v $(pwd)/config.yaml:/app/config.yaml:ro \
  plex-dovi-tool
```

The `:ro` flag on the media mount guarantees the container cannot alter your
library even in principle. Mount additional libraries by adding more `-v ...:ro`
mounts and matching `library_roots` entries in `config.yaml`.

## Configure

`config.yaml` keys:

- `library_roots` — list of `{ name, path }`, where `path` is the **in-container**
  mount point (e.g. `/media/movies`).
- `scan_extensions` — file extensions to scan (default `.mkv`).
- `reports_dir` — where `worklist.csv` and `summary.txt` are written
  (default `/reports`).

## Outputs

Written to `reports_dir` (i.e. your mounted `./reports`):

- **`worklist.csv`** — one row per file with columns:
  `library, relative_path, classification, dv_profile, dv_compatibility_id,
  bl_present, el_present, rpu_present, dv_level, el_type, notes`.
- **`summary.txt`** — per-library and grand-total counts of each classification
  (also printed to stdout).

## What's next

**Stage 2 is not yet built.** It will losslessly convert the `DV7_FEL` / `DV7_MEL`
files to Profile 8.1 (extract HEVC, convert the RPU with `dovi_tool`, reinject,
and remux to a **new** MKV in a separate output directory — never overwriting the
original). Stage 1 output should be reviewed against real files first.
