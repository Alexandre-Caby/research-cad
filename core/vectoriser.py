import json
import os
import re
import xml.etree.ElementTree as ET

try:
    import numpy as np
    import pandas as pd
    from sentence_transformers import SentenceTransformer
except ImportError as exc:
    raise SystemExit(
        "Required packages are missing. Install them with: pip install pandas numpy sentence-transformers"
    ) from exc


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)
STORAGE_DIR = os.path.join(PROJECT_ROOT, "storage")

REGISTRY_DIR = os.path.join(STORAGE_DIR, "2_register_data")
SOURCE_DIR = os.path.join(STORAGE_DIR, "3_exploitable_data")
EXTRACTED_DIR = os.path.join(STORAGE_DIR, "4_extracted_data")
VECTOR_DIR = os.path.join(STORAGE_DIR, "5_vector_data")

os.makedirs(VECTOR_DIR, exist_ok=True)
REGISTRY_PATH = os.path.join(REGISTRY_DIR, "cad_registry.csv")

PROCESSABLE_STATUSES = {"Done", "Pending", "NoBOM"}

CRITICAL_SIGNAL_LABELS = {"SPI", "I2C", "I2S", "UART", "USB", "RESET", "GND", "VCC", "SCK", "MOSI", "MISO", "CS", "CLK"}


def _extract_source_path(filename):
    source_path = os.path.join(SOURCE_DIR, filename)
    return source_path if os.path.exists(source_path) else None


def _is_critical_signal(label):
    normalized = re.sub(r"[^A-Z0-9]+", "_", str(label).upper()).strip("_")
    for token in CRITICAL_SIGNAL_LABELS:
        pattern = rf"(^|_){re.escape(token)}($|_)"
        if re.search(pattern, normalized):
            return True
    return False


def generate_semantic_description(bom_csv_path, project_name):
    """Read a BOM CSV and generate a compact semantic summary for embeddings."""
    try:
        bom = pd.read_csv(bom_csv_path)
    except Exception as exc:
        print(f"Warning: could not read BOM {os.path.basename(bom_csv_path)}: {exc}")
        return None

    if bom.empty:
        return f"Electronic board named {project_name}. This board contains no recorded components."

    required_columns = {"value", "footprint"}
    missing_columns = required_columns.difference(bom.columns)
    if missing_columns:
        print(
            f"Warning: BOM {os.path.basename(bom_csv_path)} is missing columns: {', '.join(sorted(missing_columns))}"
        )
        return None

    total_components = len(bom)
    grouped = bom.groupby(["value", "footprint"], dropna=False).size().reset_index(name="quantity")

    component_summaries = []
    for _, row in grouped.head(30).iterrows():
        value = row["value"] if pd.notna(row["value"]) else "Unknown"
        footprint = row["footprint"] if pd.notna(row["footprint"]) else "Unknown"
        quantity = row["quantity"]
        component_summaries.append(
            f"{quantity}x component(s) with value '{value}' (footprint: {footprint})"
        )

    return (
        f"Electronic board from project '{project_name}'. "
        f"This board contains {total_components} placed physical components. "
        f"Bill of materials summary: {', '.join(component_summaries)}."
    )


def _load_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            return handle.read()
    except OSError as exc:
        print(f"Warning: could not read source file {os.path.basename(path)}: {exc}")
        return None


def summarize_kicad_schematic(path):
    content = _load_text(path)
    if not content:
        return None

    net_names = set(re.findall(r'\(net \(code \d+\) \(name "([^"]+)"\)', content))
    bus_names = sorted(set(re.findall(r'\(bus \(name "([^"]+)"', content)))
    hierarchical_sheets = len(re.findall(r'\(sheet\b', content))
    label_candidates = set(re.findall(r'\(label\s+"([^"]+)"', content))
    label_candidates.update(re.findall(r'\(global_label\s+"([^"]+)"', content))
    label_candidates.update(re.findall(r'\(property "(?:Signal|Name|Net)"\s+"([^"]+)"', content))
    labels = sorted(label_candidates)
    critical_labels = sorted({label for label in labels if _is_critical_signal(label)})
    estimated_nets = len(net_names) if net_names else len(label_candidates)

    summary_parts = [
        f"Schematic source: {os.path.basename(path)}",
        f"estimated nets: {estimated_nets}" if estimated_nets else "estimated nets: not found",
        f"bus labels: {', '.join(bus_names[:10])}" if bus_names else "bus labels: none detected",
        f"hierarchical sheets: {max(1, hierarchical_sheets + 1)}",
        f"critical labels: {', '.join(critical_labels)}" if critical_labels else "critical labels: none detected",
    ]
    return ". ".join(summary_parts) + "."


