"""Safe detection and reporting of legacy SQLite databases.

Zero-data-loss policy:
- Never delete or overwrite SQLite database files.
- Never auto-migrate or seed over existing SQLite data.
- If PostgreSQL is active and an older SQLite file contains records, report it clearly
  so the administrator can review or export data using the read-only export tool.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


def find_legacy_sqlite_files() -> list[dict]:
    """Scan candidate locations for legacy SQLite database files."""
    candidate_paths: list[Path] = [
        Path("smm_reseller.db"),
        Path("./smm_reseller.db"),
    ]

    volume_mount = os.getenv("RAILWAY_VOLUME_MOUNT_PATH", "").strip()
    if volume_mount:
        candidate_paths.append(Path(volume_mount) / "smm_reseller.db")

    candidate_paths.append(Path("/app/data/smm_reseller.db"))

    results: list[dict] = []
    seen: set[str] = set()

    for p in candidate_paths:
        try:
            resolved = p.resolve()
        except Exception:
            resolved = p
        path_str = str(resolved)
        if path_str in seen:
            continue
        seen.add(path_str)

        if p.is_file() and p.stat().st_size > 0:
            info = {
                "path": path_str,
                "size_bytes": p.stat().st_size,
                "table_counts": {},
                "has_records": False,
            }
            try:
                # Open in read-only URI mode to ensure no modifications can occur
                uri = f"file:{p.as_posix()}?mode=ro"
                conn = sqlite3.connect(uri, uri=True, timeout=2.0)
                try:
                    cursor = conn.cursor()
                    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
                    tables = [row[0] for row in cursor.fetchall() if not row[0].startswith("sqlite_")]
                    total_records = 0
                    for tbl in tables:
                        try:
                            # Table names come from sqlite_master, safe from injection
                            cursor.execute(f'SELECT COUNT(*) FROM "{tbl}"')
                            cnt = cursor.fetchone()[0]
                            info["table_counts"][tbl] = cnt
                            total_records += cnt
                        except Exception:
                            pass
                    info["has_records"] = total_records > 0
                    info["total_records"] = total_records
                finally:
                    conn.close()
            except Exception as exc:
                info["error"] = str(exc)

            results.append(info)

    return results


def check_and_report_legacy_sqlite(is_postgres_active: bool = False) -> None:
    """Check for legacy SQLite databases on startup and log clear diagnostics."""
    legacy_dbs = find_legacy_sqlite_files()
    if not legacy_dbs:
        return

    for db_info in legacy_dbs:
        path = db_info["path"]
        total = db_info.get("total_records", 0)
        has_rec = db_info.get("has_records", False)

        if is_postgres_active and has_rec:
            logger.info(
                "[LEGACY-SQLITE-DETECTED] Found existing SQLite database at '%s' with %d total records across tables %s. "
                "Per zero-data-loss policy, this file is preserved untouched. "
                "PostgreSQL is active as the primary database. "
                "To export SQLite records for reference, run: python -m utils.db_export --sqlite %s",
                path,
                total,
                list(db_info.get("table_counts", {}).keys()),
                path,
            )
        elif is_postgres_active and not has_rec:
            logger.info(
                "[LEGACY-SQLITE-DETECTED] Empty legacy SQLite file detected at '%s' (0 records). "
                "Preserved untouched. PostgreSQL is the primary database.",
                path,
            )
        elif not is_postgres_active:
            logger.info(
                "[DEV-SQLITE-ACTIVE] Active database is SQLite at '%s' (%d records).",
                path,
                total,
            )
