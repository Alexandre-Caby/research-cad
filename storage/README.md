# Multi-Tier Data Lake

This directory stores the system's data, organized into five strict logical tiers based on processing stage and maturity.

## Storage Tiers

- **`1_raw_data/` (Bronze Tier)**: Temporary landing zone for large binary files, such as fallback ZIP archives downloaded from GitHub. The contents of this folder are highly volatile and are automatically purged once CAD extraction has been validated.
- **`2_register_data/` (Silver Tier)**: The SQLite control plane (`cad_registry.db`) that tracks the global system state — the `projects`, `files`, and `components` tables, the status FSM, the content-hash dedup key, and the global Mouser cache for component specifications and datasheets.
  - **Critical rule**: This folder and its contents must not be deleted or modified manually. It is the single source of truth for deduplication and pipeline progress tracking.
- **`3_exploitable_data/` (Gold Tier)**: Normalized and isolated original CAD source files (`.kicad_sch`, `.sch`, `.brd`, etc.), their schematic preview images, and component datasheet PDFs (`datasheet_*.pdf`) downloaded by Mouser, all optimized for structural analysis and model training.
  - **Critical rule**: This folder is permanent and must never be purged. Unlike Tier 1, its contents cannot be regenerated without rerunning the full network extraction process, and it forms the reusable knowledge base the pipeline is designed to build.
- **`4_extracted_data/` (Platinum Tier)**: Component bill of materials files (CSV BOMs) extracted from Tier 3, structured and ready for semantic analysis.
- **`5_vector_data/` (Diamond Tier)**: The LanceDB data plane — one multimodal table holding text and image vectors alongside project metadata and components. It serves both semantic search and a versioned training-dataset export to Parquet / HuggingFace `datasets`.
