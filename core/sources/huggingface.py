"""Hugging Face open-schematics source: row-by-row ingestion with SQLite dedup and strict cache purging."""

import argparse
import gc
import os
import re
import shutil
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
    return re.sub(r"[^A-Za-z0-9._-]+", "_", raw_name).strip("_") or "unnamed"


def _pick_schematic_ext(extensions_used):
    for ext in (extensions_used or []):
        if isinstance(ext, str) and ext.lower() in _SCHEMATIC_EXTS:
            return ext.lower()
    return ".kicad_sch"


def _purge_directory(path):
    """Supprime un dossier et son contenu pour libérer immédiatement le disque."""
    if os.path.exists(path):
        try:
            shutil.rmtree(path)
        except Exception as exc:
            logger.warning("Erreur lors de la purge de %s: %s", path, exc)


def collect(conn, batch_size=30, limit=None, save_images=True) -> None:
    from datasets import load_dataset, Image as HFImage
    from huggingface_hub import HfApi

    logger.info("Listing des parquets sur Hugging Face (%s)...", config.HF_DATASET_ID)
    api = HfApi()
    all_files = api.list_repo_files(repo_id=config.HF_DATASET_ID, repo_type="dataset")
    parquet_files = sorted(f for f in all_files if f.startswith("data/") and f.endswith(".parquet"))

    url = f"https://huggingface.co/datasets/{config.HF_DATASET_ID}"
    new_count = 0

    known_statuses, known_hashes = R.get_known_identifiers(conn)
    _resumable_statuses = (R.INGESTED, R.CLEANED, R.VECTORIZED, R.EMPTY, R.DUPLICATE)

    for idx in range(0, len(parquet_files), batch_size):
        if limit is not None and new_count >= limit:
            break

        batch_files = parquet_files[idx : idx + batch_size]

        batch_cache_dir = os.path.join(config.RAW_DIR, f"hf_cache_batch_{idx}")

        logger.info("Chargement du batch Parquet %d/%d...", (idx // batch_size) + 1, (len(parquet_files) + batch_size - 1) // batch_size)

        try:
            dataset = load_dataset(
                config.HF_DATASET_ID,
                data_files=batch_files,
                split="train",
                revision=config.HF_REVISION,
                cache_dir=batch_cache_dir,
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

                schematic = row.get(HF_COL_SCHEMATIC)
                if not schematic:
                    continue

                raw_name = row.get("name") or f"row_{i}"
                safe_name = _hf_safe_name(raw_name)

                c_hash = R.content_hash(schematic)
                project_id = f"hf_{safe_name}_{c_hash[:8]}"

                if known_statuses.get(project_id) in _resumable_statuses:
                    continue

                existing = known_hashes.get(c_hash)
                if existing is not None and existing != project_id:
                    R.upsert_project(conn, project_id, raw_name, "HuggingFace_OpenSchematics", url, content_hash=c_hash, status=R.DUPLICATE)
                    known_statuses[project_id] = R.DUPLICATE
                    continue

                ext = _pick_schematic_ext(row.get(HF_COL_EXTENSIONS))
                sch_filename = f"{project_id}_schema{ext}"
                sch_path = os.path.join(config.USABLE_DIR, sch_filename)

                with open(sch_path, "w", encoding="utf-8") as f:
                    f.write(schematic)

                files_registered = [(sch_filename, "schematic", ext)]

                if save_images and row.get(HF_COL_IMAGE):
                    img = row[HF_COL_IMAGE]
                    if isinstance(img, dict) and img.get("bytes"):
                        img_filename = f"{project_id}_schema.png"
                        with open(os.path.join(config.USABLE_DIR, img_filename), "wb") as f:
                            f.write(img["bytes"])
                        files_registered.append((img_filename, "image", ".png"))

                R.upsert_project(conn, project_id, raw_name, "HuggingFace_OpenSchematics", url, content_hash=c_hash, status=R.INGESTED)
                R.add_files(conn, project_id, files_registered)
                known_statuses[project_id] = R.INGESTED
                known_hashes[c_hash] = project_id
                new_count += 1

        finally:
            del dataset
            gc.collect()
            _purge_directory(batch_cache_dir)
            logger.info("Cache disque du batch purgé.")

    logger.info("Collecte Hugging Face terminée. %d projet(s) ingéré(s).", new_count)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest bshada/open-schematics into the registry.")
    parser.add_argument("--batch-size", type=int, default=30)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--no-images", action="store_true")
    args = parser.parse_args()

    R.init_db(config.DB_PATH)
    conn = R.connect(config.DB_PATH)
    collect(conn, batch_size=args.batch_size, limit=args.limit, save_images=not args.no_images)