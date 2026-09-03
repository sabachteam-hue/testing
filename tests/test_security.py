"""Automated security tests for SMF SHOP hardening.
Tests:
- Modern password hashing & verification
- Transparent legacy SHA-256 migration
- Constant-time comparison
- Production secrets validation & fail-safe startup
- File upload validation (type, size, content)
- Rate limiting and temporary lockout
- Sensitive log redaction
"""

import os
import unittest
from unittest.mock import patch

from utils.rate_limiter import (
    check_rate_limit,
    clear_failures,
    is_locked_out,
    record_failure_and_check_lockout,
)
from utils.security import (
    SensitiveDataFilter,
    constant_time_compare,
    hash_password,
    legacy_hash_secret,
    safe_upload_filename,
    validate_bulk_stock_upload,
    validate_image_upload,
    validate_environment_secrets,
    verify_password,
)


class TestPasswordSecurity(unittest.TestCase):
    def test_hash_and_verify_modern_password(self):
        password = "SuperSecretPassword#2026"
        hashed = hash_password(password)

        # Hash must not be plaintext or simple hex
        self.assertNotEqual(password, hashed)
        self.assertTrue(hashed.startswith(("$argon2", "$2b$", "$2a$", "$pbkdf2-sha256$")))

        # Correct password verifies
        valid, needs_rehash = verify_password(password, hashed)
        self.assertTrue(valid)

        # Incorrect password fails
        invalid, _ = verify_password("WrongPassword123", hashed)
        self.assertFalse(invalid)

    def test_transparent_legacy_password_migration(self):
        password = "UserOldPassword123"
        legacy_hash = legacy_hash_secret(password)

        # Legacy hash is 64-char hex
        self.assertEqual(len(legacy_hash), 64)

        # Verifying legacy hash succeeds and signals needs_rehash=True
        valid, needs_rehash = verify_password(password, legacy_hash)
        self.assertTrue(valid)
        self.assertTrue(needs_rehash)

        # Upgrading password hash
        new_hash = hash_password(password)
        self.assertNotEqual(new_hash, legacy_hash)

        # New hash verifies and no longer needs rehash
        valid2, needs_rehash2 = verify_password(password, new_hash)
        self.assertTrue(valid2)
        self.assertFalse(needs_rehash2)

        # Wrong password against legacy hash fails
        invalid, _ = verify_password("BadPassword", legacy_hash)
        self.assertFalse(invalid)


class TestConstantTimeCompare(unittest.TestCase):
    def test_constant_time_compare(self):
        self.assertTrue(constant_time_compare("exact_match_123", "exact_match_123"))
        self.assertFalse(constant_time_compare("exact_match_123", "different_456"))
        self.assertFalse(constant_time_compare(None, "something"))
        self.assertTrue(constant_time_compare("", None))


class TestFileUploadValidation(unittest.TestCase):
    def test_rejects_empty_file(self):
        valid, msg = validate_image_upload(b"", "photo.png")
        self.assertFalse(valid)
        self.assertIn("empty", msg.lower())

    def test_rejects_forbidden_extension(self):
        valid, msg = validate_image_upload(b"malicious content", "script.php")
        self.assertFalse(valid)
        self.assertIn("not allowed", msg.lower())

    def test_rejects_oversized_file(self):
        oversized = b"A" * (6 * 1024 * 1024)  # 6 MB (limit is 5MB)
        valid, msg = validate_image_upload(oversized, "huge.jpg")
        self.assertFalse(valid)
        self.assertIn("exceeds limit", msg.lower())

    def test_rejects_fake_image(self):
        # Disguised text file with .png extension
        fake_png = b"echo 'I am not an image';"
        valid, msg = validate_image_upload(fake_png, "fake.png")
        self.assertFalse(valid)

    def test_accepts_valid_png_image(self):
        # 1x1 valid transparent PNG bytes
        valid_png = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00"
            b"\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        )
        valid, msg = validate_image_upload(valid_png, "real.png")
        self.assertTrue(valid)

    def test_bulk_stock_upload_validation(self):
        self.assertTrue(validate_bulk_stock_upload(b"line1:pass\nline2:pass", "stock.txt")[0])
        self.assertTrue(validate_bulk_stock_upload(b"user,pass", "stock.csv")[0])
        self.assertFalse(validate_bulk_stock_upload(b"user,pass", "malware.exe")[0])

    def test_safe_upload_filename(self):
        clean = safe_upload_filename("test", "../../../etc/passwd.png")
        self.assertTrue(clean.startswith("test_"))
        self.assertTrue(clean.endswith(".png"))
        self.assertNotIn("/", clean)
        self.assertNotIn("..", clean)