def summarize_kicad_pcb(path):
    content = _load_text(path)
    if not content:
        return None

    layer_names = re.findall(r'^\s*\(\d+\s+"([^"]+)"\s+[^\)]*\)$', content, flags=re.MULTILINE)
    layer_count = len(layer_names)
    copper_layer_count = len([
        name for name in layer_names
        if name.endswith(".Cu") or name.startswith("In") and name.endswith(".Cu") or name in {"F.Cu", "B.Cu"}
    ])
    vias = len(re.findall(r'\(via\b', content))
    segments = len(re.findall(r'\(segment\b', content))
    nets = len(set(re.findall(r'\(net \d+ "([^"]+)"\)', content)))

    outline_points = []
    for start_x, start_y, end_x, end_y in re.findall(
        r'\(gr_line\s+\(start\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\)\s+\(end\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\)[^\)]*\(layer\s+"Edge.Cuts"\)',
        content,
    ):
        outline_points.extend([
            (float(start_x), float(start_y)),
            (float(end_x), float(end_y)),
        ])

    width = height = None
    if outline_points:
        xs = [point[0] for point in outline_points]
        ys = [point[1] for point in outline_points]
        width = round(max(xs) - min(xs), 2)
        height = round(max(ys) - min(ys), 2)

    summary_parts = [
        f"PCB source: {os.path.basename(path)}",
        f"copper layers: {copper_layer_count}" if copper_layer_count else (f"layers: {layer_count}" if layer_count else "copper layers: not found"),
        f"nets: {nets}" if nets else "nets: not found",
        f"vias: {vias}",
        f"trace segments: {segments}",
        f"estimated board size: {width} x {height} mm" if width and height else "estimated board size: not found",
    ]
    return ". ".join(summary_parts) + "."


def summarize_eagle_file(path):
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        print(f"Warning: malformed XML in {os.path.basename(path)}: {exc}")
        return None

    root = tree.getroot()
    layers = len(list(root.iter("layer")))
    vias = len(list(root.iter("via")))
    signals = len(list(root.iter("signal")))
    parts = len(list(root.iter("part")))

    return (
        f"Eagle source: {os.path.basename(path)}. "
        f"layers: {layers if layers else 'not found'}. "
        f"signals: {signals if signals else 'not found'}. "
        f"vias: {vias}. "
        f"parts: {parts}."
    )


def summarize_source_file(path):
    extension = os.path.splitext(path)[1].lower()
    if extension == ".kicad_sch":
        return summarize_kicad_schematic(path)
    if extension == ".kicad_pcb":
        return summarize_kicad_pcb(path)
    if extension in {".sch", ".brd"}:
        return summarize_eagle_file(path)
    return None


def build_project_embedding_text(project_row):
    extracted_files = [item.strip() for item in str(project_row["extracted_files"]).split(",") if item.strip()]
    source_summaries = []
    bom_summaries = []
    seen_bom_paths = set()

    for filename in extracted_files:
        source_path = _extract_source_path(filename)
        if source_path:
            source_summary = summarize_source_file(source_path)
            if source_summary:
                source_summaries.append(source_summary)

        bom_name = f"{filename}_BOM.csv"
        bom_path = os.path.join(EXTRACTED_DIR, bom_name)
        if os.path.exists(bom_path) and bom_path not in seen_bom_paths:
            bom_summary = generate_semantic_description(bom_path, project_row["project_name"])
            if bom_summary:
                bom_summaries.append(bom_summary)
            seen_bom_paths.add(bom_path)

    if not source_summaries and not bom_summaries:
        return None, None

    parts = [f"Project '{project_row['project_name']}'"]
    if source_summaries:
        parts.append("Tier 3 structural summary: " + " | ".join(source_summaries[:5]))
    if bom_summaries:
        parts.append("Tier 4 BOM summary: " + " | ".join(bom_summaries[:5]))

    combined_text = " ".join(parts)
    metadata = {
        "project_id": project_row["project_id"],
        "project_name": project_row["project_name"],
        "source_files": extracted_files,
        "source_summaries": source_summaries,
        "bom_summaries": bom_summaries,
    }
    return combined_text, metadata


def run_vectorization():
    if not os.path.exists(REGISTRY_PATH):
        print("Error: the central CAD registry was not found.")
        return

    registry = pd.read_csv(REGISTRY_PATH)

    required_columns = {"project_id", "project_name", "extracted_files", "processing_status"}
    missing_columns = required_columns.difference(registry.columns)
    if missing_columns:
        print(f"Error: registry is missing columns: {', '.join(sorted(missing_columns))}")
        return

    projects_to_process = registry[registry["processing_status"].isin(PROCESSABLE_STATUSES)]

    if projects_to_process.empty:
        print("CAD Vectorizer: no projects are waiting for vectorization.")
        return

    print("CAD Vectorizer: loading the 'all-MiniLM-L6-v2' embedding model...")
    model = SentenceTransformer("all-MiniLM-L6-v2")

    print(f"CAD Vectorizer: vectorizing {len(projects_to_process)} project(s)...")

    success_count = 0
    for index, project_row in projects_to_process.iterrows():
        embedding_text, metadata = build_project_embedding_text(project_row)
        if not embedding_text:
            registry.at[index, "processing_status"] = "Vector-Empty"
            continue

        vector = model.encode(embedding_text)

        project_id = project_row["project_id"]
        vector_path = os.path.join(VECTOR_DIR, f"{project_id}_vector.npy")
        np.save(vector_path, vector)

        metadata_path = os.path.join(VECTOR_DIR, f"{project_id}_meta.json")
        metadata["embedding_text"] = embedding_text
        with open(metadata_path, "w", encoding="utf-8") as metadata_file:
            json.dump(metadata, metadata_file, indent=4, ensure_ascii=False)

        registry.at[index, "processing_status"] = "Vectorized"
        success_count += 1

    registry.to_csv(REGISTRY_PATH, index=False)
    print(f"CAD Vectorizer: vectorization finished. {success_count} project(s) saved to Tier 5.")


if __name__ == "__main__":
    run_vectorization()