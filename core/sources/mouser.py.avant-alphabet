"""Mouser source: hybrid V1/V2 API search and datasheet PDF downloader."""

import argparse
import json
import os
import re
import time
import pandas as pd
import requests
from core import config
from core import registry as R
from core.log import get_logger

logger = get_logger(__name__)

MOUSER_API_KEY = os.environ.get("MOUSER_API_KEY")
API_V1_BASE = "https://api.mouser.com/api/v1/search"
API_V2_BASE = "https://api.mouser.com/api/v2/search"

MPN_MIN_LEN = 4
MPN_MAX_LEN = 40
BATCH_SIZE = 10

KEYWORD_FALLBACK = os.environ.get("MOUSER_KEYWORD_FALLBACK", "0") == "1"
KEYWORD_FALLBACK_MAX = int(os.environ.get("MOUSER_KEYWORD_FALLBACK_MAX", "50"))

DOWNLOAD_DATASHEETS = os.environ.get("MOUSER_DOWNLOAD_DATASHEETS", "0") == "1"

HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

_quota_exhausted = False
_datasheet_seen: set[str] = set()
_quota_exhausted = False
_rate_limited = False
RATE_PAUSE_S = int(os.environ.get("MOUSER_RATE_PAUSE_S", "65"))
RATE_PAUSE_MAX = int(os.environ.get("MOUSER_RATE_PAUSE_MAX", "40"))
_datasheet_seen: set[str] = set()

def _valid_mpn(value: str) -> bool:
    """Filtre AVANT appel : une seule reference invalide fait echouer tout le lot."""
    if not value:
        return False
    if not (MPN_MIN_LEN <= len(value) <= MPN_MAX_LEN):
        return False
    if "|" in value:
        return False
    if not value.isascii():
        return False
    return True


def _log_api_error(tag: str, res: requests.Response) -> None:
    """Journalise le CORPS, pas seulement le statut : Errors[].Message est la."""
    body = (res.text or "").strip()
    logger.warning("%s failed: HTTP %s | body=%s", tag, res.status_code,
                   body[:300] if body else "<vide>")
    if res.status_code in (403, 429):
        global _quota_exhausted, _rate_limited
        if "MaxCallPerMinute" in body:
            _rate_limited = True          # limite PAR MINUTE : temporisable
            logger.warning("%s: limite par minute -> pause et reprise.", tag)
        else:
            _quota_exhausted = True       # quota journalier ou cle invalide : terminal
            logger.warning("%s: quota/limite atteint -> arret des appels Mouser pour ce run.", tag)

def _download_datasheet(url: str, mpn: str) -> str | None:
    """Download the datasheet PDF from Mouser/Manufacturer CDN and store it in USABLE_DIR."""
    if not url or not url.startswith("http"):
        return None

    if url in _datasheet_seen:
        return None
    _datasheet_seen.add(url)

    safe_mpn = re.sub(r"[^A-Za-z0-9._-]+", "_", mpn)
    filename = f"datasheet_{safe_mpn}.pdf"
    dest_path = os.path.join(config.USABLE_DIR, filename)

    if os.path.exists(dest_path) and os.path.getsize(dest_path) > 1024:
        return dest_path

    try:
        response = requests.get(url, headers=HEADERS, timeout=20, stream=True)
        if response.status_code == 200 and b"%PDF" in response.content[:1024]:
            with open(dest_path, "wb") as f:
                f.write(response.content)
            logger.info("Downloaded datasheet for %s -> %s", mpn, filename)
            return dest_path
        logger.warning("Datasheet URL %s did not return a valid PDF (status %d)",
                       url, response.status_code)
    except requests.RequestException as exc:
        logger.warning("Could not download datasheet for %s from %s: %s", mpn, url, exc)

    return None


def _parse_mouser_response(data: dict) -> list[dict]:
    """Parse unified response parts array from V1 or V2 search payload."""
    errors = data.get("Errors") or []
    if errors:
        logger.warning("Mouser API a renvoye des erreurs applicatives: %s", errors[:3])

    results = []
    search_results = data.get("SearchResults") or {}
    parts = search_results.get("Parts") or []

    for part in parts:
        mpn = part.get("ManufacturerPartNumber") or part.get("MouserPartNumber")
        if not mpn:
            continue

        results.append({
            "mpn": mpn.upper().strip(),
            "mouser_part_number": part.get("MouserPartNumber", ""),
            "manufacturer": part.get("Manufacturer", ""),
            "description": part.get("Description", ""),
            "category": part.get("Category", ""),
            "lifecycle_status": part.get("LifecycleStatus", "") or "",
            "datasheet_url": part.get("DataSheetUrl", "") or "",
            "attributes_json": json.dumps(part.get("ProductAttributes", [])),
        })
    return results


