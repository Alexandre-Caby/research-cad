"""Parses xschem schematics (text format, "v {xschem version=...}" header)."""

import os
import re

from core.log import get_logger

logger = get_logger(__name__)

# C {symbol.sym} <x> <y> <rot> <flip> {attributes}. The attribute block may span
# several lines, hence DOTALL and a non-greedy body.
_COMPONENT = re.compile(
    r'^C\s*\{([^}]*)\}\s+-?[\d.]+\s+-?[\d.]+\s+\d+\s+\d+\s*\{(.*?)\}',
    re.MULTILINE | re.DOTALL,
)
_ATTRIBUTE = re.compile(r'([A-Za-z_][\w-]*)=("[^"]*"|\S+)')


def extract_components(path):
    path = str(path)
    parts = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            texte = handle.read()
    except OSError as exc:
        logger.warning("Unreadable xschem file %s: %s", os.path.basename(path), exc)
        return []

    for symbole, bloc in _COMPONENT.findall(texte):
        attrs = {k.lower(): v.strip('"') for k, v in _ATTRIBUTE.findall(bloc)}
        ref = attrs.get("name")
        if not ref:
            continue
        parts.append({
            "reference": ref,
            "value": attrs.get("value") or attrs.get("model") or "Unknown",
            "footprint": attrs.get("footprint") or "Unknown",
            "library": os.path.splitext(os.path.basename(symbole))[0] or "Unknown",
        })
    return parts
