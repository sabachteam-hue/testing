import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from database.models import (
    build_database_url,
    check_db_health,
    fallback_database_url,
    init_db,
    run_light_migrations,
    sanitize_database_url,
)
from utils.db_export import export_sqlalchemy_db, export_sqlite_file
from utils.legacy_db import find_legacy_sqlite_files
from utils.storage import (
    get_storage_root,
    get_upload_dir,
    is_persistent_volume_configured,
    resolve_file_path,
)


class TestDatabasePersistence(unittest.TestCase):
    def test_database_url_normalization(self):
        with patch.dict(os.environ, {"DATABASE_URL": "postgres://user:secret@postgres.railway.internal:5432/railway", "DEPLOYMENT_ENV": "development"}):
            url = build_database_url()
            self.assertTrue(url.startswith("postgresql+psycopg://"))
            self.assertIn("user:secret@postgres.railway.internal:5432/railway", url)

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql://user:secret@localhost:5432/testdb", "DEPLOYMENT_ENV": "development"}):
            url = build_database_url()
            self.assertTrue(url.startswith("postgresql+psycopg://"))

        with patch.dict(os.environ, {"DATABASE_URL": "postgresql+psycopg://user:secret@host/db", "DEPLOYMENT_ENV": "development"}):
            url = build_database_url()
            self.assertEqual(url, "postgresql+psycopg://user:secret@host/db")

    def test_sanitize_database_url(self):
        raw = "postgresql+psycopg://myuser:super_secret_pw123@db.railway.internal:5432/railway"
        sanitized = sanitize_database_url(raw)
        self.assertNotIn("super_secret_pw123", sanitized)
        self.assertIn(":***@", sanitized)
        self.assertIn("myuser", sanitized)
        self.assertIn("railway", sanitized)

    def test_production_fails_on_missing_database_url(self):
        """In production, application must fail startup instead of silently using SQLite."""
        with patch.dict(os.environ, {"DEPLOYMENT_ENV": "production", "DATABASE_URL": ""}):
            with self.assertRaises(RuntimeError) as ctx:
                build_database_url()
            self.assertIn("FATAL", str(ctx.exception))
            self.assertIn("PostgreSQL DATABASE_URL is required", str(ctx.exception))

    def test_production_fails_on_sqlite_url(self):
        """In production, SQLite URLs must be rejected."""
        with patch.dict(os.environ, {"DEPLOYMENT_ENV": "production", "DATABASE_URL": "sqlite:///local.db"}):
            with self.assertRaises(RuntimeError) as ctx:
                build_database_url()
            self.assertIn("SQLite is not supported in production", str(ctx.exception))

    def test_dev_allows_sqlite_fallback(self):
        """In development, absence of DATABASE_URL safely falls back to local SQLite."""
        with patch.dict(os.environ, {"DEPLOYMENT_ENV": "development", "DATABASE_URL": ""}):
            url = build_database_url()
            self.assertEqual(url, "sqlite:///./smm_reseller.db")

    def test_migration_idempotency(self):
        """Running migrations repeatedly must be safe and idempotent."""
        init_db()
        # Run light migrations twice to ensure idempotency (no errors on re-run)
        run_light_migrations()
        run_light_migrations()
        self.assertTrue(check_db_health())

    def test_storage_volume_path_resolution(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            with patch.dict(os.environ, {"RAILWAY_VOLUME_MOUNT_PATH": tmp_dir}):
                self.assertTrue(is_persistent_volume_configured())
                root = get_storage_root()
                self.assertEqual(root.resolve(), Path(tmp_dir).resolve())

                services_dir = get_upload_dir("services")
                self.assertTrue(services_dir.is_dir())
                self.assertEqual(services_dir.parent.name, "uploads")

    def test_resolve_file_path(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            test_file = Path(tmp_dir) / "test_sample.png"
            test_file.write_bytes(b"dummy image data")

            # Direct path
            resolved = resolve_file_path(str(test_file))
            self.assertIsNotNone(resolved)
            self.assertEqual(resolved.resolve(), test_file.resolve())

            # Non-existent path returns None
            self.assertIsNone(resolve_file_path("non_existent_file_xyz.png"))

    def test_db_export_utility(self):
        """Test read-only JSON export generates valid metadata and table structures."""
        init_db()
        with tempfile.TemporaryDirectory() as tmp_dir:
            out_file = Path(tmp_dir) / "backup_test.json"
            meta = export_sqlalchemy_db(out_file)

            self.assertTrue(out_file.is_file())
            self.assertIn("total_records", meta)
            self.assertIn("tables_count", meta)

            with out_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
            self.assertIn("metadata", data)
            self.assertIn("tables", data)
            self.assertIn("users", data["tables"])
            self.assertIn("services", data["tables"])

    def test_export_sqlite_file_read_only(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_file = Path(tmp_dir) / "legacy_test.db"
            conn = sqlite3.connect(str(db_file))
            try:
                conn.execute("CREATE TABLE sample (id INT, note TEXT, secret_key TEXT)")
                conn.execute("INSERT INTO sample VALUES (1, 'hello', 'secret123')")
                conn.commit()
            finally:
                conn.close()

            out_file = Path(tmp_dir) / "exported_legacy.json"
            meta = export_sqlite_file(db_file, out_file)

            self.assertEqual(meta["total_records"], 1)
            with out_file.open("r", encoding="utf-8") as f:
                content = json.load(f)
            # Verify secrets are redacted in export
            record = content["tables"]["sample"][0]
            self.assertEqual(record["note"], "hello")
            self.assertEqual(record["secret_key"], "[REDACTED]")

    def test_legacy_sqlite_finder_non_destructive(self):
        """Legacy detection must find files without deleting or modifying them."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_file = Path(tmp_dir) / "smm_reseller.db"
            conn = sqlite3.connect(str(db_file))
            try:
                conn.execute("CREATE TABLE users (id INT, username TEXT)")
                conn.execute("INSERT INTO users VALUES (1, 'admin')")
                conn.commit()
            finally:
                conn.close()

            with patch.dict(os.environ, {"RAILWAY_VOLUME_MOUNT_PATH": tmp_dir}):
                found = find_legacy_sqlite_files()
                self.assertTrue(any(f["has_records"] for f in found))
                # Confirm original file still exists and was not deleted
                self.assertTrue(db_file.is_file())


if __name__ == "__main__":
    unittest.main()