def search_part_v1(mpn_list: list[str]) -> list[dict]:
    """API V1: Search by Part Number without manufacturer constraint (batch up to 10 pipe-separated MPNs)."""
    if not MOUSER_API_KEY or not mpn_list or _quota_exhausted:
        return []

    pipe_mpns = "|".join(mpn_list[:BATCH_SIZE])
    url = f"{API_V1_BASE}/partnumber?apiKey={MOUSER_API_KEY}"
    payload = {
        "SearchByPartRequest": {
            "mouserPartNumber": pipe_mpns,
            "partSearchOptions": "Exact",
        }
    }

    try:
        res = requests.post(url, json=payload, headers=HEADERS, timeout=15)
        if res.status_code == 200:
            return _parse_mouser_response(res.json())
        _log_api_error("API V1 PartSearch", res)
    except requests.RequestException as exc:
        logger.error("API V1 PartSearch network error: %s", exc)

    return []


def search_part_v2_with_mfr(mpn: str, manufacturer: str) -> list[dict]:
    """API V2: Search by Part Number + Manufacturer Name. (Non utilise actuellement.)"""
    if not MOUSER_API_KEY or not mpn or not manufacturer or _quota_exhausted:
        return []

    url = f"{API_V2_BASE}/partnumberandmanufacturer?apiKey={MOUSER_API_KEY}"
    payload = {
        "SearchByPartMfrNameRequest": {
            "manufacturerName": manufacturer,
            "mouserPartNumber": mpn,
            "partSearchOptions": "Exact",
            "mouserPaysCustomsAndDuties": False,
        }
    }

    try:
        res = requests.post(url, json=payload, headers=HEADERS, timeout=15)
        if res.status_code == 200:
            return _parse_mouser_response(res.json())
        _log_api_error("API V2 PartMfrSearch", res)
    except requests.RequestException as exc:
        logger.error("API V2 PartMfrSearch network error: %s", exc)

    return []


def search_keyword_v1(keyword: str) -> list[dict]:
    """API V1: Fallback keyword search for fuzzy component values."""
    if not MOUSER_API_KEY or not keyword or _quota_exhausted:
        return []

    url = f"{API_V1_BASE}/keyword?apiKey={MOUSER_API_KEY}"
    payload = {
        "SearchByKeywordRequest": {
            "keyword": keyword,
            "records": 5,
            "startingRecord": 0,
            "searchOptions": "None",
        }
    }

    try:
        res = requests.post(url, json=payload, headers=HEADERS, timeout=15)
        if res.status_code == 200:
            return _parse_mouser_response(res.json())
        _log_api_error("API V1 KeywordSearch", res)
    except requests.RequestException as exc:
        logger.error("API V1 KeywordSearch error: %s", exc)

    return []


def _cache_miss(conn, mpn: str) -> None:
    """Memorise une reference introuvable.
    """
    R.upsert_component(
        conn, mpn=mpn, mouser_pn="", mfr="", desc="",
        category="", status="NOT_FOUND", ds_url="", ds_path=None,
        attrs_json="[]",
    )


