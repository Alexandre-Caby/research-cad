"""OSHWLab source: captures embedded EasyEDA state for later manual review."""
import argparse
import os

import requests

from core import config
from core import registry as R
from core.log import get_logger

logger = get_logger(__name__)

OSHWLAB_URL_LIST = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "oshwlab_urls.txt"
)


def collect(conn, url_list_path=OSHWLAB_URL_LIST) -> None:
    if not os.path.exists(url_list_path):
        logger.info(
            "%s not found -- create it with one OSHWLab project URL per line "
            "to enable this source. Skipping.", url_list_path,
        )
        return

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.warning("beautifulsoup4 is required for this source (pip install beautifulsoup4). Skipping.")
        return

    with open(url_list_path, encoding="utf-8") as f:
        urls = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    logger.info("Processing %d OSHWLab URL(s)...", len(urls))

    for url in urls:
        project_slug = url.rstrip("/").split("/")[-1]
        project_id = f"oshwlab_{project_slug}"

        try:
            response = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        except requests.RequestException as exc:
            logger.warning("Could not fetch %s: %s", url, exc)
            response = None

        if response is not None and response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            for script in soup.find_all("script"):
                text = script.string or ""
                if '"schematic"' in text or '"docType"' in text:
                    dest_name = f"oshwlab_{project_slug}_state.json"
                    with open(os.path.join(config.USABLE_DIR, dest_name), "w", encoding="utf-8") as out:
                        out.write(text)
                    R.add_files(conn, project_id, [(dest_name, "raw", ".json")])
                    break

        # EasyEDA state has no downstream parser; every project lands in
        # MANUAL_REVIEW so clean/vectorize skip it rather than dead-ending silently.
        R.upsert_project(conn, project_id, project_slug, source="OSHWLab", url=url, status=R.MANUAL_REVIEW)
        logger.info("%s: MANUAL_REVIEW", project_slug)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url-list", default=OSHWLAB_URL_LIST)
    args = parser.parse_args()

    R.init_db(config.DB_PATH)
    conn = R.connect(config.DB_PATH)
    collect(conn, args.url_list)
