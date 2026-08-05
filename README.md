# CAD Schematics Dataset Pipeline

This project builds a dataset from real-world electronic schematics and PCB layouts collected from open hardware repositories and community design-sharing sites. The pipeline is designed to keep growing as new sources are added.

The scripts in this repository are a working base. They are expected to evolve as the pipeline matures.

## Directory Structure

```text
research-cad/
├── README                  # Project documentation
├── run_pipeline.sh         # Orchestrates ingest -> clean -> vectorize -> report
├── requirements.txt        # Python dependencies
├── oshwlab_urls.txt        # Optional: one OSHWLab project URL per line
├── core/                   # Pipeline package (one module per stage)
│   ├── config.py           # Paths, model names, pinned HF revision
│   ├── registry.py         # SQLite control plane (status FSM, dedup)
│   ├── log.py              # Logging setup
│   ├── sources/            # github.py, huggingface.py, mouser.py, oshwlab.py
│   ├── parsing/            # kicad.py, eagle.py (schematic/PCB parsers)
│   ├── clean.py            # BOM extraction
│   ├── encoders.py         # Text (bge-m3) and image (SigLIP2) encoders
│   ├── vectorize.py        # Embeddings -> LanceDB
│   ├── store.py            # LanceDB access + parquet export
│   ├── report.py           # QC statistics
│   └── purge.py            # Temporary storage cleanup
└── storage/                # Multi-tier data lake
    ├── 1_raw_data/         # Temporary landing zone
    ├── 2_register_data/    # SQLite registry (cad_registry.db)
    ├── 3_exploitable_data/ # Raw CAD source files + schematic previews
    ├── 4_extracted_data/   # Generated BOM CSV files
    └── 5_vector_data/      # LanceDB table (vectors + metadata)
```

## Sources and Feasibility

Not every source is equally automatable, so each one is handled differently.

| Source | Automation level | How it is handled |
|---|---|---|
| GitHub / Adafruit (`org:adafruit pcb`) | Fully automatic | `core.sources.github` |
| GitHub / SparkFun (`org:sparkfun hardware`) | Fully automatic | `core.sources.github` with `--path-filter hardware` |
| Hugging Face `bshada/open-schematics` | Fully automatic | `core.sources.huggingface`, batched by Parquet shard at a pinned revision |
| Mouser API (V1/V2) | Fully automatic | `core.sources.mouser`, hybrid part/keyword search, technical specs caching, and datasheet PDF fetch |
| OSHWLab (`oshwlab.com`) | Partially automatic | `core.sources.oshwlab` reads `oshwlab_urls.txt`; project discovery is still manual |
| CircuitMaker (`circuitmaker.com/Projects`) | Not implemented | See explanation below |

### Why CircuitMaker is not implemented

CircuitMaker project files cannot be downloaded directly from the browser. The real design files are stored in proprietary Altium formats and require the desktop application and a logged-in account to export them manually. There is no reliable public web endpoint to automate this source, so it is intentionally left out of the pipeline.

### Why OSHWLab is only partially automated

OSHWLab does not expose a public search or listing API, so project discovery still has to be done manually through the site UI. Once you have URLs, `core.sources.oshwlab` can process them from `oshwlab_urls.txt`. OSHWLab pages carry EasyEDA state that no downstream parser handles, so every OSHWLab project is recorded as `manual_review` and skipped by cleaning and vectorization rather than failing silently.

## Naming and Directory Conventions

- The repository follows the `research-xxx` naming pattern used by the other pipeline.
- The registry lives at `storage/2_register_data/cad_registry.db` (SQLite) and is shared by all sources.
- The storage hierarchy is fixed for consistency, even when some tiers are used less heavily than others.

### Why `1_raw_data` stays mostly empty

Most sources already provide small, directly usable CAD files.

- GitHub ingestion fetches only the matching schematic or PCB files instead of downloading entire repositories. If a repository tree is too large and GitHub truncates the listing, the script falls back to a ZIP download staged in `1_raw_data`, extracts the CAD files, and deletes the archive.
- Hugging Face shards are downloaded one Parquet batch at a time, written into `3_exploitable_data`, and their cache is cleared between batches, so there is no large persistent download.

