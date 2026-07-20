"""
Extracts a Bill of Materials (BOM) from every schematic sitting in
3_exploitable_data, for every project the registry marks as "Pending".

Two formats are supported:
  - KiCad (.kicad_sch), the modern open S-expression format
  - Eagle (.sch / .brd), an older XML format still used by many legacy
    Adafruit repositories
"""

import os
import re
import xml.etree.ElementTree as ET

import pandas as pd

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
STORAGE_DIR = os.path.join(ROOT_DIR, "storage")

REGISTRY_DIR = os.path.join(STORAGE_DIR, "2_register_data")
USABLE_DIR = os.path.join(STORAGE_DIR, "3_exploitable_data")
EXTRACTED_DIR = os.path.join(STORAGE_DIR, "4_extracted_data")

os.makedirs(EXTRACTED_DIR, exist_ok=True)
REGISTRY_PATH = os.path.join(REGISTRY_DIR, "cad_registry.csv")

_INSTANCE_START = re.compile(r'\(symbol\s*\(lib_id\s+"([^"]+)"')
_DEFINITION_START = re.compile(r'\(symbol\s+"')
_REFERENCE = re.compile(r'\(property "Reference"\s+"([^"]*)"')
_VALUE = re.compile(r'\(property "Value"\s+"([^"]*)"')
_FOOTPRINT = re.compile(r'\(property "Footprint"\s+"([^"]*)"')


def extract_components_kicad(path):
    """Parses a KiCad v6+ schematic (S-expression format) and lists the
    components actually placed on the sheet (skips library symbol
    definitions/templates, which look similar but aren't real instances)."""
    components = []
    current = None

    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            stripped = line.strip()

            instance_match = _INSTANCE_START.search(stripped)
            if instance_match:
                if current:
                    components.append(current)
                current = {
                    "reference": "Unknown",
                    "value": "Unknown",
                    "footprint": "Unknown",
                    "lib_id": instance_match.group(1),
                }
                continue

            if _DEFINITION_START.match(stripped):
                # Start of a library symbol *definition* -- not a placed
                # component. Flush whatever instance we were building and
                # stop collecting until the next real instance.
                if current:
                    components.append(current)
                current = None
                continue

            if current is None:
                continue

            match = _REFERENCE.search(stripped)
            if match:
                current["reference"] = match.group(1)
                continue
            match = _VALUE.search(stripped)
            if match:
                current["value"] = match.group(1)
                continue
            match = _FOOTPRINT.search(stripped)
            if match:
                current["footprint"] = match.group(1)

    if current:
        components.append(current)
    return pd.DataFrame(components)


def extract_components_eagle(path):
    """Parses an Eagle schematic/board (.sch or .brd, XML format) and lists
    its parts."""
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        print(f"  ⚠️ Malformed XML in {os.path.basename(path)}: {exc}")
        return pd.DataFrame()

    components = [
        {
            "reference": part.get("name", "Unknown"),
            "value": part.get("value", "Unknown"),
            "footprint": part.get("device", "Unknown"),
            "library": part.get("library", "Unknown"),
        }
        for part in tree.getroot().iter("part")
    ]
    return pd.DataFrame(components)


EXTRACTORS = {
    ".kicad_sch": extract_components_kicad,
    ".sch": extract_components_eagle,
    ".brd": extract_components_eagle,
}


def run_cleaning():
    if not os.path.exists(REGISTRY_PATH):
        print("⚠️ Central registry not found.")
        return

    df = pd.read_csv(REGISTRY_PATH)
    pending_indices = df[df["processing_status"] == "Pending"].index

    if len(pending_indices) == 0:
        print("CAD Cleaner: no project waiting to be cleaned.")
        return

    print(f"CAD Cleaner: normalizing {len(pending_indices)} project(s)...")

    for idx in pending_indices:
        files_field = df.at[idx, "extracted_files"]
        if pd.isna(files_field) or not str(files_field).strip():
            df.at[idx, "processing_status"] = "Empty"
            continue

        file_list = [f.strip() for f in str(files_field).split(",")]
        bom_generated = False

        for filename in file_list:
            file_path = os.path.join(USABLE_DIR, filename)
            if not os.path.exists(file_path):
                continue

            extension = os.path.splitext(filename)[1].lower()
            extractor = EXTRACTORS.get(extension)
            if extractor is None:
                continue

            bom_df = extractor(file_path)
            if not bom_df.empty:
                # Keep the full original filename (extension included) in the
                # BOM name: a project can have both a .kicad_sch and a .sch
                # sharing the same base name, and os.path.splitext() would
                # otherwise make both collapse onto the same "<base>_BOM.csv"
                # and silently overwrite one with the other.
                bom_name = f"{filename}_BOM.csv"
                bom_df.to_csv(os.path.join(EXTRACTED_DIR, bom_name), index=False)
                bom_generated = True

        df.at[idx, "processing_status"] = "Done" if bom_generated else "NoBOM"

    df.to_csv(REGISTRY_PATH, index=False)
    print("CAD Cleaner: cleaning complete. Bills of materials written to Tier 4.")


if __name__ == "__main__":
    run_cleaning()