"""Unified ingestion pipeline for CAD schematic sources."""

import os
import sys

# 1. Définir le cache HF tout en haut AVANT tout import Hugging Face
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
STORAGE_DIR = os.path.join(ROOT_DIR, "storage")
RAW_DIR = os.path.join(STORAGE_DIR, "1_raw_data")
HF_CACHE_DIR = os.path.join(RAW_DIR, "hf_cache")

os.makedirs(HF_CACHE_DIR, exist_ok=True)
os.environ["HF_HOME"] = HF_CACHE_DIR
os.environ["HF_DATASETS_CACHE"] = os.path.join(HF_CACHE_DIR, "datasets")
os.environ["HUGGINGFACE_HUB_CACHE"] = os.path.join(HF_CACHE_DIR, "hub")
os.environ["HF_HUB_CACHE"] = os.path.join(HF_CACHE_DIR, "hub")
os.environ["HF_ASSETS_CACHE"] = os.path.join(HF_CACHE_DIR, "assets")

# 2. Imports standard & Hugging Face
import time
import zipfile
import gc
import shutil
import json
import io
import requests
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed
from huggingface_hub import HfApi

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
STORAGE_DIR = os.path.join(ROOT_DIR, "storage")

RAW_DIR = os.path.join(STORAGE_DIR, "1_raw_data")
REGISTRY_DIR = os.path.join(STORAGE_DIR, "2_register_data")
USABLE_DIR = os.path.join(STORAGE_DIR, "3_exploitable_data")

for _dir in (RAW_DIR, REGISTRY_DIR, USABLE_DIR):
    os.makedirs(_dir, exist_ok=True)

REGISTRY_PATH = os.path.join(REGISTRY_DIR, "cad_registry.csv")

CAD_EXTENSIONS = {".sch", ".brd", ".kicad_sch", ".kicad_pcb", ".schdoc", ".pcbdoc"}

GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")


def init_registry():
    if os.path.exists(REGISTRY_PATH):
        return pd.read_csv(REGISTRY_PATH)
    return pd.DataFrame(columns=[
        "project_id", "project_name", "source", "url",
        "extracted_files", "processing_status",
    ])


def save_registry(df):
    df.to_csv(REGISTRY_PATH, index=False)


def register_project(df, project_id, project_name, source, url, extracted_files,
                      status_override=None):
    status = status_override or ("Pending" if extracted_files else "Empty")
    row = {
        "project_id": project_id,
        "project_name": project_name,
        "source": source,
        "url": url,
        "extracted_files": ", ".join(extracted_files),
        "processing_status": status,
    }
    return pd.concat([df, pd.DataFrame([row])], ignore_index=True)


