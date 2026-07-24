"""Builds embedding text, batch-encodes text+image, upserts to LanceDB with Mouser specs enrichment."""

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
    values = set()
    for col in ("value", "lib_id"):
        if col in bom.columns:
            values.update([str(v).strip() for v in bom[col].dropna().tolist() if str(v).strip()])
    return list(values)


def _gather(project_id, conn) -> dict:
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
            if os.path.exists(candidate):
                image_path = candidate
        if row["kind"] not in ("schematic", "pcb"):
            continue

        ext = os.path.splitext(row["filename"])[1].lower()
        abs_path = os.path.join(config.USABLE_DIR, row["filename"])
        if os.path.exists(abs_path):
            sch = _SCHEMATIC_SUMMARIZERS.get(ext)
            pcb = _PCB_SUMMARIZERS.get(ext)
            if sch:
                schematic = sch(abs_path)
                schematic_summaries.append(schematic)
                schematic_summary = schematic_summary or schematic
            if pcb:
                pcb_res = pcb(abs_path)
                pcb_summaries.append(pcb_res)
                pcb_summary = pcb_summary or pcb_res
            if (sch or pcb) and not source_filename:
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


def _compose_text(project_name, gathered, conn) -> str:
    bom_summaries = gathered["bom_summaries"]
    structural_summaries = gathered["structural_summaries"]
    if not bom_summaries and not structural_summaries:
        return ""

    parts = [f"Project '{project_name}'."]
    if structural_summaries:
        parts.append("Structural summary: " + " | ".join(structural_summaries))
    if bom_summaries:
        parts.append("BOM summary: " + " | ".join(bom_summaries))

    components = gathered["components"]
    if components:
        cached_mouser = R.get_cached_components(conn, [c.upper() for c in components])
        mouser_specs = []
        for mpn, info in cached_mouser.items():
            desc = info.get("description")
            mfr = info.get("manufacturer")
            cat = info.get("category")
            if desc:
                mouser_specs.append(f"{mpn} ({mfr or 'Unknown'}): {desc} [{cat or 'General'}]")
        
        if mouser_specs:
            parts.append("Enriched Component Specs: " + " | ".join(mouser_specs[:20]))

    return " ".join(parts)


def _project_row(project, conn) -> dict | None:
    pid = project["project_id"]
    gathered = _gather(pid, conn)
    text = _compose_text(project["name"], gathered, conn)
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
        "yaml": "",
        "json": "",
        "board_metrics": json.dumps({
            "schematic": gathered["schematic_summary"],
            "pcb": gathered["pcb_summary"],
        }),
    }


def run_vectorize(text_encoder=None, image_encoder=None, limit=None, chunk_size=64) -> None:
    from core import encoders
    text_encoder = text_encoder or encoders.TextEncoder()
    image_encoder = image_encoder or encoders.ImageEncoder()

    conn = R.connect(config.DB_PATH)
    projects = R.get_by_status(conn, R.CLEANED)

    if limit is not None:
        projects = projects[:limit]
    if not projects:
        return

    store = LanceStore(lance_dir=config.LANCE_DIR)

    for i in range(0, len(projects), chunk_size):
        batch_projects = projects[i : i + chunk_size]
        rows = []
        for project in batch_projects:
            row = _project_row(project, conn)
            if row is None:
                R.update_status(conn, project["project_id"], R.EMPTY)
                continue
            rows.append(row)

        if not rows:
            continue

        text_vectors = text_encoder.encode([row["text"] for row in rows])
        image_indices = [idx for idx, r in enumerate(rows) if r["image_path"]]
        image_vectors = {}

        if image_indices:
            paths = [rows[idx]["image_path"] for idx in image_indices]
            encoded = image_encoder.encode_images(paths)
            image_vectors = dict(zip(image_indices, encoded))

        store_rows = []
        for idx, row in enumerate(rows):
            row["text_vector"] = text_vectors[idx]
            row["image_vector"] = image_vectors.get(idx, np.zeros(image_encoder.dim, dtype="float32"))
            store_rows.append(row)

        store.upsert(store_rows)
        for row in store_rows:
            R.update_status(conn, row["project_id"], R.VECTORIZED)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Batch-encode cleaned projects and upsert to LanceDB.")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    run_vectorize(limit=args.limit)