"""Single source of truth for Eagle XML parsing (schematics and boards)."""

import os
import xml.etree.ElementTree as ET

from core.log import get_logger

logger = get_logger(__name__)

# Schematics place parts under <part> with a "device" footprint attribute;
# boards place the same parts under <element> with "package" instead -- the
# tag and attribute name differ by file kind, not the schema.
_PART_SPEC = {".sch": ("part", "device"), ".brd": ("element", "package")}


def extract_components(path):
    """Parses an Eagle schematic (.sch) or board (.brd, XML format) and
    lists its parts."""
    path = str(path)
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        logger.warning("Malformed XML in %s: %s", os.path.basename(path), exc)
        return []

    extension = os.path.splitext(path)[1].lower()
    tag, footprint_attr = _PART_SPEC.get(extension, ("part", "device"))

    return [
        {
            "reference": el.get("name", "Unknown"),
            "value": el.get("value", "Unknown"),
            "footprint": el.get(footprint_attr, "Unknown"),
            "library": el.get("library", "Unknown"),
        }
        for el in tree.getroot().iter(tag)
    ]


def summarize(path):
    path = str(path)
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        logger.warning("Malformed XML in %s: %s", os.path.basename(path), exc)
        return None

    root = tree.getroot()
    layers = len(list(root.iter("layer")))
    signals = len(list(root.iter("signal")))
    vias = len(list(root.iter("via")))
    parts = len(list(root.iter("part")))

    return (
        f"Eagle source: {os.path.basename(path)}. "
        f"layers: {layers if layers else 'not found'}. "
        f"signals: {signals if signals else 'not found'}. "
        f"vias: {vias}. "
        f"parts: {parts}."
    )
