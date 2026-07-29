"""Parses schematics/boards into BOM CSVs with real-time checkpoints."""

import argparse
import csv
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from core import config
from core import registry as R
from core.log import get_logger
from core.parsing import eagle, kicad

logger = get_logger(__name__)

EXTRACTORS = {
    ".kicad_sch": kicad.extract_components,
    ".sch": eagle.extract_components,
    ".brd": eagle.extract_components,
}


def _parse_file(args):
    """Worker function executed in worker processes."""
    pid, filename, ext, abs_path = args
    try:
        components = EXTRACTORS[ext](abs_path)
        return pid, filename, components, None
    except Exception as exc:
        return pid, filename, [], str(exc)


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

    if not projects:
        logger.info("CAD Cleaner: no projects waiting to be cleaned.")
        return

    tasks = []
    project_files_count = {}
    
    for project in projects:
        pid = project["project_id"]
        rows = conn.execute(
            "SELECT filename, kind, ext FROM files WHERE project_id = ?", (pid,)
        ).fetchall()
        
        valid_files = [r for r in rows if r["ext"] in EXTRACTORS]
        project_files_count[pid] = len(valid_files)
        
        if not valid_files:
            R.update_status(conn, pid, R.EMPTY)
            continue
            
        for row in valid_files:
            abs_path = os.path.join(config.USABLE_DIR, row["filename"])
            tasks.append((pid, row["filename"], row["ext"], abs_path))

    project_results = {p["project_id"]: [] for p in projects if project_files_count.get(p["project_id"], 0) > 0}

    with ProcessPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_parse_file, task): task for task in tasks}

        for future in as_completed(futures):
            pid, filename, components, err = future.result()
            
            if err:
                logger.error("Failed to parse %s (project %s): %s", filename, pid, err)
            
            if components:
                bom_name = _write_bom(filename, components)
                R.append_files(conn, pid, [(bom_name, "bom", ".csv")])
                project_results[pid].append(True)
            else:
                project_results[pid].append(False)

            if len(project_results[pid]) == project_files_count[pid]:
                has_bom = any(project_results[pid])
                R.update_status(conn, pid, R.CLEANED if has_bom else R.EMPTY)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Parse schematics/boards into BOM CSVs.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=None)
    args = parser.parse_args()
    run_clean(limit=args.limit, workers=args.workers)