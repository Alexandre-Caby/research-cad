"""Parses schematics/boards into BOM CSVs, one worker process per file."""

import argparse
import csv
import os
from concurrent.futures import ProcessPoolExecutor

from core import config
from core import registry as R
from core.log import get_logger
from core.parsing import eagle, kicad

logger = get_logger(__name__)

# .kicad_pcb is a board layout, not a schematic -- it carries no BOM here.
EXTRACTORS = {".kicad_sch": kicad.extract_components, ".sch": eagle.extract_components, ".brd": eagle.extract_components}


def _parse(ext, path):
    return EXTRACTORS[ext](path)


def _write_bom(filename, components):
    fieldnames = ["reference", "value", "footprint"]
    extra = [key for key in components[0] if key not in fieldnames]
    fieldnames += extra

    bom_name = f"{filename}_BOM.csv"
    bom_path = os.path.join(config.EXTRACTED_DIR, bom_name)
    with open(bom_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(components)
    return bom_name


def run_clean(limit=None, workers=None):
    conn = R.connect(config.DB_PATH)
    projects = R.get_by_status(conn, R.INGESTED)
    if limit is not None:
        projects = projects[:limit]

    work_items = []
    for project in projects:
        pid = project["project_id"]
        rows = conn.execute(
            "SELECT filename, kind, ext FROM files WHERE project_id = ?", (pid,)
        ).fetchall()
        for row in rows:
            if row["ext"] in EXTRACTORS:
                abs_path = os.path.join(config.USABLE_DIR, row["filename"])
                work_items.append((pid, row["filename"], row["ext"], abs_path))

    if not work_items:
        for project in projects:
            R.update_status(conn, project["project_id"], R.EMPTY)
        return

    results = {}
    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_parse, ext, abs_path): (pid, filename)
            for pid, filename, ext, abs_path in work_items
        }
        for future, (pid, filename) in futures.items():
            try:
                components = future.result()
            except Exception as exc:
                logger.error("Failed to parse %s (project %s): %s", filename, pid, exc)
                components = []
            results.setdefault(pid, []).append((filename, components))

    for project in projects:
        pid = project["project_id"]
        bom_written = False
        for filename, components in results.get(pid, []):
            if not components:
                continue
            bom_name = _write_bom(filename, components)
            R.add_files(conn, pid, [(bom_name, "bom", ".csv")])
            bom_written = True
        R.update_status(conn, pid, R.CLEANED if bom_written else R.EMPTY)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Parse schematics/boards into BOM CSVs.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=None)
    args = parser.parse_args()
    run_clean(limit=args.limit, workers=args.workers)
