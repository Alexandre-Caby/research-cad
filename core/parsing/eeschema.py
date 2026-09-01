"""Parses legacy KiCad schematics (EESchema file format, v2 to v5)."""

import os
import re

from core.log import get_logger

logger = get_logger(__name__)

_LIB_LINE = re.compile(r'^L\s+(\S+)\s+(\S+)\s*$')
_FIELD_LINE = re.compile(r'^F\s+(\d+)\s+"([^"]*)"')

# F 0 is the reference, F 1 the value, F 2 the footprint. Later fields are
# datasheet and user-defined ones, which the BOM does not carry.
_FIELD_NAMES = {"0": "reference", "1": "value", "2": "footprint"}


def _flush(part):
    """A component is kept only once its reference is known."""
    if not part.get("reference"):
        return None
    return {
        "reference": part["reference"],
        "value": part.get("value") or "Unknown",
        "footprint": part.get("footprint") or "Unknown",
        "library": part.get("library") or "Unknown",
    }


def extract_components(path):
    path = str(path)
    parts, part, inside = [], {}, False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if line == "$Comp":
                    part, inside = {}, True
                    continue
                if line == "$EndComp":
                    kept = _flush(part)
                    if kept:
                        parts.append(kept)
                    part, inside = {}, False
                    continue
                if not inside:
                    continue

                lib = _LIB_LINE.match(line)
                if lib:
                    # v4+ writes "library:part"; older versions only the part name.
                    nom, ref = lib.group(1), lib.group(2)
                    part["library"] = nom.split(":")[0] if ":" in nom else "Unknown"
                    part.setdefault("value", nom.split(":")[-1])
                    part.setdefault("reference", ref)
                    continue

                field = _FIELD_LINE.match(line)
                if field and field.group(1) in _FIELD_NAMES:
                    valeur = field.group(2).strip()
                    if valeur:
                        part[_FIELD_NAMES[field.group(1)]] = valeur
    except OSError as exc:
        logger.warning("Unreadable EESchema file %s: %s", os.path.basename(path), exc)
        return []
    return parts