`1_raw_data` remains in the layout for architectural consistency and for any future source that only offers bulk archives.

## Data Lifecycle

The SQLite registry tracks each project through an explicit status machine. A project is
deduplicated across sources by the SHA-256 hash of its schematic text.

```
ingested -> cleaned -> mouser_enriched -> vectorized -> exported
    └-> empty | duplicate | manual_review | error   (terminal)
```

1. **Ingestion** (`core.sources.*`): a source finds a project, skips projects that are already registered in the database, writes its schematic/PCB files and schematic preview to `3_exploitable_data`, and records the project as `ingested` (or `empty` if no CAD file was found, `duplicate` if its content was already ingested, `manual_review` for OSHWLab).
2. **Cleaning** (`core.clean`): each `ingested` project is parsed with the matching extractor and a BOM CSV is written to `4_extracted_data`. SQLite checkpoints are committed in real time, project by project, via `as_completed()`. Status becomes `cleaned`, or `empty` if no components were found.
3. **Mouser enrichment** (`core.sources.mouser`): the cleaned BOMs in Tier 4 are queried against Mouser API V1/V2, the SQLite `components` table is enriched with cached manufacturer and category attributes, and datasheet PDFs are downloaded into Tier 3.
4. **Vectorization** (`core.vectorize`): Tier 3 structural features, Mouser technical specifications, and the Tier 4 BOM are combined into an embedding text, then text and schematic image are encoded in mini-batches and upserted into the LanceDB table in `5_vector_data`. Status becomes `vectorized`. The table is exportable to Parquet / HuggingFace `datasets` for model training.
5. **Reporting** (`core.report`): prints counts per source, per status, and per file kind.
6. **Purge** (`core.purge`): removes only the temporary `1_raw_data` staging area. `3_exploitable_data`, `4_extracted_data`, and `5_vector_data` are retained as reusable tiers.

## How to Run

### 1. Setup

```bash
pip install -r requirements.txt
```

Stages run as package modules, so invoke them from the project root with the
environment that holds the dependencies active.

Before running ingestion, create a GitHub personal access token and export it as `GITHUB_TOKEN`, otherwise GitHub requests will quickly hit the unauthenticated rate limit.

```bash
export GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx
```

If you want Mouser enrichment, create a Mouser API key and export it as `MOUSER_API_KEY`.

```bash
export MOUSER_API_KEY=your_mouser_api_key
```

If you want to use OSHWLab, create `oshwlab_urls.txt` in the project root with one project URL per line.

```text
https://oshwlab.com/someuser/some-project
https://oshwlab.com/anotheruser/another-project
```

### 2. Ingestion

```bash
python -m core.sources.github --org adafruit --search pcb
python -m core.sources.github --org sparkfun --search hardware --path-filter hardware
python -m core.sources.huggingface          # add --limit N for a test run
python -m core.sources.oshwlab              # reads oshwlab_urls.txt if present
```

### 3. Cleaning and BOM Extraction

```bash
python -m core.clean
```

This parses every `ingested` project and writes one BOM CSV per source file to `4_extracted_data`. Run Mouser enrichment after cleaning so the BOM cache is available before vectorization.

### 4. Mouser Enrichment

```bash
python -m core.sources.mouser   # component enrichment and datasheet downloads
```

This enriches the cleaned BOMs from Tier 4, updates the cached component table, and downloads datasheet PDFs into Tier 3.

### 5. Vectorization

```bash
python -m core.vectorize
```

This combines Tier 3 structural signals, Mouser technical data, and Tier 4 BOM data, encodes text and image in mini-batches, and upserts them into the LanceDB table in `5_vector_data`. Export the table with `python -m core.store --export <path>`.

### 6. Reporting and Cleanup

```bash
python -m core.report
python -m core.purge --dry-run
python -m core.purge --yes
```

### All in One

```bash
./run_pipeline.sh                # optional: --limit N, --purge, --yes
```
