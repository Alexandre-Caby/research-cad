"""SQLite control plane: project lifecycle state shared by every source/worker."""
import hashlib
import os
import sqlite3
from datetime import datetime, timezone

INGESTED = "ingested"
CLEANED = "cleaned"
VECTORIZED = "vectorized"
EXPORTED = "exported"
EMPTY = "empty"
DUPLICATE = "duplicate"
MANUAL_REVIEW = "manual_review"
ERROR = "error"


def connect(db_path) -> sqlite3.Connection:
    db_path = os.path.abspath(db_path)
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path) -> None:
    conn = connect(db_path)
    try:
        with conn:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    name TEXT,
                    source TEXT,
                    url TEXT,
                    content_hash TEXT,
                    status TEXT,
                    error TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS files (
                    project_id TEXT REFERENCES projects(project_id),
                    filename TEXT,
                    kind TEXT,
                    ext TEXT
                )"""
            )
            conn.execute(
                """CREATE TABLE IF NOT EXISTS components (
                    mpn TEXT PRIMARY KEY,
                    mouser_part_number TEXT,
                    manufacturer TEXT,
                    description TEXT,
                    category TEXT,
                    lifecycle_status TEXT,
                    datasheet_url TEXT,
                    datasheet_path TEXT,
                    attributes_json TEXT,
                    updated_at TEXT
                )"""
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_projects_content_hash ON projects(content_hash)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status)"
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_components_mpn ON components(mpn)")
    finally:
        conn.close()


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def upsert_project(conn, project_id, name, source, url, content_hash=None, status=INGESTED) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute(
            """INSERT INTO projects
            (project_id, name, source, url, content_hash, status, error, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
            ON CONFLICT(project_id) DO UPDATE SET
                name=excluded.name,
                source=excluded.source,
                url=excluded.url,
                content_hash=COALESCE(excluded.content_hash, projects.content_hash),
                status=excluded.status,
                updated_at=excluded.updated_at""",
            (project_id, name, source, url, content_hash, status, now, now),
        )


def add_files(conn, project_id, files: list[tuple[str, str, str]]) -> None:
    with conn:
        conn.execute("DELETE FROM files WHERE project_id = ?", (project_id,))
        conn.executemany(
            "INSERT INTO files (project_id, filename, kind, ext) VALUES (?, ?, ?, ?)",
            [(project_id, filename, kind, ext) for filename, kind, ext in files],
        )


def append_files(conn, project_id, files: list[tuple[str, str, str]]) -> None:
    """Adds derived-artifact file rows without touching a project's existing
    rows. Safe to call multiple times per project (e.g. once per CAD file
    that produces a BOM), unlike add_files's clear-then-add semantics."""
    if not files:
        return
    with conn:
        conn.executemany(
            "INSERT INTO files (project_id, filename, kind, ext) VALUES (?, ?, ?, ?)",
            [(project_id, filename, kind, ext) for filename, kind, ext in files],
        )


def update_status(conn, project_id, status, error=None) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute(
            "UPDATE projects SET status = ?, error = ?, updated_at = ? WHERE project_id = ?",
            (status, error, now, project_id),
        )


def get_by_status(conn, status) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM projects WHERE status = ?", (status,)).fetchall()


def hash_seen(conn, content_hash) -> str | None:
    if content_hash is None:
        return None
    row = conn.execute(
        "SELECT project_id FROM projects WHERE content_hash = ?", (content_hash,)
    ).fetchone()
    return row["project_id"] if row else None


def get_known_identifiers(conn) -> tuple[dict[str, str], dict[str, str]]:
    """Preloads {project_id: status} and {content_hash: project_id} once so
    callers processing many rows (e.g. huggingface.py over ~317GB) can do
    in-memory skip checks instead of one SQL round trip per row."""
    rows = conn.execute("SELECT project_id, status, content_hash FROM projects").fetchall()
    known_statuses = {row["project_id"]: row["status"] for row in rows}
    known_hashes = {row["content_hash"]: row["project_id"] for row in rows if row["content_hash"]}
    return known_statuses, known_hashes

def get_cached_components(conn, mpns: list[str]) -> dict:
    if not mpns:
        return {}
    placeholders = ",".join(["?"] * len(mpns))
    rows = conn.execute(
        f"SELECT * FROM components WHERE mpn IN ({placeholders})", list(mpns)
    ).fetchall()
    return {row["mpn"]: dict(row) for row in rows}


def upsert_component(conn, mpn, mouser_pn, mfr, desc, category, status, ds_url, ds_path, attrs_json) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with conn:
        conn.execute(
            """INSERT INTO components
            (mpn, mouser_part_number, manufacturer, description, category,
             lifecycle_status, datasheet_url, datasheet_path, attributes_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(mpn) DO UPDATE SET
                mouser_part_number=excluded.mouser_part_number,
                manufacturer=excluded.manufacturer,
                description=excluded.description,
                category=excluded.category,
                lifecycle_status=excluded.lifecycle_status,
                datasheet_url=excluded.datasheet_url,
                datasheet_path=COALESCE(excluded.datasheet_path, components.datasheet_path),
                attributes_json=excluded.attributes_json,
                updated_at=excluded.updated_at""",
            (mpn, mouser_pn, mfr, desc, category, status, ds_url, ds_path, attrs_json, now),
        )