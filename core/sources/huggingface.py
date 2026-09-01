"""Hugging Face open-schematics source: row-by-row ingestion with SQLite dedup and strict cache purging."""

import argparse
import gc
import json
import os
import re
from core import config
from core import registry as R
from core.log import get_logger

logger = get_logger(__name__)

HF_COL_SCHEMATIC = "schematic"
HF_COL_IMAGE = "schematic_image"
HF_COL_EXTENSIONS = "extensions_used"
# pcb_files est volontairement exclue : 266 Go sur 317, aucun consommateur en aval.
HF_KEEP_COLUMNS = [
    HF_COL_SCHEMATIC, HF_COL_IMAGE, HF_COL_EXTENSIONS,
    "components_used", "schematic_json", "schematic_yaml", "name", "description",
]
_SCHEMATIC_EXTS = (".kicad_sch", ".sch", ".schdoc")


def _hf_safe_name(raw_name):
    return re.sub(r"[^A-Za-z0-9._-]+", "_", raw_name).strip("_") or "unnamed"


def _pick_schematic_ext(extensions_used):
    for ext in (extensions_used or []):
        if isinstance(ext, str) and ext.lower() in _SCHEMATIC_EXTS:
            return ext.lower()
    return ".kicad_sch"


def collect(conn, batch_size=64, limit=None, save_images=True) -> None:
    """Ingestion par projection de colonnes : pyarrow ne lit que les colonnes demandées.

    batch_size est un nombre de lignes par lot Arrow, plus un nombre de fichiers.
    """
    import pyarrow.parquet as pq
    from huggingface_hub import HfApi, HfFileSystem

    logger.info("Listing des parquets sur Hugging Face (%s)...", config.HF_DATASET_ID)
    api = HfApi()
    all_files = api.list_repo_files(repo_id=config.HF_DATASET_ID, repo_type="dataset")
    parquet_files = sorted(f for f in all_files if f.startswith("data/") and f.endswith(".parquet"))

    url = f"https://huggingface.co/datasets/{config.HF_DATASET_ID}"
    new_count = 0

    known_statuses, known_hashes = R.get_known_identifiers(conn)
    _resumable_statuses = (R.INGESTED, R.CLEANED, R.VECTORIZED, R.EMPTY, R.DUPLICATE)

    fs = HfFileSystem()
    racine = f"datasets/{config.HF_DATASET_ID}@{config.HF_REVISION}"

    for n_frag, pq_path in enumerate(parquet_files, 1):
        if limit is not None and new_count >= limit:
            break

        logger.info("Fragment %d/%d : %s", n_frag, len(parquet_files), pq_path)
        try:
            with fs.open(f"{racine}/{pq_path}", "rb") as fh:
                pf = pq.ParquetFile(fh)
                dispo = set(pf.schema_arrow.names)
                cols = [c for c in HF_KEEP_COLUMNS if c in dispo]
                if not save_images and HF_COL_IMAGE in cols:
                    cols.remove(HF_COL_IMAGE)

                for lot in pf.iter_batches(batch_size=batch_size, columns=cols):
                    if limit is not None and new_count >= limit:
                        break

                    for i, row in enumerate(lot.to_pylist()):
                        if limit is not None and new_count >= limit:
                            break

                        schematic = row.get(HF_COL_SCHEMATIC)
                        if not schematic or not str(schematic).strip():
                            continue

                        raw_name = row.get("name") or f"row_{i}"
                        safe_name = _hf_safe_name(raw_name)

                        c_hash = R.content_hash(schematic)
                        project_id = f"hf_{safe_name}_{c_hash[:8]}"

                        if known_statuses.get(project_id) in _resumable_statuses:
                            continue

                        existing = known_hashes.get(c_hash)
                        if existing is not None and existing != project_id:
                            R.upsert_project(conn, project_id, raw_name, "HuggingFace_OpenSchematics",
                                             url, content_hash=c_hash, status=R.DUPLICATE)
                            known_statuses[project_id] = R.DUPLICATE
                            continue

                        ext = _pick_schematic_ext(row.get(HF_COL_EXTENSIONS))
                        sch_filename = f"{project_id}_schema{ext}"
                        with open(os.path.join(config.USABLE_DIR, sch_filename), "w", encoding="utf-8") as f:
                            f.write(schematic)

                        files_registered = [(sch_filename, "schematic", ext)]

                        # Représentations kiutils : volumineuses, donc tier 3 et non SQLite.
                        for col, kind, suffixe in (("schematic_json", "schematic_json", ".json"),
                                                   ("schematic_yaml", "schematic_yaml", ".yaml")):
                            contenu = row.get(col)
                            if contenu and str(contenu).strip():
                                nom = f"{project_id}_schema{suffixe}"
                                with open(os.path.join(config.USABLE_DIR, nom), "w", encoding="utf-8") as f:
                                    f.write(contenu)
                                files_registered.append((nom, kind, suffixe))

                        if save_images and row.get(HF_COL_IMAGE):
                            img = row[HF_COL_IMAGE]
                            if isinstance(img, dict) and img.get("bytes"):
                                img_filename = f"{project_id}_schema.png"
                                with open(os.path.join(config.USABLE_DIR, img_filename), "wb") as f:
                                    f.write(img["bytes"])
                                files_registered.append((img_filename, "image", ".png"))

                        # Symboles KiCad, distincts de la table components (références Mouser).
                        composants = row.get("components_used")
                        composants_json = json.dumps(composants, ensure_ascii=False) if composants else None
                        description = (row.get("description") or "").strip() or None

                        R.upsert_project(conn, project_id, raw_name, "HuggingFace_OpenSchematics",
                                         url, content_hash=c_hash, status=R.INGESTED,
                                         description=description, components_used=composants_json)
                        R.add_files(conn, project_id, files_registered)
                        known_statuses[project_id] = R.INGESTED
                        known_hashes[c_hash] = project_id
                        new_count += 1
        except Exception as exc:
            logger.warning("Fragment %s ignoré : %s: %s", pq_path, type(exc).__name__, exc)
            continue
        finally:
            gc.collect()

    logger.info("Collecte Hugging Face terminée. %d projet(s) ingéré(s).", new_count)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest bshada/open-schematics into the registry.")
    parser.add_argument("--batch-size", type=int, default=64)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--no-images", action="store_true")
    args = parser.parse_args()

    R.init_db(config.DB_PATH)
    conn = R.connect(config.DB_PATH)
    collect(conn, batch_size=args.batch_size, limit=args.limit, save_images=not args.no_images)
