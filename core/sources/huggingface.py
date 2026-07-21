"""Hugging Face open-schematics source: batches parquet shards into the registry."""

import argparse
import os
import re

from core import config
from core import registry as R
from core.log import get_logger

logger = get_logger(__name__)

HF_COL_SCHEMATIC = "schematic"
HF_COL_IMAGE = "schematic_image" 
HF_COL_EXTENSIONS = "extensions_used" 
HF_KEEP_COLUMNS = [
    HF_COL_SCHEMATIC, HF_COL_IMAGE, HF_COL_EXTENSIONS,
    "components_used", "schematic_json", "schematic_yaml", "name",
]

_SCHEMATIC_EXTS = (".kicad_sch", ".sch", ".schdoc")


def _hf_safe_name(raw_name):
    """Filesystem-safe project name, stable between the dedup check and the writer."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", raw_name).strip("_") or "unnamed"


def _pick_schematic_ext(extensions_used):
    """Schematic text is a KiCad S-expression, so default to .kicad_sch."""
    for ext in (extensions_used or []):
        if isinstance(ext, str) and ext.lower() in _SCHEMATIC_EXTS:
            return ext.lower()
    return ".kicad_sch"


def _process_row_worker(args):
    i, row, save_images, usable_dir = args

    raw_name = row.get("name") or f"row_{i}"
    safe_name = _hf_safe_name(raw_name)

    schematic = row.get(HF_COL_SCHEMATIC)
    if not (schematic and str(schematic).strip()):
        return None

    content_hash = R.content_hash(schematic)

    project_id = f"hf_{safe_name}_{content_hash[:8]}"

    return {
        "project_id": project_id,
        "raw_name": raw_name,
        "safe_name": safe_name,
        "schematic": schematic,
        "content_hash": content_hash,
        "extension": _pick_schematic_ext(row.get(HF_COL_EXTENSIONS)),
        "image": row.get(HF_COL_IMAGE) if save_images else None,
    }


def _write_files(item, usable_dir):

    files = []
    sch_filename = f"{item['project_id']}_schema{item['extension']}"
    with open(os.path.join(usable_dir, sch_filename), "w", encoding="utf-8") as f:
        f.write(item["schematic"])
    files.append((sch_filename, "schematic", item["extension"]))

    img = item["image"]
    if isinstance(img, dict):
        img_bytes = img.get("bytes")
        img_ext = ".png"
        path = img.get("path")
        if path and os.path.splitext(path)[1]:
            img_ext = os.path.splitext(path)[1].lower()
        if img_bytes:
            image_filename = f"{item['project_id']}_schema{img_ext}"
            with open(os.path.join(usable_dir, image_filename), "wb") as f:
                f.write(img_bytes)
            files.append((image_filename, "image", img_ext))

    return files


def _ingest_item(conn, item, usable_dir, url) -> bool:
    """Register one row; returns True if it was ingested (new or re-run of
    itself), False if it's a genuine duplicate of a DIFFERENT project."""
    existing = R.hash_seen(conn, item["content_hash"])

    if existing is not None and existing != item["project_id"]:
        R.upsert_project(
            conn, item["project_id"], item["raw_name"],
            "HuggingFace_OpenSchematics", url,
            content_hash=item["content_hash"], status=R.DUPLICATE,
        )
        return False

    files = _write_files(item, usable_dir)
    R.upsert_project(
        conn, item["project_id"], item["raw_name"],
        "HuggingFace_OpenSchematics", url,
        content_hash=item["content_hash"], status=R.INGESTED,
    )
    R.add_files(conn, item["project_id"], files)
    return True


def collect(conn, batch_size=30, limit=None, save_images=True) -> None:
    """Ingest bshada/open-schematics parquet shards into the SQLite registry."""
    from datasets import load_dataset, Image as HFImage
    from huggingface_hub import HfApi

    logger.info("Listing Parquet files for %s...", config.HF_DATASET_ID)
    api = HfApi()
    all_files = api.list_repo_files(repo_id=config.HF_DATASET_ID, repo_type="dataset")
    parquet_files = sorted(f for f in all_files if f.startswith("data/") and f.endswith(".parquet"))

    cache_dir = os.path.join(config.RAW_DIR, "hf_cache")
    url = f"https://huggingface.co/datasets/{config.HF_DATASET_ID}"
    new_count = 0

    for idx in range(0, len(parquet_files), batch_size):
        if limit is not None and new_count >= limit:
            break

        batch_files = parquet_files[idx: idx + batch_size]
        logger.info("Loading batch of %d parquet file(s)...", len(batch_files))

        dataset = load_dataset(
            config.HF_DATASET_ID,
            data_files=batch_files,
            split="train",
            revision=config.HF_REVISION,
            cache_dir=cache_dir,
        )

        keep = [c for c in HF_KEEP_COLUMNS if c in dataset.column_names]
        dataset = dataset.select_columns(keep)

        if HF_COL_IMAGE in dataset.column_names:
            dataset = dataset.cast_column(HF_COL_IMAGE, HFImage(decode=False))

        dataset = dataset.filter(
            lambda schematic: bool(schematic and str(schematic).strip()),
            input_columns=[HF_COL_SCHEMATIC],
        )

        for i, row in enumerate(dataset):
            if limit is not None and new_count >= limit:
                break

            item = _process_row_worker((i, row, save_images, config.USABLE_DIR))
            if item is None:
                continue

            if _ingest_item(conn, item, config.USABLE_DIR, url):
                new_count += 1

        dataset = None

    logger.info("Hugging Face collection finished: %d new project(s).", new_count)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest bshada/open-schematics into the registry.")
    parser.add_argument("--batch-size", type=int, default=30)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--no-images", action="store_true")
    args = parser.parse_args()

    R.init_db(config.DB_PATH)
    conn = R.connect(config.DB_PATH)
    collect(conn, batch_size=args.batch_size, limit=args.limit, save_images=not args.no_images)
