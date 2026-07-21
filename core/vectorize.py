"""Builds embedding text, batch-encodes text+image, upserts to LanceDB."""

import argparse
import json
import os

import numpy as np

from core import config
from core import registry as R
from core.parsing import eagle, kicad
from core.store import LanceStore

_SCHEMATIC_SUMMARIZERS = {".kicad_sch": kicad.summarize_schematic, ".sch": eagle.summarize}
_PCB_SUMMARIZERS = {".kicad_pcb": kicad.summarize_pcb, ".brd": eagle.summarize}


def _bom_summary(bom_path, project_name):
    import pandas as pd

    bom = pd.read_csv(bom_path)
    if bom.empty:
        return f"Electronic board named {project_name}. This board contains no recorded components."

    required_columns = {"value", "footprint"}
    if required_columns.difference(bom.columns):
        return None

    total_components = len(bom)
    grouped = bom.groupby(["value", "footprint"], dropna=False).size().reset_index(name="quantity")
    component_summaries = [
        f"{row['quantity']}x component(s) with value '{row['value'] if pd.notna(row['value']) else 'Unknown'}' "
        f"(footprint: {row['footprint'] if pd.notna(row['footprint']) else 'Unknown'})"
        for _, row in grouped.head(30).iterrows()
    ]
    return (
        f"Electronic board from project '{project_name}'. "
        f"This board contains {total_components} placed physical components. "
        f"Bill of materials summary: {', '.join(component_summaries)}."
    )


def _bom_values(bom_path):
    import pandas as pd

    bom = pd.read_csv(bom_path)
    if "value" not in bom.columns:
        return []
    return [str(v) for v in bom["value"].dropna().tolist()]


def _structural_summary(filename):
    ext = os.path.splitext(filename)[1].lower()
    abs_path = os.path.join(config.USABLE_DIR, filename)
    if not os.path.exists(abs_path):
        return None, None
    schematic = _SCHEMATIC_SUMMARIZERS.get(ext)
    if schematic:
        return schematic(abs_path), None
    pcb = _PCB_SUMMARIZERS.get(ext)
    if pcb:
        return None, pcb(abs_path)
    return None, None


def _gather(project_id, conn) -> dict:
    """One pass over a project's files: BOM summaries/values, structural
    summaries, and the first schematic/pcb filename (its basename anchors
    sidecar yaml/json lookups, per the ingest sources' shared-prefix naming)."""
    project = conn.execute(
        "SELECT name, source FROM projects WHERE project_id=?", (project_id,)
    ).fetchone()
    files = conn.execute(
        "SELECT filename, kind, ext FROM files WHERE project_id=?", (project_id,)
    ).fetchall()

    bom_summaries, components = [], set()
    for row in files:
        if row["kind"] != "bom":
            continue
        bom_path = os.path.join(config.EXTRACTED_DIR, row["filename"])
        if not os.path.exists(bom_path):
            continue
        summary = _bom_summary(bom_path, project["name"])
        if summary:
            bom_summaries.append(summary)
        components.update(_bom_values(bom_path))

    schematic_summaries, pcb_summaries = [], []
    schematic_summary, pcb_summary, source_filename = "", "", ""
    image_path = ""
    for row in files:
        if row["kind"] == "image" and not image_path:
            candidate = os.path.abspath(os.path.join(config.USABLE_DIR, row["filename"]))
            # purge_worker can unlink usable-dir files after a files row is written
            if os.path.exists(candidate):
                image_path = candidate
        if row["kind"] not in ("schematic", "pcb"):
            continue
        schematic, pcb = _structural_summary(row["filename"])
        if schematic:
            schematic_summaries.append(schematic)
            schematic_summary = schematic_summary or schematic
        if pcb:
            pcb_summaries.append(pcb)
            pcb_summary = pcb_summary or pcb
        if (schematic or pcb) and not source_filename:
            source_filename = row["filename"]

    return {
        "project": project,
        "bom_summaries": bom_summaries,
        "components": components,
        "structural_summaries": schematic_summaries + pcb_summaries,
        "schematic_summary": schematic_summary,
        "pcb_summary": pcb_summary,
        "source_filename": source_filename,
        "image_path": image_path,
    }


