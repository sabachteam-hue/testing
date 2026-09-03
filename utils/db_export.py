"""Read-only database export utility for backup and inspection.

Features:
- Completely read-only: never modifies, creates, drops, or updates any database tables.
- Exports all key relational models to a structured, human-readable JSON file.
- Supports the active SQLAlchemy database (PostgreSQL or dev SQLite) or an explicit legacy SQLite file.
- Masks API keys and tokens to prevent secret leakage in unencrypted backup files.

Usage:
  # Export active PostgreSQL database
  python -m utils.db_export

  # Export with custom output path
  python -m utils.db_export --output backup_20260903.json

  # Export a legacy SQLite file for archiving
  python -m utils.db_export --sqlite /app/data/smm_reseller.db --output sqlite_archive.json
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def _serialize_val(val):
    if isinstance(val, datetime):
        return val.isoformat()
    return val


def export_sqlalchemy_db(output_path: Path) -> dict:
    """Export the active SQLAlchemy database to JSON."""
    from database.models import (
        Announcement,
        ApiKey,
        AuditLog,
        BotConfig,
        Category,
        IssueReport,
        Language,
        Order,
        PaymentMethod,
        Provider,
        RefundLog,
        Service,
        SessionLocal,
        Stock,
        Transaction,
        User,
    )

    db = SessionLocal()
    try:
        tables_data = {}

        models = [
            ("users", User),
            ("categories", Category),
            ("services", Service),
            ("stocks", Stock),
            ("orders", Order),
            ("transactions", Transaction),
            ("bot_configs", BotConfig),
            ("payment_methods", PaymentMethod),
            ("providers", Provider),
            ("announcements", Announcement),
            ("issue_reports", IssueReport),
            ("refund_logs", RefundLog),
            ("languages", Language),
            ("audit_logs", AuditLog),
            ("api_keys", ApiKey),
        ]

        total_records = 0
        for name, model in models:
            records = []
            try:
                rows = db.query(model).all()
                for row in rows:
                    record = {}
                    for col in row.__table__.columns:
                        val = getattr(row, col.name)
                        # Redact sensitive secrets from export for safety
                        if col.name in ("bot_token", "payfast_secured_key", "secret_key"):
                            record[col.name] = "[REDACTED]"
                        else:
                            record[col.name] = _serialize_val(val)
                    records.append(record)
            except Exception as exc:
                records = [{"_error": str(exc)}]

            tables_data[name] = records
            total_records += len(records)

        payload = {
            "metadata": {
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "database_dialect": db.bind.dialect.name if db.bind else "unknown",
                "total_records": total_records,
                "tables_count": len(tables_data),
            },
            "tables": tables_data,
        }

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

        return payload["metadata"]
    finally:
        db.close()


def export_sqlite_file(sqlite_path: Path, output_path: Path) -> dict:
    """Export a legacy SQLite file to JSON in read-only mode."""
    if not sqlite_path.is_file():
        raise FileNotFoundError(f"SQLite file not found: {sqlite_path}")

    uri = f"file:{sqlite_path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        tables = [r[0] for r in cursor.fetchall()]

        tables_data = {}
        total_records = 0
        for tbl in tables:
            cursor.execute(f'SELECT * FROM "{tbl}"')
            rows = cursor.fetchall()
            records = []
            for row in rows:
                record = {}
                for key in row.keys():
                    val = row[key]
                    if key in ("bot_token", "payfast_secured_key", "secret_key"):
                        record[key] = "[REDACTED]"
                    else:
                        record[key] = val
                records.append(record)
            tables_data[tbl] = records
            total_records += len(records)

        payload = {
            "metadata": {
                "exported_at": datetime.now(timezone.utc).isoformat() if "timezone" in globals() else datetime.utcnow().isoformat(),
                "source_sqlite_file": str(sqlite_path),
                "total_records": total_records,
                "tables_count": len(tables_data),
            },
            "tables": tables_data,
        }

        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

        return payload["metadata"]
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Export SMF SHOP database to read-only JSON backup.")
    parser.add_argument("--sqlite", help="Path to specific SQLite file to export (read-only)")
    parser.add_argument(
        "--output",
        "-o",
        default=f"backup_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json",
        help="Target output JSON path",
    )
    args = parser.parse_args()

    out_file = Path(args.output)
    print(f"Exporting database to {out_file}...")

    if args.sqlite:
        meta = export_sqlite_file(Path(args.sqlite), out_file)
    else:
        meta = export_sqlalchemy_db(out_file)

    print(f"Export completed successfully! Total records exported: {meta.get('total_records')}")
    print(f"Backup saved to: {out_file.resolve()}")


if __name__ == "__main__":
    main()
