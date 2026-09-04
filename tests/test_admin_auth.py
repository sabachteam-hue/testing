import os
import unittest
from unittest.mock import patch

from utils.security import (
    constant_time_compare,
    hash_password,
    normalize_password_hash,
    verify_password,
    _hash_argon2,
    _hash_bcrypt,
    _hash_pbkdf2,
)


def simulate_admin_login(username_input: str, password_input: str) -> bool:
    """Exact authentication logic used by /admin/login in admin/routes.py."""
    expected_user = (os.getenv("ADMIN_USERNAME") or "admin").strip()
    admin_hash = normalize_password_hash(os.getenv("ADMIN_PASSWORD_HASH"))
    admin_pass = (os.getenv("ADMIN_PASSWORD") or "").strip()

    user_matches = constant_time_compare(username_input.strip(), expected_user)
    pass_matches = False
    if admin_hash:
        pass_matches, _ = verify_password(password_input, admin_hash)
        if not pass_matches and password_input.strip() != password_input:
            pass_matches, _ = verify_password(password_input.strip(), admin_hash)

    if not pass_matches and admin_pass:
        pass_matches = constant_time_compare(password_input, admin_pass)
        if not pass_matches and password_input.strip() != password_input:
            pass_matches = constant_time_compare(password_input.strip(), admin_pass)

    return bool(user_matches and pass_matches)


class TestAdminPasswordAuth(unittest.TestCase):
    def setUp(self):
        self.password = "MyComplexAdmin#Password_2026!"
        self.wrong_password = "IncorrectPassword999"

    def test_argon2_hash_authentication(self):
        argon_hash = _hash_argon2(self.password)
        with patch.dict(os.environ, {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD_HASH": argon_hash}):
            self.assertTrue(simulate_admin_login("admin", self.password))
            self.assertFalse(simulate_admin_login("admin", self.wrong_password))
            self.assertFalse(simulate_admin_login("wronguser", self.password))

    def test_bcrypt_hash_authentication(self):
        b_hash = _hash_bcrypt(self.password)
        with patch.dict(os.environ, {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD_HASH": b_hash}):
            self.assertTrue(simulate_admin_login("admin", self.password))
            self.assertFalse(simulate_admin_login("admin", self.wrong_password))

    def test_pbkdf2_hash_authentication(self):
        p_hash = _hash_pbkdf2(self.password)
        with patch.dict(os.environ, {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD_HASH": p_hash}):
            self.assertTrue(simulate_admin_login("admin", self.password))
            self.assertFalse(simulate_admin_login("admin", self.wrong_password))

    def test_railway_quoted_hash_formats(self):
        """Railway or .env variables often contain quotes or shell escape sequences."""
        raw_hash = hash_password(self.password)

        variations = [
            f'"{raw_hash}"',           # double quotes
            f"'{raw_hash}'",           # single quotes
            f"`{raw_hash}`",           # markdown backticks
            f'  "{raw_hash}"  ',       # whitespace and quotes
            raw_hash.replace("$", r"\$"),  # backslash escaped dollar signs
            raw_hash.replace("$", "$$"),   # double dollar escaped
            raw_hash.lstrip("$"),          # missing leading dollar
        ]

        for var_hash in variations:
            with patch.dict(os.environ, {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD_HASH": var_hash}):
                self.assertTrue(
                    simulate_admin_login("admin", self.password),
                    f"Failed to authenticate with variation: {var_hash[:20]}...",
                )
                self.assertFalse(simulate_admin_login("admin", self.wrong_password))

    def test_password_input_with_accidental_whitespace(self):
        raw_hash = hash_password(self.password)
        with patch.dict(os.environ, {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD_HASH": raw_hash}):
            # Browser paste with trailing newline or space
            self.assertTrue(simulate_admin_login("admin", f"{self.password} "))
            self.assertTrue(simulate_admin_login("admin", f" {self.password}"))
            self.assertFalse(simulate_admin_login("admin", " wrong "))

    def test_admin_username_defaulting(self):
        """When ADMIN_USERNAME is empty/omitted, username defaults to 'admin'."""
        raw_hash = hash_password(self.password)
        with patch.dict(os.environ, {"ADMIN_USERNAME": "", "ADMIN_PASSWORD_HASH": raw_hash}):
            self.assertTrue(simulate_admin_login("admin", self.password))
            self.assertTrue(simulate_admin_login(" admin ", self.password))
            self.assertFalse(simulate_admin_login("root", self.password))


if __name__ == "__main__":
    unittest.main()
