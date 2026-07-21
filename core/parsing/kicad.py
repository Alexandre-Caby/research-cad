"""Single source of truth for KiCad file parsing (schematics and PCBs)."""

import os
import re

CRITICAL_SIGNAL_LABELS = {"SPI", "I2C", "I2S", "UART", "USB", "RESET", "GND", "VCC", "SCK", "MOSI", "MISO", "CS", "CLK"}

_INSTANCE_START = re.compile(r'\(symbol\s*\(lib_id\s+"([^"]+)"')
_DEFINITION_START = re.compile(r'\(symbol\s+"')
# Modern KiCad (v6+) pretty-printer puts "(symbol" and its "(lib_id ...)"
# child on separate lines, so a bare "(symbol" also opens an instance --
# the lib_id only becomes visible on the following line.
_BARE_SYMBOL_OPEN = re.compile(r'^\(symbol\s*$')
_LIB_ID_ONLY = re.compile(r'^\(lib_id\s+"([^"]+)"\)$')
_REFERENCE = re.compile(r'\(property "Reference"\s+"([^"]*)"')
_VALUE = re.compile(r'\(property "Value"\s+"([^"]*)"')
_FOOTPRINT = re.compile(r'\(property "Footprint"\s+"([^"]*)"')


def _load_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            return handle.read()
    except OSError as exc:
        print(f"Warning: could not read source file {os.path.basename(path)}: {exc}")
        return None


def _is_critical_signal(label):
    normalized = re.sub(r"[^A-Z0-9]+", "_", str(label).upper()).strip("_")
    for token in CRITICAL_SIGNAL_LABELS:
        pattern = rf"(^|_){re.escape(token)}($|_)"
        if re.search(pattern, normalized):
            return True
    return False


def extract_components(path):
    """Parses a KiCad v6+ schematic (S-expression format) and lists the
    components actually placed on the sheet (skips library symbol
    definitions/templates, which look similar but aren't real instances)."""
    components = []
    current = None
    pending_instance_open = False

    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            stripped = line.strip()

            if pending_instance_open:
                pending_instance_open = False
                lib_id_match = _LIB_ID_ONLY.match(stripped)
                if lib_id_match:
                    if current:
                        components.append(current)
                    current = {
                        "reference": "Unknown",
                        "value": "Unknown",
                        "footprint": "Unknown",
                        "lib_id": lib_id_match.group(1),
                    }
                    continue
                # Not actually followed by a lib_id -- fall through and let
                # the checks below process this line normally.

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

            if _BARE_SYMBOL_OPEN.match(stripped):
                pending_instance_open = True
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
    return components


def summarize_schematic(path):
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


def summarize_pcb(path):
    content = _load_text(path)
    if not content:
        return None

    layer_names = re.findall(r'^\s*\(\d+\s+"([^"]+)"\s+[^\)]*\)$', content, flags=re.MULTILINE)
    layer_count = len(layer_names)
    copper_layer_count = len([
        name for name in layer_names
        if name in {"F.Cu", "B.Cu"} or (name.startswith("In") and name.endswith(".Cu"))
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
