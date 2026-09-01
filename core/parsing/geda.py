"""Parses gEDA gschem schematics (text format, "v <date> <version>" header)."""

import os
import re

from core.log import get_logger

logger = get_logger(__name__)

# C <x> <y> <selectable> <angle> <mirror> <symbol.sym>, optionally followed by
# a { } block holding the attributes as key=value lines.
_COMPONENT = re.compile(r'^C\s+-?\d+\s+-?\d+\s+\d+\s+\d+\s+\d+\s+(\S+)\s*$')
_ATTRIBUTE = re.compile(r'^([A-Za-z_][\w-]*)=(.*)$')


def _build(symbole, attrs):
    ref = attrs.get("refdes") or attrs.get("uref")
    if not ref:
        return None
    return {
        "reference": ref,
        "value": attrs.get("value") or attrs.get("device") or "Unknown",
        "footprint": attrs.get("footprint") or "Unknown",
        "library": os.path.splitext(symbole)[0] or "Unknown",
    }


def extract_components(path):
    path = str(path)
    parts, symbole, attrs, inside = [], None, {}, False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                comp = _COMPONENT.match(line)
                if comp:
                    symbole, attrs, inside = comp.group(1), {}, False
                    continue
                if line == "{" and symbole:
                    inside = True
                    continue
                if line == "}" and symbole:
                    kept = _build(symbole, attrs)
                    if kept:
                        parts.append(kept)
                    symbole, attrs, inside = None, {}, False
                    continue
                if inside:
                    attr = _ATTRIBUTE.match(line)
                    if attr:
                        attrs[attr.group(1).lower()] = attr.group(2).strip()
    except OSError as exc:
        logger.warning("Unreadable gEDA file %s: %s", os.path.basename(path), exc)
        return []
    return parts
