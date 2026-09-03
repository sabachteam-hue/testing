"""Security utilities: password hashing, file upload validation, constant-time comparison,
and environment validation for SMF SHOP.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import re
import secrets
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Known placeholder secrets that must never be used in production
INSECURE_SECRETS = {
    "admin123",
    "admin",
    "password",
    "insecure-dev-secret-change-me",
    "dev-secret",
    "change_this_to_random_string_32chars",
    "random_session_secret",
    "your_telegram_bot_token",
    "your_bscscan_api_key",
    "your_tronscan_api_key",
    "your_binance_read_only_api_key",
    "your_binance_read_only_api_secret",
}

# Supported image extensions and MIME prefixes
ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
ALLOWED_BULK_STOCK_EXTENSIONS = {".txt", ".csv", ".xlsx", ".xls", ".pdf", ".docx", ".doc"}
MAX_IMAGE_UPLOAD_BYTES = 5 * 1024 * 1024       # 5 MB
MAX_BULK_STOCK_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB


# =====================================================================
# 1. PASSWORD HASHING & VERIFICATION (Argon2id -> bcrypt -> PBKDF2)
# =====================================================================

def _hash_argon2(password: str) -> str:
    from argon2 import PasswordHasher, Type
    ph = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4, hash_len=32, type=Type.ID)
    return ph.hash(password)


def _hash_bcrypt(password: str) -> str:
    import bcrypt
    salt = bcrypt.gensalt(rounds=12)
    return bcrypt.hashpw(password.encode("utf-8"), salt).decode("utf-8")


def _hash_pbkdf2(password: str) -> str:
    salt = secrets.token_bytes(16)
    iterations = 600000
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return f"$pbkdf2-sha256${iterations}${salt.hex()}${derived.hex()}"


def hash_password(password: str) -> str:
    """Hash password using the most secure available algorithm.
    Priority: Argon2id -> bcrypt -> PBKDF2-HMAC-SHA256 (std lib).
    """
    if not password:
        raise ValueError("Password cannot be empty")
    try:
        return _hash_argon2(password)
    except ImportError:
        pass
    try:
        return _hash_bcrypt(password)
    except ImportError:
        pass
    return _hash_pbkdf2(password)


def _verify_pbkdf2(plain_password: str, hashed_password: str) -> bool:
    try:
        parts = hashed_password.split("$")
        if len(parts) != 5 or parts[1] != "pbkdf2-sha256":
            return False
        iterations = int(parts[2])
        salt = bytes.fromhex(parts[3])
        expected_derived = bytes.fromhex(parts[4])
        actual_derived = hashlib.pbkdf2_hmac("sha256", plain_password.encode("utf-8"), salt, iterations)
        return secrets.compare_digest(expected_derived, actual_derived)
    except Exception:
        return False


def legacy_hash_secret(secret: str) -> str:
    """Legacy SHA-256 + pepper hash, kept purely for backward-compatibility verification."""
    pepper = os.getenv("SECRET_KEY", "dev-secret")
    return hashlib.sha256(f"{secret}:{pepper}".encode("utf-8")).hexdigest()


def verify_password(plain_password: str, hashed_password: str | None) -> tuple[bool, bool]:
    """Verify password against modern or legacy hash.
    Returns:
        (is_valid: bool, needs_rehash: bool)
    When needs_rehash is True, caller should update user.password_hash with hash_password(plain_password).
    """
    if not plain_password or not hashed_password:
        return False, False

    # 1. Argon2id
    if hashed_password.startswith("$argon2"):
        try:
            from argon2 import PasswordHasher
            ph = PasswordHasher()
            ph.verify(hashed_password, plain_password)
            needs_rehash = ph.check_needs_rehash(hashed_password)
            return True, needs_rehash
        except Exception:
            return False, False

    # 2. bcrypt
    if hashed_password.startswith(("$2b$", "$2a$", "$2y$")):
        try:
            import bcrypt
            valid = bcrypt.checkpw(plain_password.encode("utf-8"), hashed_password.encode("utf-8"))
            # If argon2 is installed, rehash bcrypt to argon2id
            needs_rehash = False
            try:
                import argon2  # noqa: F401
                needs_rehash = True
            except ImportError:
                pass
            return valid, (valid and needs_rehash)
        except Exception:
            return False, False

    # 3. PBKDF2-HMAC-SHA256
    if hashed_password.startswith("$pbkdf2-sha256$"):
        valid = _verify_pbkdf2(plain_password, hashed_password)
        # If argon2 or bcrypt is installed, upgrade PBKDF2
        needs_rehash = False
        try:
            import argon2  # noqa: F401
            needs_rehash = True
        except ImportError:
            try:
                import bcrypt  # noqa: F401
                needs_rehash = True
            except ImportError:
                pass
        return valid, (valid and needs_rehash)

    # 4. Legacy 64-character hex SHA-256 + pepper
    if len(hashed_password) == 64 and all(c in "0123456789abcdefABCDEF" for c in hashed_password):
        legacy = legacy_hash_secret(plain_password)
        if secrets.compare_digest(legacy.lower(), hashed_password.lower()):
            # Valid legacy password! Signal transparent rehash to modern algorithm
            return True, True
        return False, False

    return False, False


def constant_time_compare(val1: Optional[str], val2: Optional[str]) -> bool:
    """Safe constant-time comparison for strings."""
    s1 = str(val1 or "")
    s2 = str(val2 or "")
    return secrets.compare_digest(s1, s2)


# =====================================================================
# 2. FILE UPLOAD SECURITY (Type, Size, Content & Safe Names)
# =====================================================================

def safe_upload_filename(prefix: str, original_filename: str) -> str:
    """Generate a sanitized server-side filename, ignoring user-supplied basename."""
    ext = Path(original_filename or "").suffix.lower()
    clean_prefix = re.sub(r"[^a-zA-Z0-9_-]", "", prefix) or "file"
    random_part = secrets.token_hex(8)
    return f"{clean_prefix}_{random_part}{ext}"


def validate_image_upload(
    content: bytes,
    filename: str,
    max_bytes: int = MAX_IMAGE_UPLOAD_BYTES,
) -> tuple[bool, str]:
    """Validate uploaded image: extension, size, and real image structure via Pillow."""
    if not content:
        return False, "File is empty"
    if len(content) > max_bytes:
        return False, f"File size ({len(content)} bytes) exceeds limit of {max_bytes} bytes"

    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_IMAGE_EXTENSIONS:
        return False, f"File extension '{ext}' is not allowed. Allowed: {', '.join(sorted(ALLOWED_IMAGE_EXTENSIONS))}"

    # Verify actual image content
    try:
        from PIL import Image
        with Image.open(io.BytesIO(content)) as img:
            img.verify()
            format_name = (img.format or "").upper()
            allowed_formats = {"PNG", "JPEG", "WEBP", "GIF"}
            if format_name not in allowed_formats:
                return False, f"Unsupported image format: {format_name}"
    except ImportError:
        # Fallback to magic byte verification if Pillow is not installed
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return True, "Valid PNG"
        if content.startswith(b"\xff\xd8\xff"):
            return True, "Valid JPEG"
        if content.startswith(b"GIF87a") or content.startswith(b"GIF89a"):
            return True, "Valid GIF"
        if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
            return True, "Valid WEBP"
        return False, "Unrecognized or invalid image format headers"
    except Exception as exc:
        return False, f"Invalid or corrupted image content: {exc}"

    return True, "Valid"


def validate_bulk_stock_upload(
    content: bytes,
    filename: str,
    max_bytes: int = MAX_BULK_STOCK_UPLOAD_BYTES,
) -> tuple[bool, str]:
    """Validate uploaded stock import file."""
    if not content:
        return False, "File is empty"
    if len(content) > max_bytes:
        return False, f"File size exceeds limit of {max_bytes} bytes"

    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_BULK_STOCK_EXTENSIONS:
        return False, f"File extension '{ext}' is not allowed for stock files"

    return True, "Valid"


# =====================================================================
# 3. ENVIRONMENT & SECRETS VALIDATION
# =====================================================================

def is_production() -> bool:
    env = (os.getenv("DEPLOYMENT_ENV") or os.getenv("ENVIRONMENT") or "").strip().lower()
    if env == "production":
        return True
    if os.getenv("RAILWAY_ENVIRONMENT") and os.getenv("RAILWAY_ENVIRONMENT") != "development":
        return True
    return False


def validate_environment_secrets() -> None:
    """Validate that required secrets are present and safe.
    Fails startup with RuntimeError in production if insecure defaults are used.
    """
    prod = is_production()

    # 1. SECRET_KEY / SESSION_SECRET
    secret_key = (os.getenv("SECRET_KEY") or "").strip()
    session_secret = (os.getenv("SESSION_SECRET") or "").strip() or secret_key

    if prod:
        if not secret_key or secret_key in INSECURE_SECRETS or len(secret_key) < 32:
            raise RuntimeError(
                "FATAL: Insecure or missing SECRET_KEY in production. "
                "Set a unique, random string of at least 32 characters in your environment."
            )
        if not session_secret or session_secret in INSECURE_SECRETS or len(session_secret) < 32:
            raise RuntimeError(
                "FATAL: Insecure or missing SESSION_SECRET in production. "
                "Set a unique, random string of at least 32 characters in your environment."
            )

        # 2. Admin password checks
        admin_hash = (os.getenv("ADMIN_PASSWORD_HASH") or "").strip()
        admin_pass = (os.getenv("ADMIN_PASSWORD") or "").strip()
        if not admin_hash and not admin_pass:
            raise RuntimeError("FATAL: Neither ADMIN_PASSWORD nor ADMIN_PASSWORD_HASH is set in production.")
        if not admin_hash and admin_pass in INSECURE_SECRETS:
            raise RuntimeError(
                f"FATAL: ADMIN_PASSWORD is set to an insecure default ('{admin_pass}') in production. "
                "Change ADMIN_PASSWORD or set ADMIN_PASSWORD_HASH."
            )

        # 3. Bot Token
        bot_token = (os.getenv("BOT_TOKEN") or "").strip()
        if not bot_token or bot_token in INSECURE_SECRETS:
            logger.warning("BOT_TOKEN is missing or placeholder in production.")
    else:
        # Development warnings
        if secret_key in INSECURE_SECRETS or not secret_key:
            logger.warning("[DEV] SECRET_KEY is using a development placeholder. Remember to configure a strong key in production.")
        if (os.getenv("ADMIN_PASSWORD") or "") in INSECURE_SECRETS:
            logger.warning("[DEV] ADMIN_PASSWORD is using a default placeholder ('admin123').")


# =====================================================================
# 4. SENSITIVE DATA REDACTION FOR LOGS
# =====================================================================

_REDACT_PATTERNS = [
    (re.compile(r"(sk_[a-zA-Z0-9_-]{16,})"), "[REDACTED_API_KEY]"),
    (re.compile(r"(\d{8,12}:[a-zA-Z0-9_-]{30,})"), "[REDACTED_BOT_TOKEN]"),
    (re.compile(r"(Bearer\s+)[a-zA-Z0-9_\-\.]{16,}", re.IGNORECASE), r"\1[REDACTED_TOKEN]"),
    (re.compile(r"(password=)[^&\s]+", re.IGNORECASE), r"\1[REDACTED]"),
    (re.compile(r"(admin_pass(?:word)?\s*=\s*)[^\s,]+", re.IGNORECASE), r"\1[REDACTED]"),
]


def redact_sensitive_text(text: str) -> str:
    """Scrub sensitive keys and tokens from a string before logging."""
    if not text:
        return ""
    result = str(text)
    for pattern, replacement in _REDACT_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


class SensitiveDataFilter(logging.Filter):
    """Logging filter to automatically scrub sensitive tokens from all log records."""
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_sensitive_text(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: redact_sensitive_text(str(v)) for k, v in record.args.items()}
            elif isinstance(record.args, (list, tuple)):
                record.args = tuple(redact_sensitive_text(str(a)) for a in record.args)
        return True
