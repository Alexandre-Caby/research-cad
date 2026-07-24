# Pipeline Core

This package holds the ingestion, processing, and worker modules for the CAD
processing workflow. Each stage is one module with a single job and a `run_x()`
entry point, invoked as `python -m core.<stage>`. Operational state lives in a
SQLite control plane; the produced vectors and metadata live in a LanceDB data
plane.

## Layout

- **`config.py`** — resolved storage paths, model names, the pinned Hugging Face
  dataset revision, batch sizes, and device selection. All paths derive from
  `__file__`.
- **`registry.py`** — the SQLite control plane. `projects`, `files`, and
  `components` tables, the status FSM (`ingested`, `cleaned`, `vectorized`,
  `exported`, `empty`, `duplicate`, `manual_review`, `error`), atomic upserts,
  the permanent cross-project components cache, `get_cached_components()`,
  `upsert_component()`, and SHA-256 content dedup shared by every source.
- **`log.py`** — logging setup (console + `storage/pipeline.log`).
- **`sources/`** — one collector per source, each writing raw files to Tier 3 and
  registering projects.
  - `github.py`: scans an org for CAD files, fetches matching blobs in parallel,
    falls back to a ZIP download for truncated repo trees, and skips projects
    that are already registered in the database immediately.
  - `huggingface.py`: streams `bshada/open-schematics` one Parquet batch at a time
    at a pinned revision, writing schematic text and the raw preview image, and
    skips already registered projects immediately.
  - `mouser.py`: hybrid Mouser V1/V2 search, manufacturer and category attribute
    caching, component enrichment from cleaned BOMs, and datasheet PDF downloads.
  - `oshwlab.py`: reads `oshwlab_urls.txt` and records each project as
    `manual_review`, while also skipping already registered projects immediately.
- **`parsing/`** — the single source of truth for parsing, shared by cleaning and
  vectorization.
  - `kicad.py`: `.kicad_sch` component extraction and `.kicad_sch`/`.kicad_pcb`
    structural summaries.
  - `eagle.py`: `.sch` (parts) and `.brd` (elements) component extraction and
    summaries.
- **`clean.py`** — parses every `ingested` project's schematic/board files into a
  BOM CSV in Tier 4, across processes, with immediate SQLite checkpoints per
  project via `as_completed()`. Sets `cleaned` or `empty`.
- **`encoders.py`** — the text encoder (`bge-m3`) and the two-tower image encoder
  (`SigLIP2`, image and text in one space for cross-modal search). Models load
  lazily and select CUDA when available.
- **`vectorize.py`** — builds an embedding text from the BOM, structural
  summaries, and Mouser technical data, then mini-batch encodes text and
  schematic image before upserting one row per project into LanceDB. Sets
  `vectorized`.
- **`store.py`** — LanceDB access: the multimodal table schema, idempotent upsert
  by `project_id`, vector search, and Parquet export.
- **`report.py`** — QC statistics over the registry (counts per source, status,
  and file kind).
- **`purge.py`** — frees disk space by clearing the temporary Tier 1 staging area
  only.

**Rule**: all modules resolve paths from `__file__` (via `config.py`) so they stay
independent of the deployment environment.
