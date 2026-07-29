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

HEADERS = {
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}


def _download_datasheet(url: str, mpn: str) -> str | None:
    """Download the datasheet PDF from Mouser/Manufacturer CDN and store it in USABLE_DIR."""
    if not url or not url.startswith("http"):
        return None

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
        else:
            logger.warning("Datasheet URL %s did not return a valid PDF (status %d)", url, response.status_code)
    except requests.RequestException as exc:
        logger.warning("Could not download datasheet for %s from %s: %s", mpn, url, exc)

    return None


def _parse_mouser_response(data: dict) -> list[dict]:
    """Parse unified response parts array from V1 or V2 search payload."""
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
            "lifecycle_status": part.get("LifecycleStatus", ""),
            "datasheet_url": part.get("DataSheetUrl", ""),
            "attributes_json": json.dumps(part.get("ProductAttributes", [])),
        })
    return results


def search_part_v1(mpn_list: list[str]) -> list[dict]:
    """API V1: Search by Part Number without manufacturer constraint (batch up to 10 pipe-separated MPNs)."""
    if not MOUSER_API_KEY or not mpn_list:
        return []

    pipe_mpns = "|".join(mpn_list[:10])
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
        logger.warning("API V1 PartSearch failed: HTTP %d", res.status_code)
    except requests.RequestException as exc:
        logger.error("API V1 PartSearch network error: %s", exc)

    return []


def search_part_v2_with_mfr(mpn: str, manufacturer: str) -> list[dict]:
    """API V2: Search by Part Number + Manufacturer Name."""
    if not MOUSER_API_KEY or not mpn or not manufacturer:
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
        logger.warning("API V2 PartMfrSearch failed: HTTP %d", res.status_code)
    except requests.RequestException as exc:
        logger.error("API V2 PartMfrSearch network error: %s", exc)

    return []


def search_keyword_v1(keyword: str) -> list[dict]:
    """API V1: Fallback keyword search for fuzzy component values."""
    if not MOUSER_API_KEY or not keyword:
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
    except requests.RequestException as exc:
        logger.error("API V1 KeywordSearch error: %s", exc)

    return []


def collect_mouser_metadata(conn, limit=None, download_pdfs=True) -> None:
    if not MOUSER_API_KEY:
        logger.warning("MOUSER_API_KEY environment variable is missing. Skipping Mouser source.")
        return

    projects = R.get_by_status(conn, R.CLEANED)
    if limit:
        projects = projects[:limit]

    candidates = set()
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
                            if len(v_str) > 3 and not v_str.endswith("OHM") and v_str != "UNKNOWN":
                                candidates.add(v_str)
            except Exception as exc:
                logger.warning("Error reading BOM %s: %s", bf["filename"], exc)

    if not candidates:
        logger.info("Mouser Source: No eligible component MPNs found in cleaned BOMs.")
        return

    cached = R.get_cached_components(conn, list(candidates))
    missing_mpns = sorted(list(candidates - set(cached.keys())))

    logger.info("Mouser Source: %d total component candidate(s), %d pending API lookup.",
                len(candidates), len(missing_mpns))

    batch_size = 10
    processed_count = 0

    for idx in range(0, len(missing_mpns), batch_size):
        batch = missing_mpns[idx : idx + batch_size]

        mouser_parts = search_part_v1(batch)
        found_mpns = {p["mpn"] for p in mouser_parts}

        for mpn in batch:
            if mpn not in found_mpns:
                fallback_parts = search_keyword_v1(mpn)
                if fallback_parts:
                    mouser_parts.append(fallback_parts[0])

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

    logger.info("Mouser Source: Processed and cached %d component(s).", processed_count)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch Mouser component metadata and datasheets.")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--no-pdfs", action="store_true", help="Disable PDF datasheet download")
    args = parser.parse_args()

    R.init_db(config.DB_PATH)
    conn = R.connect(config.DB_PATH)
    collect_mouser_metadata(conn, limit=args.limit, download_pdfs=not args.no_pdfs)