def _github_headers():
    headers = {"Accept": "application/vnd.github.v3+json"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    return headers


def _github_get(url, params=None, max_retries=5):
    """GET wrapper that waits out GitHub's rate limit instead of failing."""
    response = None
    for _ in range(max_retries):
        response = requests.get(url, headers=_github_headers(), params=params, timeout=30)
        if response.status_code == 200:
            return response
        if response.status_code in (403, 429) and response.headers.get("X-RateLimit-Remaining") == "0":
            reset_at = int(response.headers.get("X-RateLimit-Reset", time.time() + 60))
            wait_s = max(reset_at - int(time.time()), 1) + 2
            print(f"  ⏳ GitHub rate limit reached, waiting {wait_s}s...")
            time.sleep(min(wait_s, 900))
            continue
        return response
    return response


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
        response = requests.get(url, timeout=30)
    except requests.RequestException:
        return None
    return response.content if response.status_code == 200 else None


def _download_repo_zip_fallback(owner, repo, branch):
    """Fallback for truncated repo trees."""
    url = f"{GITHUB_API}/repos/{owner}/{repo}/zipball/{branch}"
    try:
        response = requests.get(url, headers=_github_headers(), stream=True, timeout=180)
    except requests.RequestException:
        return []
    if response.status_code != 200:
        return []

    zip_path = os.path.join(RAW_DIR, f"{owner}_{repo}.zip")
    with open(zip_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=1 << 20):
            f.write(chunk)

    extracted = []
    try:
        with zipfile.ZipFile(zip_path) as archive:
            for info in archive.infolist():
                ext = os.path.splitext(info.filename)[1].lower()
                if ext in CAD_EXTENSIONS:
                    dest_name = f"{owner}_{repo}_{os.path.basename(info.filename)}"
                    with open(os.path.join(USABLE_DIR, dest_name), "wb") as out:
                        out.write(archive.read(info.filename))
                    extracted.append(dest_name)
    except zipfile.BadZipFile:
        print(f"  ⚠️ Corrupted archive for {owner}/{repo}, skipped.")
    finally:
        os.remove(zip_path)

    return extracted


def collect_github(org, search_term, path_filter=None):
    """Scan repositories for CAD files and copy them into storage."""
    if not GITHUB_TOKEN:
        print("  ⚠️ No GITHUB_TOKEN set; GitHub's unauthenticated rate limit is low.")

    registry = init_registry()
    known_urls = set(registry["url"].dropna())

    print(f"[ingest] Scanning GitHub org '{org}' for '{search_term}'...")
    page = 1
    per_page = 100
    total_seen = 0

    while True:
        response = _github_get(
            f"{GITHUB_API}/search/repositories",
            params={"q": f"org:{org} {search_term}", "per_page": per_page, "page": page},
        )
        if response is None or response.status_code != 200:
            print(f"  ⚠️ GitHub search API call failed for {org} (page {page}).")
            break

        repos = response.json().get("items", [])
        if not repos:
            break

        for repo in repos:
            total_seen += 1
            repo_url = repo["html_url"]
            if repo_url in known_urls:
                continue

            name = repo["name"]
            branch = repo["default_branch"]

            tree, truncated = _list_repo_tree(org, name, branch)
            if tree is None:
                continue

            if truncated:
                extracted = _download_repo_zip_fallback(org, name, branch)
            else:
                matches = [
                    item["path"] for item in tree
                    if item["type"] == "blob"
                    and os.path.splitext(item["path"])[1].lower() in CAD_EXTENSIONS
                    and (path_filter is None or path_filter.lower() in item["path"].lower())
                ]
                extracted = []
                for path in matches:
                    content = _fetch_raw_file(org, name, branch, path)
                    if content is None:
                        continue
                    dest_name = f"{org}_{name}_{os.path.basename(path)}"
                    with open(os.path.join(USABLE_DIR, dest_name), "wb") as f:
                        f.write(content)
                    extracted.append(dest_name)

            registry = register_project(
                registry,
                project_id=f"gh_{repo['id']}",
                project_name=name,
                source=f"GitHub_{org}",
                url=repo_url,
                extracted_files=extracted,
            )
            save_registry(registry)
            print(f"  {'✔' if extracted else '·'} {name}: {len(extracted)} CAD file(s)")

        if len(repos) < per_page or page * per_page >= 1000:
            break
        page += 1

    print(f"[ingest] GitHub_{org}: {total_seen} repositories scanned, "
          f"{len(known_urls)} already known before this run.")


HF_DATASET_ID = "bshada/open-schematics"

HF_PARQUET_CHECKPOINT = os.path.join(REGISTRY_DIR, "hf_parquets_checkpoint.json")
HF_CACHE_DIR = os.path.join(RAW_DIR, "hf_cache")


def _configure_hf_cache(cache_root):
    os.makedirs(cache_root, exist_ok=True)
    os.environ["HF_HOME"] = cache_root
    os.environ["HF_DATASETS_CACHE"] = os.path.join(cache_root, "datasets")
    os.environ["HUGGINGFACE_HUB_CACHE"] = os.path.join(cache_root, "hub")
    os.environ["HF_HUB_CACHE"] = os.path.join(cache_root, "hub")
    os.environ["HF_ASSETS_CACHE"] = os.path.join(cache_root, "assets")
    os.environ["XDG_CACHE_HOME"] = cache_root

def _load_parquet_checkpoint():
    if os.path.exists(HF_PARQUET_CHECKPOINT):
        try:
            with open(HF_PARQUET_CHECKPOINT, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()

def _save_parquet_checkpoint(processed_set):
    with open(HF_PARQUET_CHECKPOINT, "w", encoding="utf-8") as f:
        json.dump(list(processed_set), f, indent=2)


def _purge_hf_cache():
    """Remove the temporary Hugging Face cache used for batch downloads."""
    if os.path.exists(HF_CACHE_DIR):
        try:
            shutil.rmtree(HF_CACHE_DIR)
            print("  ✅ Hugging Face cache cleared.")
        except Exception as e:
            print(f"  ⚠️ Partial Hugging Face cache cleanup: {e}")


def _process_row_worker(args):
    i, row, save_preview_images, usable_dir, dataset_id = args

    raw_name = row.get("name") or f"row_{i}"
    safe_name = raw_name.replace("/", "_")
    project_id = f"hf_{safe_name}"

    schematic = row.get("schematic")
    if not schematic:
        return None

    extracted = []
    extension = row.get("type") or ".kicad_sch"
    sch_filename = f"{safe_name}_schema{extension}"

    try:
        with open(os.path.join(usable_dir, sch_filename), "w", encoding="utf-8") as f:
            f.write(schematic)
        extracted.append(sch_filename)
    except Exception:
        return None

    if save_preview_images and row.get("image") is not None:
        image_filename = f"{safe_name}_schema.png"
        try:
            from PIL import Image as PILImage
            img_data = row["image"]

            if isinstance(img_data, dict) and "bytes" in img_data and img_data["bytes"]:
                img = PILImage.open(io.BytesIO(img_data["bytes"]))
                img.save(os.path.join(usable_dir, image_filename))
                extracted.append(image_filename)
            elif isinstance(img_data, bytes):
                img = PILImage.open(io.BytesIO(img_data))
                img.save(os.path.join(usable_dir, image_filename))
                extracted.append(image_filename)
            elif hasattr(img_data, "save"):
                img_data.save(os.path.join(usable_dir, image_filename))
                extracted.append(image_filename)
        except Exception:
            pass

    return {
        "project_id": project_id,
        "project_name": raw_name,
        "source": "HuggingFace_OpenSchematics",
        "url": f"https://huggingface.co/datasets/{dataset_id}",
        "extracted_files": ", ".join(extracted),
        "processing_status": "Pending" if extracted else "Empty",
    }

def collect_huggingface_in_batches(batch_size_parquets=20, max_workers=16, save_preview_images=True):
    """Process the Hugging Face dataset in Parquet batches."""
    try:
        from datasets import load_dataset, Image as HFImage
    except ImportError:
        print("  ⚠️ The 'datasets' package is required. Skipping.")
        return

    registry = init_registry()
    known_ids = set(registry["project_id"].dropna())
    processed_parquets = _load_parquet_checkpoint()

    print(f"[ingest] Listing Parquet files for {HF_DATASET_ID}...")
    api = HfApi()
    all_files = api.list_repo_files(repo_id=HF_DATASET_ID, repo_type="dataset")
    parquet_files = sorted([f for f in all_files if f.startswith("data/") and f.endswith(".parquet")])

    pending_parquets = [f for f in parquet_files if f not in processed_parquets]
    print(f"[ingest] {len(pending_parquets)} / {len(parquet_files)} Parquet files pending.")

    for idx_batch in range(0, len(pending_parquets), batch_size_parquets):
        current_batch_files = pending_parquets[idx_batch: idx_batch + batch_size_parquets]
        batch_number = idx_batch // batch_size_parquets + 1
        batch_cache_dir = os.path.join(HF_CACHE_DIR, f"batch_{batch_number:05d}")

        _configure_hf_cache(batch_cache_dir)

        print("\n--------------------------------------------------")
        print(f"[ingest] Batch {batch_number}: loading {len(current_batch_files)} Parquet files...")
        print("--------------------------------------------------")

        try:
            dataset = load_dataset(
                HF_DATASET_ID,
                data_files=current_batch_files,
                split="train",
                cache_dir=batch_cache_dir,
            )

            try:
                dataset = dataset.cast_column("image", HFImage(decode=False))
            except Exception:
                try:
                    dataset = dataset.decode(False)
                except Exception:
                    pass

            def _keep_hf_row(schematic):
                return bool(schematic and str(schematic).strip())

            try:
                dataset = dataset.filter(_keep_hf_row, input_columns=["schematic"])
            except Exception as e:
                print(f"  ⚠️ Erreur lors du filtrage du dataset: {e}")

            try:
                dataset = dataset.filter(_keep_hf_row, input_columns=["schematic", "image"])
            except Exception:
                pass

            futures = []
            new_records = []
            processed_in_batch = 0

            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                for i, row in enumerate(dataset):
                    raw_name = row.get("name") or f"row_{i}"
                    safe_name = raw_name.replace("/", "_")
                    project_id = f"hf_{safe_name}"

                    if project_id in known_ids:
                        continue

                    known_ids.add(project_id)

                    future = executor.submit(
                        _process_row_worker,
                        (i, row, save_preview_images, USABLE_DIR, HF_DATASET_ID)
                    )
                    futures.append(future)

                for completed in as_completed(futures):
                    res = completed.result()
                    if res:
                        new_records.append(res)
                        processed_in_batch += 1

            if new_records:
                registry = pd.concat([registry, pd.DataFrame(new_records)], ignore_index=True)
                save_registry(registry)
                print(f"✔️ {processed_in_batch} new schematics extracted in this batch.")

            processed_parquets.update(current_batch_files)
            _save_parquet_checkpoint(processed_parquets)

        finally:
            del dataset
            gc.collect()
            if os.path.exists(batch_cache_dir):
                try:
                    shutil.rmtree(batch_cache_dir)
                    print(f"  🧹 Cache du batch {batch_number} supprimé.")
                except Exception as exc:
                    print(f"  ⚠️ N'a pas pu supprimer le cache du batch {batch_number}: {exc}")
            
            if os.path.exists(HF_CACHE_DIR):
                for item in os.listdir(HF_CACHE_DIR):
                    item_path = os.path.join(HF_CACHE_DIR, item)
                    try:
                        if os.path.isdir(item_path):
                            shutil.rmtree(item_path)
                        else:
                            os.remove(item_path)
                    except Exception:
                        pass

    print("\n[ingest] Hugging Face processing finished.")


OSHWLAB_URL_LIST = os.path.join(ROOT_DIR, "oshwlab_urls.txt")


def collect_oshwlab(url_list_path=OSHWLAB_URL_LIST):
    if not os.path.exists(url_list_path):
        print(f"[ingest] {url_list_path} not found -- create it with one OSHWLab "
              f"project URL per line to enable this source. Skipping.")
        return

    try:
        from bs4 import BeautifulSoup
    except ImportError:
        print("  ⚠️ beautifulsoup4 is required for this source "
              "(pip install beautifulsoup4). Skipping.")
        return

    registry = init_registry()
    known_urls = set(registry["url"].dropna())

    with open(url_list_path, encoding="utf-8") as f:
        urls = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    print(f"[ingest] Processing {len(urls)} OSHWLab URL(s)...")

    for url in urls:
        if url in known_urls:
            continue

        project_slug = url.rstrip("/").split("/")[-1]
        try:
            response = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        except requests.RequestException as exc:
            print(f"  ⚠️ Could not fetch {url}: {exc}")
            continue

        extracted = []
        status = "NeedsManualReview"

        if response.status_code == 200:
            soup = BeautifulSoup(response.text, "html.parser")
            for script in soup.find_all("script"):
                text = script.string or ""
                if '"schematic"' in text or '"docType"' in text:
                    dest_name = f"oshwlab_{project_slug}_state.json"
                    with open(os.path.join(USABLE_DIR, dest_name), "w", encoding="utf-8") as out:
                        out.write(text)
                    extracted.append(dest_name)
                    status = "Pending"
                    break

        registry = register_project(
            registry,
            project_id=f"oshwlab_{project_slug}",
            project_name=project_slug,
            source="OSHWLab",
            url=url,
            extracted_files=extracted,
            status_override=status,
        )
        save_registry(registry)
        print(f"  {'✔' if extracted else '?'} {project_slug}: {status}")


if __name__ == "__main__":
    collect_github("adafruit", "pcb")
    collect_github("sparkfun", "hardware", path_filter="hardware")
    collect_huggingface_in_batches(batch_size_parquets=30, max_workers=16, save_preview_images=True)
    collect_oshwlab()