def _compose_text(project_name, gathered) -> str:
    bom_summaries = gathered["bom_summaries"]
    structural_summaries = gathered["structural_summaries"]
    if not bom_summaries and not structural_summaries:
        return ""

    parts = [f"Project '{project_name}'."]
    if structural_summaries:
        parts.append("Structural summary: " + " | ".join(structural_summaries))
    if bom_summaries:
        parts.append("BOM summary: " + " | ".join(bom_summaries))
    return " ".join(parts)


def build_text(project_id, conn) -> str:
    gathered = _gather(project_id, conn)
    return _compose_text(gathered["project"]["name"], gathered)


def _sidecar(base_path, ext) -> str:
    path = base_path + ext
    if not os.path.exists(path):
        return ""
    with open(path, "r", encoding="utf-8", errors="ignore") as handle:
        return handle.read()


def _project_row(project, conn) -> dict | None:
    pid = project["project_id"]
    gathered = _gather(pid, conn)

    bom_summaries = gathered["bom_summaries"]
    structural_summaries = gathered["structural_summaries"]
    text = ""
    if bom_summaries or structural_summaries:
        parts = [f"Project '{project['name']}'."]
        if structural_summaries:
            parts.append("Structural summary: " + " | ".join(structural_summaries))
        if bom_summaries:
            parts.append("BOM summary: " + " | ".join(bom_summaries))
        text = " ".join(parts)

    image_path = gathered["image_path"]
    if not text and not image_path:
        return None

    base = gathered["source_filename"]
    base_path = os.path.join(config.USABLE_DIR, os.path.splitext(base)[0]) if base else ""
    return {
        "project_id": pid,
        "name": project["name"],
        "source": project["source"],
        "text": text,
        "image_path": image_path,
        "components": sorted(gathered["components"]),
        "yaml": _sidecar(base_path, ".yaml") if base_path else "",
        "json": _sidecar(base_path, ".json") if base_path else "",
        "board_metrics": json.dumps({
            "schematic": gathered["schematic_summary"],
            "pcb": gathered["pcb_summary"],
        }),
    }


def run_vectorize(text_encoder=None, image_encoder=None, limit=None) -> None:
    from core import encoders

    text_encoder = text_encoder or encoders.TextEncoder()
    image_encoder = image_encoder or encoders.ImageEncoder()

    conn = R.connect(config.DB_PATH)
    projects = R.get_by_status(conn, R.CLEANED)
    if limit is not None:
        projects = projects[:limit]

    rows = []
    for project in projects:
        row = _project_row(project, conn)
        if row is None:
            R.update_status(conn, project["project_id"], R.EMPTY)
            continue
        rows.append(row)

    if not rows:
        return

    text_vectors = text_encoder.encode([row["text"] for row in rows])

    image_indices = [i for i, row in enumerate(rows) if row["image_path"]]
    image_vectors = {}
    if image_indices:
        paths = [rows[i]["image_path"] for i in image_indices]
        encoded = image_encoder.encode_images(paths)
        assert len(encoded) == len(paths)  # zip below silently truncates if this ever drifts
        image_vectors = dict(zip(image_indices, encoded))

    store_rows = []
    for i, row in enumerate(rows):
        row["text_vector"] = text_vectors[i]
        row["image_vector"] = image_vectors.get(i, np.zeros(image_encoder.dim, dtype="float32"))
        store_rows.append(row)

    store = LanceStore(lance_dir=config.LANCE_DIR)
    store.upsert(store_rows)
    for row in store_rows:
        R.update_status(conn, row["project_id"], R.VECTORIZED)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Batch-encode cleaned projects and upsert to LanceDB.")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    run_vectorize(limit=args.limit)
