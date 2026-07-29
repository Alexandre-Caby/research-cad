"""GitHub source: scans an org's repositories for CAD files with instant skip for known projects."""

import argparse
import hashlib
import os
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from core import config
from core import registry as R
from core.log import get_logger

logger = get_logger(__name__)

GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO_WORKERS = int(os.environ.get("GITHUB_REPO_WORKERS", "6"))
_PCB_EXTENSIONS = {".kicad_pcb", ".brd", ".pcbdoc"}
_BINARY_EXTENSIONS = {".schdoc", ".pcbdoc"}

os.makedirs(config.USABLE_DIR, exist_ok=True)
os.makedirs(config.RAW_DIR, exist_ok=True)


def _github_headers():
    headers = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def _github_get(url, params=None, max_retries=5):
    response = None
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(url, headers=_github_headers(), params=params, timeout=30)
        except requests.RequestException as e:
            logger.warning("Network/Proxy error on GitHub request (%d/%d): %s. Retrying in 5s...", attempt, max_retries, e)
            time.sleep(5)
            continue
        if response.status_code == 200:
            return response
        if response.status_code in (403, 429) and response.headers.get("X-RateLimit-Remaining") == "0":
            reset_at = int(response.headers.get("X-RateLimit-Reset", time.time() + 60))
            wait_s = max(reset_at - int(time.time()), 1) + 2
            logger.info("GitHub rate limit reached, waiting %ss...", wait_s)
            time.sleep(min(wait_s, 900))
            continue
        return response
    return None


def _list_repo_tree(owner, repo, branch):
    response = _github_get(
        f"{GITHUB_API}/repos/{owner}/{repo}/git/trees/{branch}",
        params={"recursive": "1"},
    )
    if response is None or response.status_code != 200:
        return None, False
    data = response.json()
    return data.get("tree", []), bool(data.get("truncated", False))


def _fetch_raw_file(owner, repo, branch, path):
    url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
    try:
        with requests.get(url, timeout=30) as response:
            return response.content if response.status_code == 200 else None
    except requests.RequestException:
        return None


def _dest_name(org, name, path):
    basename = os.path.basename(path)
    dirname = os.path.dirname(path)
    if not dirname:
        return f"{org}_{name}_{basename}"
    dir_hash = hashlib.sha1(dirname.encode()).hexdigest()[:8]
    return f"{org}_{name}_{dir_hash}_{basename}"


def _file_kind(ext):
    return "pcb" if ext in _PCB_EXTENSIONS else "schematic"


def _is_duplicate(conn, content, ext):
    if ext in _BINARY_EXTENSIONS:
        return False
    text = content.decode("utf-8", errors="ignore")
    return R.hash_seen(conn, R.content_hash(text)) is not None


def _primary_content_hash(entries: list[tuple[bytes, str, str]]) -> str | None:
    for content, kind, ext in entries:
        if kind == "schematic" and ext not in _BINARY_EXTENSIONS:
            return R.content_hash(content.decode("utf-8", errors="ignore"))
    return None


def _download_repo_zip_fallback(conn, org, name, branch):
    url = f"{GITHUB_API}/repos/{org}/{name}/zipball/{branch}"
    try:
        with requests.get(url, headers=_github_headers(), stream=True, timeout=180) as response:
            if response.status_code != 200:
                return [], None
            zip_path = os.path.join(config.RAW_DIR, f"{org}_{name}.zip")
            with open(zip_path, "wb") as f:
                for chunk in response.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
    except requests.RequestException:
        return [], None

    extracted = []
    raw_entries = []
    try:
        with zipfile.ZipFile(zip_path) as archive:
            for info in archive.infolist():
                ext = os.path.splitext(info.filename)[1].lower()
                if ext not in config.CAD_EXTENSIONS:
                    continue
                content = archive.read(info.filename)
                if _is_duplicate(conn, content, ext):
                    continue
                relative_path = info.filename.split("/", 1)[-1]
                dest_name = _dest_name(org, name, relative_path)
                dest_path = os.path.join(config.USABLE_DIR, dest_name)
                with open(dest_path, "wb") as out:
                    out.write(content)
                kind = _file_kind(ext)
                extracted.append((dest_name, kind, ext))
                raw_entries.append((content, kind, ext))
    except zipfile.BadZipFile:
        logger.warning("Corrupted archive for %s/%s, skipped.", org, name)
    finally:
        if os.path.exists(zip_path):
            os.remove(zip_path)
    return extracted, _primary_content_hash(raw_entries)