class TestEnvironmentValidation(unittest.TestCase):
    def test_production_fails_on_insecure_secret_key(self):
        with patch.dict(os.environ, {
            "DEPLOYMENT_ENV": "production",
            "SECRET_KEY": "insecure-dev-secret-change-me",
            "SESSION_SECRET": "random_session_secret",
            "ADMIN_PASSWORD": "admin123",
        }):
            with self.assertRaises(RuntimeError) as ctx:
                validate_environment_secrets()
            self.assertIn("SECRET_KEY", str(ctx.exception))

    def test_production_fails_on_default_admin_password(self):
        with patch.dict(os.environ, {
            "DEPLOYMENT_ENV": "production",
            "SECRET_KEY": "a" * 32,
            "SESSION_SECRET": "b" * 32,
            "ADMIN_PASSWORD": "admin123",
            "ADMIN_PASSWORD_HASH": "",
        }):
            with self.assertRaises(RuntimeError) as ctx:
                validate_environment_secrets()
            self.assertIn("ADMIN_PASSWORD", str(ctx.exception))

    def test_production_passes_with_strong_secrets(self):
        with patch.dict(os.environ, {
            "DEPLOYMENT_ENV": "production",
            "SECRET_KEY": "c98f7e2a4b1c8d5e7f0a3b2c1d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e",
            "SESSION_SECRET": "d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2",
            "ADMIN_PASSWORD": "StrongAdminPassword#9876",
            "ADMIN_PASSWORD_HASH": "",
        }):
            # Should not raise
            validate_environment_secrets()

    def test_development_allows_default_placeholders(self):
        with patch.dict(os.environ, {
            "DEPLOYMENT_ENV": "development",
            "SECRET_KEY": "insecure-dev-secret-change-me",
            "ADMIN_PASSWORD": "admin123",
        }):
            # Should log warnings but not raise an exception
            validate_environment_secrets()


class TestRateLimiter(unittest.TestCase):
    async def _test_sliding_window(self):
        key = "test_window_key"
        # 3 requests allowed per 10 seconds
        res1, _ = await check_rate_limit(key, limit=3, window_seconds=10)
        res2, _ = await check_rate_limit(key, limit=3, window_seconds=10)
        res3, _ = await check_rate_limit(key, limit=3, window_seconds=10)
        res4, retry_after = await check_rate_limit(key, limit=3, window_seconds=10)

        self.assertTrue(res1)
        self.assertTrue(res2)
        self.assertTrue(res3)
        self.assertFalse(res4)
        self.assertGreater(retry_after, 0)

    async def _test_lockout(self):
        key = "test_lockout_key"
        await clear_failures(key)

        # 3 failures triggers lockout
        for _ in range(2):
            locked, _ = await record_failure_and_check_lockout(key, max_failures=3, window_seconds=60, lockout_seconds=120)
            self.assertFalse(locked)

        # 3rd failure
        locked, retry_after = await record_failure_and_check_lockout(key, max_failures=3, window_seconds=60, lockout_seconds=120)
        self.assertTrue(locked)
        self.assertGreater(retry_after, 0)

        # Subsequent check reports locked
        locked2, _ = await is_locked_out(key)
        self.assertTrue(locked2)

        # Clear failures
        await clear_failures(key)
        locked3, _ = await is_locked_out(key)
        self.assertFalse(locked3)

    def test_rate_limiter_async(self):
        import asyncio
        asyncio.run(self._test_sliding_window())
        asyncio.run(self._test_lockout())


class TestSensitiveDataFilter(unittest.TestCase):
    def test_redacts_bot_token(self):
        filter_ = SensitiveDataFilter()
        import logging
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Starting bot with token 1234567890:ABCdefGHIjklMNOpqrsTUVwxyz_1234567 now.",
            args=(),
            exc_info=None,
        )
        filter_.filter(record)
        self.assertNotIn("1234567890:ABCdefGHIjklMNOpqrsTUVwxyz_1234567", record.msg)
        self.assertIn("[REDACTED_BOT_TOKEN]", record.msg)

    def test_redacts_api_key(self):
        filter_ = SensitiveDataFilter()
        import logging
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="User authenticated using sk_abcdef1234567890abcdef1234 key.",
            args=(),
            exc_info=None,
        )
        filter_.filter(record)
        self.assertNotIn("sk_abcdef1234567890abcdef1234", record.msg)
        self.assertIn("[REDACTED_API_KEY]", record.msg)


class TestAdminAuthVerification(unittest.TestCase):
    def test_verify_against_admin_password_hash(self):
        password = "AdminSuperPass#2026"
        hashed = hash_password(password)
        with patch.dict(os.environ, {
            "ADMIN_USERNAME": "superadmin",
            "ADMIN_PASSWORD_HASH": hashed,
            "ADMIN_PASSWORD": "",
        }):
            expected_user = os.getenv("ADMIN_USERNAME", "admin").strip()
            user_ok = constant_time_compare("superadmin", expected_user)
            self.assertTrue(user_ok)

            pass_ok, _ = verify_password(password, os.getenv("ADMIN_PASSWORD_HASH"))
            self.assertTrue(pass_ok)

            bad_pass, _ = verify_password("WrongPassword", os.getenv("ADMIN_PASSWORD_HASH"))
            self.assertFalse(bad_pass)

    def test_verify_against_plain_admin_password(self):
        password = "AdminPlainPass#2026"
        with patch.dict(os.environ, {
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD_HASH": "",
            "ADMIN_PASSWORD": password,
        }):
            expected_pass = os.getenv("ADMIN_PASSWORD", "")
            self.assertTrue(constant_time_compare(password, expected_pass))
            self.assertFalse(constant_time_compare("WrongPass", expected_pass))


class TestCSRFLogic(unittest.TestCase):
    def test_csrf_token_validation(self):
        import secrets

        session_token = secrets.token_hex(32)
        valid_form_token = session_token
        invalid_form_token = secrets.token_hex(32)

        self.assertTrue(constant_time_compare(session_token, valid_form_token))
        self.assertFalse(constant_time_compare(session_token, invalid_form_token))
        self.assertFalse(constant_time_compare(session_token, None))


if __name__ == "__main__":
    unittest.main()
