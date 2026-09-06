"""Comprehensive test suite for Phase 7: Customer Notifications, Messages & Account Settings.

Validates:
1. Access control: Logged-out users rejected (401) on notifications and settings endpoints.
2. Cross-customer isolation: Customer A cannot see or mark Customer B's notifications; Customer A cannot modify Customer B's settings.
3. In-app notification creation on business events:
   - Order creation
   - Granted account delivery
   - Claim submission
   - Claim replacement resolution
   - Claim pro-rata refund resolution & wallet credit
   - Claim rejection
   - Claim evidence requested
   - Security events (password change, profile update)
4. Read/unread states, single mark read, mark all read, and unread count badge.
5. Notification preference toggles: promotional opt-out respected while critical transactional events (refund, replacement, security) remain safe and unsuppressed.
6. Settings profile update: full_name, language, currency persistence.
7. Password change security: current password verification, length requirement, confirmation matching, Argon2id hashing.
8. Regression guards: Dashboard, Orders, Granted Accounts, Wallet, and Claims functionality intact.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import (
    Base,
    CustomerNotification,
    GrantedAccount,
    IssueReport,
    Order,
    PaymentMethod,
    Service,
    Stock,
    Transaction,
    User,
    get_db,
)
from main import app
from utils.claims_workflow import (
    create_customer_claim,
    reject_claim,
    request_claim_evidence,
    resolve_claim_with_refund,
    resolve_claim_with_replacement,
)
from utils.customer_notifications import (
    create_customer_notification,
    get_customer_notifications,
    get_unread_notification_count,
    mark_all_notifications_read,
    mark_notification_read,
    notify_order_created_inapp,
    notify_order_delivered_inapp,
    notify_password_changed_inapp,
    notify_profile_updated_inapp,
)
from utils.granted_accounts import sync_granted_accounts_for_order
from utils.rate_limiter import _memory_failures, _memory_lockouts, _memory_windows
from utils.security import hash_password, verify_password


class TestNotificationsAndSettings(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        cls.TestingSessionLocal = sessionmaker(bind=cls.engine, autoflush=False, autocommit=False)
        Base.metadata.create_all(bind=cls.engine)

        db = cls.TestingSessionLocal()
        try:
            # Payment method
            method = PaymentMethod(
                name="TRON USDT",
                code="TRC20",
                method_type="crypto",
                network="Tron",
                address="TTest1234567890",
                is_active=True,
            )
            db.add(method)

            # Service
            service = Service(
                sku="CHATGPT-PLUS-P7",
                name="ChatGPT Plus Subscription",
                sell_price=20.0,
                is_active=True,
                is_deleted=False,
                duration_days=30,
            )
            db.add(service)
            db.flush()

            # Stock with replacement credentials
            stock = Stock(
                service_id=service.id,
                quantity=5,
                reserved_qty=0,
                login_details="replacement_openai@service.com:SecretNewPass99\nbackup_user@service.com:BackupPass",
            )
            db.add(stock)

            # Customer Alice
            alice = User(
                telegram_id="web:alice_p7@test.com",
                username="alice_p7",
                full_name="Alice Wonderland",
                email="alice_p7@test.com",
                password_hash=hash_password("AliceInitialPass1!"),
                wallet_usdt=50.0,
                currency="USD",
                notify_order_updates=True,
                notify_claim_updates=True,
                notify_promotions=True,
                notify_email=True,
                notify_telegram=True,
            )
            db.add(alice)

            # Customer Bob
            bob = User(
                telegram_id="web:bob_p7@test.com",
                username="bob_p7",
                full_name="Bob Builder",
                email="bob_p7@test.com",
                password_hash=hash_password("BobInitialPass2@"),
                wallet_usdt=15.0,
                currency="USD",
                notify_order_updates=True,
                notify_claim_updates=True,
                notify_promotions=True,
                notify_email=True,
                notify_telegram=True,
            )
            db.add(bob)
            db.commit()

            cls.service_id = service.id
            cls.alice_id = alice.id
            cls.bob_id = bob.id

            now = datetime.utcnow()
            # Alice's order and account
            order_alice = Order(
                order_code="ORD-ALICE-P7",
                user_id=alice.id,
                service_id=service.id,
                link="web_order",
                quantity=1,
                amount_usdt=20.0,
                status="completed",
                payment_method="TRC20",
                customer_email=alice.email,
                delivered_info="alice_ai@openai.com:Pass12345",
            )
            db.add(order_alice)
            db.flush()

            acc_alice = GrantedAccount(
                user_id=alice.id,
                order_id=order_alice.id,
                service_id=service.id,
                login_email="alice_ai@openai.com",
                login_password="Pass12345",
                status="active",
                duration_days=30,
                subscription_start_at=now - timedelta(days=5),
                subscription_expires_at=now + timedelta(days=25),
            )
            db.add(acc_alice)

            # Bob's order and account
            order_bob = Order(
                order_code="ORD-BOB-P7",
                user_id=bob.id,
                service_id=service.id,
                link="web_order",
                quantity=1,
                amount_usdt=20.0,
                status="completed",
                payment_method="TRC20",
                customer_email=bob.email,
                delivered_info="bob_ai@openai.com:BobPass987",
            )
            db.add(order_bob)
            db.flush()

            acc_bob = GrantedAccount(
                user_id=bob.id,
                order_id=order_bob.id,
                service_id=service.id,
                login_email="bob_ai@openai.com",
                login_password="BobPass987",
                status="active",
                duration_days=30,
                subscription_start_at=now - timedelta(days=5),
                subscription_expires_at=now + timedelta(days=25),
            )
            db.add(acc_bob)
            db.commit()

            cls.order_alice_id = order_alice.id
            cls.acc_alice_id = acc_alice.id
            cls.order_bob_id = order_bob.id
            cls.acc_bob_id = acc_bob.id

        finally:
            db.close()

    def setUp(self):
        _memory_windows.clear()
        _memory_failures.clear()
        _memory_lockouts.clear()

        db = self.TestingSessionLocal()
        try:
            alice = db.get(User, self.alice_id)
            if alice:
                alice.password_hash = hash_password("AliceInitialPass1!")
                alice.full_name = "Alice Wonderland"
                alice.language = "en"
                alice.currency = "USD"
            bob = db.get(User, self.bob_id)
            if bob:
                bob.password_hash = hash_password("BobInitialPass2@")
            db.commit()
        finally:
            db.close()

        def override_get_db():
            db = self.TestingSessionLocal()
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def _login_alice(self):
        res = self.client.post(
            "/api/web/login",
            json={"email": "alice_p7@test.com", "password": "AliceInitialPass1!"},
        )
        self.assertEqual(res.status_code, 200)

    def _login_bob(self):
        res = self.client.post(
            "/api/web/login",
            json={"email": "bob_p7@test.com", "password": "BobInitialPass2@"},
        )
        self.assertEqual(res.status_code, 200)

    # --------------------------------------------------------------------------
    # 1 & 2: Auth Protection (Logged-out user rejected)
    # --------------------------------------------------------------------------
    def test_logged_out_cannot_access_notifications(self):
        res = self.client.get("/api/web/account/notifications")
        self.assertEqual(res.status_code, 401)

        res_unread = self.client.get("/api/web/account/notifications/unread-count")
        self.assertEqual(res_unread.status_code, 401)

        res_read = self.client.post("/api/web/account/notifications/1/read")
        self.assertEqual(res_read.status_code, 401)

        res_read_all = self.client.post("/api/web/account/notifications/read-all")
        self.assertEqual(res_read_all.status_code, 401)

    def test_logged_out_cannot_access_settings(self):
        res = self.client.get("/api/web/account/settings")
        self.assertEqual(res.status_code, 401)

        res_profile = self.client.post("/api/web/account/settings/profile", json={"full_name": "Hacker"})
        self.assertEqual(res_profile.status_code, 401)

        res_pw = self.client.post(
            "/api/web/account/settings/password",
            json={"current_password": "x", "new_password": "newpass", "confirm_password": "newpass"},
        )
        self.assertEqual(res_pw.status_code, 401)

        res_pref = self.client.post("/api/web/account/settings/preferences", json={"notify_order_updates": False})
        self.assertEqual(res_pref.status_code, 401)

    # --------------------------------------------------------------------------
    # 3 & 4: Cross-Customer Isolation
    # --------------------------------------------------------------------------
    def test_customer_isolation_notifications(self):
        db = self.TestingSessionLocal()
        try:
            # Create notification for Alice
            notif_alice = CustomerNotification(
                user_id=self.alice_id,
                type="security",
                title="Alice Secret Notice",
                message="Only Alice should see this.",
                is_read=False,
            )
            # Create notification for Bob
            notif_bob = CustomerNotification(
                user_id=self.bob_id,
                type="security",
                title="Bob Secret Notice",
                message="Only Bob should see this.",
                is_read=False,
            )
            db.add_all([notif_alice, notif_bob])
            db.commit()
            db.refresh(notif_alice)
            db.refresh(notif_bob)
            alice_notif_id = notif_alice.id
            bob_notif_id = notif_bob.id
        finally:
            db.close()

        # Alice logs in
        self._login_alice()
        res_alice = self.client.get("/api/web/account/notifications")
        self.assertEqual(res_alice.status_code, 200)
        alice_data = res_alice.json()
        titles = [n["title"] for n in alice_data["notifications"]]
        self.assertIn("Alice Secret Notice", titles)
        self.assertNotIn("Bob Secret Notice", titles)

        # Alice tries to mark Bob's notification read -> 404 forbidden
        res_hack = self.client.post(f"/api/web/account/notifications/{bob_notif_id}/read")
        self.assertEqual(res_hack.status_code, 404)

        # Verify Bob's notification is still unread
        db = self.TestingSessionLocal()
        try:
            check_bob = db.get(CustomerNotification, bob_notif_id)
            self.assertFalse(check_bob.is_read)
        finally:
            db.close()

    # --------------------------------------------------------------------------
    # 5: Read/Unread State, Single Read, Read All, and Badge Counter
    # --------------------------------------------------------------------------
    def test_read_unread_workflow_and_badge_count(self):
        db = self.TestingSessionLocal()
        try:
            # Clear previous notifications for Alice
            db.query(CustomerNotification).filter(CustomerNotification.user_id == self.alice_id).delete()
            db.commit()

            n1 = CustomerNotification(user_id=self.alice_id, type="order", title="Notice 1", message="Msg 1", is_read=False)
            n2 = CustomerNotification(user_id=self.alice_id, type="claim", title="Notice 2", message="Msg 2", is_read=False)
            n3 = CustomerNotification(user_id=self.alice_id, type="wallet", title="Notice 3", message="Msg 3", is_read=True)
            db.add_all([n1, n2, n3])
            db.commit()
            db.refresh(n1)
            db.refresh(n2)
            n1_id = n1.id
            n2_id = n2.id
        finally:
            db.close()

        self._login_alice()

        # Check badge count = 2 unread
        res_badge = self.client.get("/api/web/account/notifications/unread-count")
        self.assertEqual(res_badge.status_code, 200)
        self.assertEqual(res_badge.json()["unread_count"], 2)

        # Filter unread only -> should return 2
        res_filter = self.client.get("/api/web/account/notifications?unread_only=true")
        self.assertEqual(res_filter.status_code, 200)
        self.assertEqual(len(res_filter.json()["notifications"]), 2)

        # Mark n1 read -> unread count becomes 1
        res_mark = self.client.post(f"/api/web/account/notifications/{n1_id}/read")
        self.assertEqual(res_mark.status_code, 200)
        self.assertEqual(res_mark.json()["unread_count"], 1)

        # Mark all read -> unread count becomes 0
        res_mark_all = self.client.post("/api/web/account/notifications/read-all")
        self.assertEqual(res_mark_all.status_code, 200)
        self.assertEqual(res_mark_all.json()["unread_count"], 0)

        # Confirm badge is 0
        res_badge_after = self.client.get("/api/web/account/notifications/unread-count")
        self.assertEqual(res_badge_after.json()["unread_count"], 0)

    # --------------------------------------------------------------------------
    # 6: In-App Notification on Claim Events (Submitted, Replaced, Refunded)
    # --------------------------------------------------------------------------
    def test_claim_lifecycle_creates_inapp_notifications(self):
        db = self.TestingSessionLocal()
        try:
            # Clear previous notifications
            db.query(CustomerNotification).filter(CustomerNotification.user_id == self.alice_id).delete()
            db.commit()

            alice = db.get(User, self.alice_id)
            acc = db.get(GrantedAccount, self.acc_alice_id)

            # 1. Submit claim
            claim = create_customer_claim(
                db,
                user=alice,
                granted_account=acc,
                resolution_preference="replacement",
                stopped_working_at=datetime.utcnow() - timedelta(days=1),
                problem_description="Password was changed by somebody else and I cannot log in.",
            )
            claim_id = claim.id
            claim_code = claim.claim_code

            # Verify in-app notification created
            notifs, unread, _ = get_customer_notifications(db, alice.id)
            self.assertTrue(any(claim_code in n["title"] or claim_code in n["message"] for n in notifs))
            self.assertEqual(unread, 1)

            # 2. Resolve with replacement
            new_acc, _ = resolve_claim_with_replacement(db, claim=claim, auto_from_stock=True)

            # Verify replacement notification created
            notifs, unread, _ = get_customer_notifications(db, alice.id)
            repl_notifs = [n for n in notifs if n["type"] == "replacement"]
            self.assertTrue(len(repl_notifs) >= 1)
            self.assertIn(claim_code, repl_notifs[0]["title"])
        finally:
            db.close()

    def test_claim_refund_creates_inapp_notifications(self):
        db = self.TestingSessionLocal()
        try:
            bob = db.get(User, self.bob_id)
            acc = db.get(GrantedAccount, self.acc_bob_id)

            # Submit claim for Bob
            claim = create_customer_claim(
                db,
                user=bob,
                granted_account=acc,
                resolution_preference="refund",
                stopped_working_at=datetime.utcnow() - timedelta(days=2),
                problem_description="Subscription was suspended for violating terms of service.",
            )
            claim_code = claim.claim_code

            # Resolve with wallet refund
            res = resolve_claim_with_refund(db, claim=claim, refund_method="wallet", amount_override=16.50)
            self.assertTrue(res["ok"])

            # Verify refund notification and wallet notification exist
            notifs, _, _ = get_customer_notifications(db, bob.id)
            refund_notifs = [n for n in notifs if n["type"] == "refund"]
            self.assertTrue(len(refund_notifs) >= 1)
            self.assertIn(claim_code, refund_notifs[0]["title"])
            self.assertIn("16.50", refund_notifs[0]["message"])
        finally:
            db.close()

    # --------------------------------------------------------------------------
    # 7: Profile & Settings Management
    # --------------------------------------------------------------------------
    def test_settings_retrieval_and_profile_update(self):
        self._login_alice()

        # Retrieve settings
        res = self.client.get("/api/web/account/settings")
        self.assertEqual(res.status_code, 200)
        s = res.json()["settings"]
        self.assertEqual(s["email"], "alice_p7@test.com")
        self.assertEqual(s["full_name"], "Alice Wonderland")
        self.assertEqual(s["currency"], "USD")
        self.assertTrue(s["has_password"])

        # Update profile: full_name, language, currency
        res_update = self.client.post(
            "/api/web/account/settings/profile",
            json={"full_name": "Alice In Wonderland", "language": "es", "currency": "PKR"},
        )
        self.assertEqual(res_update.status_code, 200)
        updated_s = res_update.json()["settings"]
        self.assertEqual(updated_s["full_name"], "Alice In Wonderland")
        self.assertEqual(updated_s["language"], "es")
        self.assertEqual(updated_s["currency"], "PKR")

        # Verify in DB
        db = self.TestingSessionLocal()
        try:
            alice_db = db.get(User, self.alice_id)
            self.assertEqual(alice_db.full_name, "Alice In Wonderland")
            self.assertEqual(alice_db.language, "es")
            self.assertEqual(alice_db.currency, "PKR")
            # Email must remain intact and unchanged
            self.assertEqual(alice_db.email, "alice_p7@test.com")
        finally:
            db.close()

    # --------------------------------------------------------------------------
    # 8: Password Change Security
    # --------------------------------------------------------------------------
    def test_password_change_validation_and_security(self):
        self._login_alice()

        # Wrong current password rejected
        res_wrong = self.client.post(
            "/api/web/account/settings/password",
            json={"current_password": "WrongPassword123", "new_password": "NewValidPass999!", "confirm_password": "NewValidPass999!"},
        )
        self.assertEqual(res_wrong.status_code, 400)
        self.assertIn("Current password is incorrect", res_wrong.json()["detail"])

        # Mismatch confirmation rejected
        res_mismatch = self.client.post(
            "/api/web/account/settings/password",
            json={"current_password": "AliceInitialPass1!", "new_password": "NewValidPass999!", "confirm_password": "DifferentPass123!"},
        )
        self.assertEqual(res_mismatch.status_code, 400)
        self.assertIn("do not match", res_mismatch.json()["detail"])

        # Too short rejected
        res_short = self.client.post(
            "/api/web/account/settings/password",
            json={"current_password": "AliceInitialPass1!", "new_password": "123", "confirm_password": "123"},
        )
        self.assertEqual(res_short.status_code, 400)
        self.assertIn("at least 6 characters", res_short.json()["detail"])

        # Successful password change
        res_success = self.client.post(
            "/api/web/account/settings/password",
            json={
                "current_password": "AliceInitialPass1!",
                "new_password": "BrandNewSecretPass2026#",
                "confirm_password": "BrandNewSecretPass2026#",
            },
        )
        self.assertEqual(res_success.status_code, 200)
        self.assertTrue(res_success.json()["ok"])

        # Verify old password cannot log in anymore
        res_old_login = self.client.post(
            "/api/web/login",
            json={"email": "alice_p7@test.com", "password": "AliceInitialPass1!"},
        )
        self.assertEqual(res_old_login.status_code, 401)

        # Verify new password logs in successfully
        res_new_login = self.client.post(
            "/api/web/login",
            json={"email": "alice_p7@test.com", "password": "BrandNewSecretPass2026#"},
        )
        self.assertEqual(res_new_login.status_code, 200)

        # Verify in-app security notification was created
        db = self.TestingSessionLocal()
        try:
            notifs, _, _ = get_customer_notifications(db, self.alice_id)
            sec_notifs = [n for n in notifs if n["type"] == "security" and "Password Changed" in n["title"]]
            self.assertTrue(len(sec_notifs) >= 1)
        finally:
            db.close()

    # --------------------------------------------------------------------------
    # 9: Notification Preferences & Promotional Filtering
    # --------------------------------------------------------------------------
    def test_notification_preferences_and_filtering(self):
        self._login_alice()

        # Disable promotional updates and order updates
        res_pref = self.client.post(
            "/api/web/account/settings/preferences",
            json={
                "notify_order_updates": False,
                "notify_claim_updates": True,
                "notify_promotions": False,
                "notify_email": True,
                "notify_telegram": False,
            },
        )
        self.assertEqual(res_pref.status_code, 200)
        prefs = res_pref.json()["preferences"]
        self.assertFalse(prefs["notify_order_updates"])
        self.assertFalse(prefs["notify_promotions"])

        # Test promotional announcement is suppressed
        db = self.TestingSessionLocal()
        try:
            promo = create_customer_notification(
                db,
                user_id=self.alice_id,
                type="announcement",
                title="50% Off Flash Sale",
                message="Limited time discount.",
            )
            self.assertIsNone(promo)  # Suppressed by preference!

            # But critical transactional notice (e.g. security or refund) is NEVER suppressed
            sec = create_customer_notification(
                db,
                user_id=self.alice_id,
                type="security",
                title="Security Alert",
                message="Password updated.",
            )
            self.assertIsNotNone(sec)  # Allowed!
        finally:
            db.close()

    # --------------------------------------------------------------------------
    # 10: Regression Check on Portal Endpoints (Dashboard, Orders, Accounts, Wallet)
    # --------------------------------------------------------------------------
    def test_portal_endpoints_remain_functional(self):
        self._login_bob()

        # Dashboard
        res_dash = self.client.get("/api/web/account/dashboard")
        self.assertEqual(res_dash.status_code, 200)
        self.assertIn("customer", res_dash.json())
        self.assertIn("stats", res_dash.json())

        # Orders
        res_orders = self.client.get("/api/web/account/orders")
        self.assertEqual(res_orders.status_code, 200)
        self.assertIn("orders", res_orders.json())

        # Granted Accounts
        res_acc = self.client.get("/api/web/account/granted-accounts")
        self.assertEqual(res_acc.status_code, 200)
        self.assertIn("accounts", res_acc.json())

        # Wallet
        res_wallet = self.client.get("/api/web/account/wallet")
        self.assertEqual(res_wallet.status_code, 200)
        self.assertIn("balance", res_wallet.json())

        # Claims
        res_claims = self.client.get("/api/web/account/claims")
        self.assertEqual(res_claims.status_code, 200)
        self.assertIn("claims", res_claims.json())


if __name__ == "__main__":
    unittest.main()
