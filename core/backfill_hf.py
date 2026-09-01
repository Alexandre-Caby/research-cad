"""Rattrapage des colonnes Hugging Face non ingérées sur les projets déjà enregistrés.

Lit les fragments Parquet en ne projetant que les colonnes textuelles, sans le schéma ni les
images. La jointure se fait sur projects.name (owner/repo) : sans le schéma, le content_hash
qui identifie un projet ne peut pas être recalculé.

description est propre au dépôt, donc appliquée à tous ses projets. components_used,
schematic_json et schematic_yaml sont propres à CHAQUE schéma : ils ne sont écrits que sur les
dépôts ne portant qu'un seul projet, sinon l'attribution serait arbitraire.
"""

import argparse
import collections
import gc
import json
import os
from core import config
from core import registry as R
from core.log import get_logger

logger = get_logger(__name__)

COLONNES = ["name", "description", "components_used", "schematic_json", "schematic_yaml"]
SOURCE = "HuggingFace_OpenSchematics"


def _index_projets(conn):
    """{name: [project_id, ...]} pour les projets déjà ingérés depuis Hugging Face."""
    index = collections.defaultdict(list)
    for row in conn.execute(
        "SELECT project_id, name FROM projects WHERE source=? AND name IS NOT NULL", (SOURCE,)
    ):
        index[row["name"]].append(row["project_id"])
    return index


def _fichiers_connus(conn):
    """{(project_id, kind)} déjà enregistrés, pour ne pas dupliquer les lignes de files."""
    return {(r["project_id"], r["kind"]) for r in conn.execute(
        "SELECT DISTINCT project_id, kind FROM files WHERE kind IN ('schematic_json','schematic_yaml')"
    )}


def _ecrire_structure(project_id, row, connus):
    """Écrit les représentations kiutils absentes, rend les lignes de files à ajouter."""
    ajouts = []
    for col, kind, suffixe in (("schematic_json", "schematic_json", ".json"),
                               ("schematic_yaml", "schematic_yaml", ".yaml")):
        contenu = row.get(col)
        if not contenu or not str(contenu).strip():
            continue
        if (project_id, kind) in connus:
            continue
        nom = f"{project_id}_schema{suffixe}"
        chemin = os.path.join(config.USABLE_DIR, nom)
        if not os.path.exists(chemin):
            with open(chemin, "w", encoding="utf-8") as f:
                f.write(contenu)
        ajouts.append((nom, kind, suffixe))
    return ajouts


def backfill(conn, batch_size=256, limit=None) -> None:
    import pyarrow.parquet as pq
    from huggingface_hub import HfApi, HfFileSystem

    index = _index_projets(conn)
    connus = _fichiers_connus(conn)
    logger.info("%d dépôt(s) Hugging Face déjà en base.", len(index))
    if not index:
        return

    api = HfApi()
    all_files = api.list_repo_files(repo_id=config.HF_DATASET_ID, repo_type="dataset")
    parquet_files = sorted(f for f in all_files if f.startswith("data/") and f.endswith(".parquet"))

    fs = HfFileSystem()
    racine = f"datasets/{config.HF_DATASET_ID}@{config.HF_REVISION}"
    decrits, structures, ambigus = 0, 0, 0

    for n_frag, pq_path in enumerate(parquet_files, 1):
        if limit is not None and decrits >= limit:
            break
        try:
            with fs.open(f"{racine}/{pq_path}", "rb") as fh:
                pf = pq.ParquetFile(fh)
                cols = [c for c in COLONNES if c in set(pf.schema_arrow.names)]

                for lot in pf.iter_batches(batch_size=batch_size, columns=cols):
                    for row in lot.to_pylist():
                        cibles = index.get(row.get("name") or "")
                        if not cibles:
                            continue

                        description = (row.get("description") or "").strip() or None
                        composants = row.get("components_used")
                        composants_json = json.dumps(composants, ensure_ascii=False) if composants else None

                        if len(cibles) > 1:
                            # Plusieurs schémas dans ce dépôt : seule la description est attribuable.
                            composants_json = None
                            ambigus += 1

                        for project_id in cibles:
                            with conn:
                                conn.execute(
                                    "UPDATE projects SET description=COALESCE(?, description),"
                                    " components_used=COALESCE(?, components_used) WHERE project_id=?",
                                    (description, composants_json, project_id),
                                )
                            if description:
                                decrits += 1
                            if len(cibles) == 1:
                                ajouts = _ecrire_structure(project_id, row, connus)
                                if ajouts:
                                    R.append_files(conn, project_id, ajouts)
                                    connus.update((project_id, k) for _, k, _ in ajouts)
                                    structures += 1
        except Exception as exc:
            logger.warning("Fragment %s ignoré : %s: %s", pq_path, type(exc).__name__, exc)
            continue
        finally:
            gc.collect()

        if n_frag % 50 == 0:
            logger.info("Fragment %d/%d — %d description(s), %d structure(s).",
                        n_frag, len(parquet_files), decrits, structures)

    logger.info("Rattrapage terminé. %d description(s), %d structure(s), "
                "%d ligne(s) sur dépôt multi-schémas (structure non attribuée).",
                decrits, structures, ambigus)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill des colonnes HF sur les projets existants.")
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    R.init_db(config.DB_PATH)
    conn = R.connect(config.DB_PATH)
    backfill(conn, batch_size=args.batch_size, limit=args.limit)
