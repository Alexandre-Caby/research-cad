"""
Clears out storage/1_raw_data, the temporary staging area used only as a
fallback when a GitHub repo's tree listing is truncated (see
_download_repo_zip_fallback in core/sources/github.py). Never touches
3_exploitable_data or 4_extracted_data, which are the permanent, reusable
parts of this dataset.
"""

import argparse
import os
import shutil

from core import config


def run_purge(dry_run=False, assume_yes=False):
    raw_dir = config.RAW_DIR
    if not os.path.exists(raw_dir):
        print("The 1_raw_data staging folder does not exist.")
        return

    entries = os.listdir(raw_dir)
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
        path = os.path.join(raw_dir, name)
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
    parser = argparse.ArgumentParser(description="Clear the temporary raw-data staging area.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = parser.parse_args()
    run_purge(dry_run=args.dry_run, assume_yes=args.yes)
