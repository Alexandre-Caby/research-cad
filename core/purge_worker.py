"""
Clears out storage/1_raw_data, the temporary staging area used only as a
fallback when a GitHub repo's tree listing is truncated (see
_download_repo_zip_fallback in ingest.py). Never touches
3_exploitable_data or 4_extracted_data, which are the permanent, reusable
parts of this dataset.
"""

import os
import shutil
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
RAW_DIR = os.path.abspath(os.path.join(ROOT_DIR, "storage", "1_raw_data"))


def run_purge(dry_run=False, assume_yes=False):
    if not os.path.exists(RAW_DIR):
        print("The 1_raw_data staging folder does not exist.")
        return

    entries = os.listdir(RAW_DIR)
    if not entries:
        print("1_raw_data is already empty.")
        return

    print(f"Contents of 1_raw_data: {entries}")

    if dry_run:
        print(f"[dry-run] Would delete {len(entries)} item(s). Nothing was touched.")
        return

    if not assume_yes:
        answer = input("⚠️ Really clear the temporary staging area (1_raw_data)? (yes/no): ")
        if answer.strip().lower() not in ("yes", "y"):
            print("Purge cancelled.")
            return

    removed = 0
    for name in entries:
        path = os.path.join(RAW_DIR, name)
        try:
            if os.path.isfile(path) or os.path.islink(path):
                os.unlink(path)
            elif os.path.isdir(path):
                shutil.rmtree(path)
            removed += 1
        except Exception as exc:
            print(f"Could not remove {name}: {exc}")

    print(f"🧹 Purge complete. {removed} item(s) removed.")


if __name__ == "__main__":
    run_purge(dry_run="--dry-run" in sys.argv, assume_yes="--yes" in sys.argv)