def collect(conn, org, search_term, path_filter=None):
    if not GITHUB_TOKEN:
        logger.warning("No GITHUB_TOKEN set; GitHub's unauthenticated rate limit is low.")

    known_projects = {
        row["project_id"] for row in conn.execute("SELECT project_id FROM projects").fetchall()
    }

    logger.info("Scanning org '%s' for '%s' (%d projects already in DB)...", org, search_term, len(known_projects))
    page = 1
    per_page = 100
    total_seen = 0
    skipped_count = 0

    while True:
        response = _github_get(
            f"{GITHUB_API}/search/repositories",
            params={"q": f"org:{org} {search_term}", "per_page": per_page, "page": page},
        )
        if response is None or response.status_code != 200:
            logger.warning("GitHub search API call failed for %s (page %d).", org, page)
            break
        repos = response.json().get("items", [])
        if not repos:
            break

        # Filter known repos first so we never spend a network call listing
        # a tree we're going to skip anyway.
        new_repos = []
        for repo in repos:
            total_seen += 1
            project_id = f"gh_{repo['id']}"
            if project_id in known_projects:
                skipped_count += 1
                continue
            new_repos.append(repo)

        trees = {}
        if new_repos:
            with ThreadPoolExecutor(max_workers=GITHUB_REPO_WORKERS) as executor:
                futures = {
                    executor.submit(_list_repo_tree, org, repo["name"], repo["default_branch"]): repo["id"]
                    for repo in new_repos
                }
                for future in as_completed(futures):
                    trees[futures[future]] = future.result()

        for repo in new_repos:
            project_id = f"gh_{repo['id']}"
            name = repo["name"]

            repo_url = repo["html_url"]
            branch = repo["default_branch"]
            tree, truncated = trees[repo["id"]]
            if tree is None:
                continue

            if truncated:
                extracted, content_hash = _download_repo_zip_fallback(conn, org, name, branch)
            else:
                matches = [
                    item["path"] for item in tree
                    if item["type"] == "blob"
                    and os.path.splitext(item["path"])[1].lower() in config.CAD_EXTENSIONS
                    and (path_filter is None or path_filter.lower() in item["path"].lower())
                ]
                with ThreadPoolExecutor(max_workers=8) as executor:
                    fetched = list(executor.map(
                        lambda path: (path, _fetch_raw_file(org, name, branch, path)), matches
                    ))
                extracted = []
                raw_entries = []
                for path, content in fetched:
                    if content is None:
                        continue
                    ext = os.path.splitext(path)[1].lower()
                    if _is_duplicate(conn, content, ext):
                        continue
                    dest_name = _dest_name(org, name, path)
                    with open(os.path.join(config.USABLE_DIR, dest_name), "wb") as f:
                        f.write(content)
                    kind = _file_kind(ext)
                    extracted.append((dest_name, kind, ext))
                    raw_entries.append((content, kind, ext))
                content_hash = _primary_content_hash(raw_entries)

            status = R.INGESTED if extracted else R.EMPTY
            R.upsert_project(
                conn, project_id, name, f"GitHub_{org}", repo_url,
                content_hash=content_hash, status=status,
            )
            if extracted:
                R.add_files(conn, project_id, extracted)

            known_projects.add(project_id)
            logger.info("%s %s: %d CAD file(s)", "OK" if extracted else "-", name, len(extracted))

        if len(repos) < per_page or page * per_page >= 1000:
            break
        page += 1

    logger.info("GitHub_%s: %d repositories scanned, %d skipped (already in DB).", org, total_seen, skipped_count)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Scan a GitHub org for CAD files.")
    parser.add_argument("--org", required=True)
    parser.add_argument("--search", required=True)
    parser.add_argument("--path-filter", default=None)
    args = parser.parse_args()

    R.init_db(config.DB_PATH)
    conn = R.connect(config.DB_PATH)
    collect(conn, args.org, args.search, args.path_filter)