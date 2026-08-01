"""QC stats over the registry: project counts by status/source, file counts by kind."""

from core import config
from core import registry as R
from core.log import get_logger

logger = get_logger(__name__)


def _counts(conn, table, column) -> dict:
    rows = conn.execute(f"SELECT {column}, COUNT(*) AS n FROM {table} GROUP BY {column}").fetchall()
    return {row[column]: row["n"] for row in rows}


def run_report(conn=None) -> dict:
    if conn is None:
        conn = R.connect(config.DB_PATH)

    total = conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"]
    by_status = _counts(conn, "projects", "status")
    by_source = _counts(conn, "projects", "source")
    files_by_kind = _counts(conn, "files", "kind")

    out = {
        "total": total,
        "by_status": by_status,
        "by_source": by_source,
        "files_by_kind": files_by_kind,
    }
    logger.info(
        "QC report: total=%d by_status=%s by_source=%s files_by_kind=%s",
        total, by_status, by_source, files_by_kind,
    )
    return out


if __name__ == "__main__":
    conn = R.connect(config.DB_PATH)
    report = run_report(conn)
    for key, value in report.items():
        if isinstance(value, dict):
            print(key)
            for sub_key, count in value.items():
                print(f"  {str(sub_key):<20} {count}")
        else:
            print(f"{key:<20} {value}")