def collect_mouser_metadata(conn, limit=None, download_pdfs=None) -> None:
    global _quota_exhausted, _datasheet_seen, _rate_limited
    _quota_exhausted = False
    _rate_limited = False
    _datasheet_seen = set()

    if download_pdfs is None:
        download_pdfs = DOWNLOAD_DATASHEETS

    if not MOUSER_API_KEY:
        logger.warning("MOUSER_API_KEY environment variable is missing. Skipping Mouser source.")
        return

    projects = R.get_by_status(conn, R.CLEANED)
    if limit:
        projects = projects[:limit]

    candidates = set()
    rejected = 0
    for project in projects:
        pid = project["project_id"]
        bom_files = conn.execute(
            "SELECT filename FROM files WHERE project_id=? AND kind='bom'", (pid,)
        ).fetchall()

        for bf in bom_files:
            bom_path = os.path.join(config.EXTRACTED_DIR, bf["filename"])
            if not os.path.exists(bom_path):
                continue
            try:
                df = pd.read_csv(bom_path)
                for col in ("value", "lib_id"):
                    if col in df.columns:
                        for val in df[col].dropna().unique():
                            v_str = str(val).strip().upper()
                            if ":" in v_str:
                                v_str = v_str.split(":")[-1]
                            if v_str.endswith("OHM") or v_str == "UNKNOWN":
                                continue
                            if not _valid_mpn(v_str):
                                rejected += 1
                                continue
                            candidates.add(v_str)
            except Exception as exc:
                logger.warning("Error reading BOM %s: %s", bf["filename"], exc)

    if rejected:
        logger.info("Mouser Source: %d valeur(s) ecartee(s) (hors 4-40 caracteres, pipe ou non-ASCII).",
                    rejected)

    if not candidates:
        logger.info("Mouser Source: No eligible component MPNs found in cleaned BOMs.")
        return

    cached = R.get_cached_components(conn, list(candidates))
    missing_mpns = sorted(list(candidates - set(cached.keys())))

    logger.info("Mouser Source: %d total component candidate(s), %d pending API lookup.",
                len(candidates), len(missing_mpns))
    logger.info("Mouser Source: repli mot-cle=%s (max %d), telechargement datasheets=%s.",
                "actif" if KEYWORD_FALLBACK else "inactif", KEYWORD_FALLBACK_MAX,
                "actif" if download_pdfs else "inactif")

    processed_count = 0
    miss_count = 0
    fallback_used = 0
    pauses = 0

    for idx in range(0, len(missing_mpns), BATCH_SIZE):
        if _quota_exhausted:
            restants = len(missing_mpns) - idx
            logger.warning("Mouser Source: arret anticipe, %d reference(s) non traitee(s).", restants)
            break

        batch = missing_mpns[idx : idx + BATCH_SIZE]

        mouser_parts = search_part_v1(batch)

        while _rate_limited and not _quota_exhausted:
            if pauses >= RATE_PAUSE_MAX:
                _quota_exhausted = True
                logger.warning("Mouser Source: %d pauses atteintes -> abandon reel.", RATE_PAUSE_MAX)
                break
            pauses += 1
            logger.info("Mouser Source: pause %ds (limite/minute, %d/%d).",
                        RATE_PAUSE_S, pauses, RATE_PAUSE_MAX)
            time.sleep(RATE_PAUSE_S)
            _rate_limited = False
            mouser_parts = search_part_v1(batch)

        if _quota_exhausted:
            restants = len(missing_mpns) - idx
            logger.warning("Mouser Source: arret anticipe, %d reference(s) non traitee(s).", restants)
            break

        found_mpns = {p["mpn"] for p in mouser_parts}
        for mpn in batch:
            if mpn in found_mpns:
                continue
            if KEYWORD_FALLBACK and fallback_used < KEYWORD_FALLBACK_MAX and not _quota_exhausted:
                fallback_used += 1
                fallback_parts = search_keyword_v1(mpn)
                if fallback_parts:
                    mouser_parts.append(fallback_parts[0])
                    continue
            _cache_miss(conn, mpn)
            miss_count += 1

        for item in mouser_parts:
            ds_path = None
            if download_pdfs and item["datasheet_url"]:
                ds_path = _download_datasheet(item["datasheet_url"], item["mpn"])

            R.upsert_component(
                conn,
                mpn=item["mpn"],
                mouser_pn=item["mouser_part_number"],
                mfr=item["manufacturer"],
                desc=item["description"],
                category=item["category"],
                status=item["lifecycle_status"],
                ds_url=item["datasheet_url"],
                ds_path=ds_path,
                attrs_json=item["attributes_json"],
            )
            processed_count += 1

        time.sleep(2.0)

    logger.info("Mouser Source: Processed and cached %d component(s), %d introuvable(s), %d repli(s).",
                processed_count, miss_count, fallback_used)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch Mouser component metadata and datasheets.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--no-pdfs", action="store_true", help="Disable PDF datasheet download")
    parser.add_argument("--pdfs", action="store_true",
                        help="Force datasheet download (bloque par anti-bot Mouser au 31/07/2026)")
    args = parser.parse_args()

    dl = None
    if args.no_pdfs:
        dl = False
    elif args.pdfs:
        dl = True

    R.init_db(config.DB_PATH)
    conn = R.connect(config.DB_PATH)
    collect_mouser_metadata(conn, limit=args.limit, download_pdfs=dl)
