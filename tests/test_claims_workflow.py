import io
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import (
    Base,
    Claim,
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
    format_claim_payload,
    reject_claim,
    request_claim_evidence,
    resolve_claim_with_refund,
    resolve_claim_with_replacement,
    resolve_claim_with_support_fix,
)
from utils.granted_accounts import calculate_account_refund_estimate, compute_account_lifecycle
from utils.notifications import (
    notify_claim_approved_replacement,
    notify_claim_evidence_requested,
    notify_claim_refunded,
    notify_claim_rejected,
    notify_claim_resolved_support,
    notify_claim_submitted,
)
from utils.rate_limiter import _memory_failures, _memory_lockouts, _memory_windows
from utils.security import hash_password, validate_claim_evidence_upload


class TestClaimsWorkflow(unittest.TestCase):
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
            # Payment Method
            method = PaymentMethod(
                name="USDT TRC20",
                code="TRC20",
                method_type="crypto",
                network="Tron (TRC20)",
                address="TXYZ1234567890",
                is_active=True,
            )
            db.add(method)

            # Service
            service = Service(
                sku="NETFLIX-4K-TEST",
                name="Netflix 4K Premium UHD",
                sell_price=30.0,
                is_active=True,
                is_deleted=False,
                min_qty=1,
                max_qty=5,
            )
            db.add(service)
            db.flush()

            # Stock with credentials
            stock = Stock(
                service_id=service.id,
                quantity=10,
                reserved_qty=0,
                login_details="replacement_user1@netflix.com:SecretPass123\nreplacement_user2@netflix.com:SecretPass456",
            )
            db.add(stock)

            # User Alice
            alice = User(
                telegram_id="web:alice@example.com",
                username="alice",
                full_name="Alice Smith",
                email="alice@example.com",
                password_hash=hash_password("password123"),
                wallet_usdt=25.0,
            )
            db.add(alice)

            # User Bob
            bob = User(
                telegram_id="web:bob@example.com",
                username="bob",
                full_name="Bob Jones",
                email="bob@example.com",
                password_hash=hash_password("password456"),
                wallet_usdt=10.0,
            )
            db.add(bob)
            db.commit()

            cls.service_id = service.id
            cls.alice_id = alice.id
            cls.bob_id = bob.id

            now = datetime.utcnow()
            start_date = now - timedelta(days=10)
            expiry_date = now + timedelta(days=20)

            # Alice's Order 1
            order_alice = Order(
                order_code="ORD-ALICE-001",
                user_id=alice.id,
                service_id=service.id,
                link="web_order",
                quantity=1,
                amount_usdt=30.0,
                status="completed",
                payment_method="TRC20",
            )
            db.add(order_alice)
            db.flush()

            # Alice's Active Granted Account
            acc_alice = GrantedAccount(
                user_id=alice.id,
                order_id=order_alice.id,
                service_id=service.id,
                login_email="alice_netflix@premium.com",
                login_password="OriginalPassword999",
                status="active",
                subscription_start_at=start_date,
                subscription_expires_at=expiry_date,
                duration_days=30,
            )
            db.add(acc_alice)

            # Bob's Order
            order_bob = Order(
                order_code="ORD-BOB-001",
                user_id=bob.id,
                service_id=service.id,
                link="web_order",
                quantity=1,
                amount_usdt=30.0,
                status="completed",
                payment_method="TRC20",
            )
            db.add(order_bob)
            db.flush()

            # Bob's Active Granted Account
            acc_bob = GrantedAccount(
                user_id=bob.id,
                order_id=order_bob.id,
                service_id=service.id,
                login_email="bob_netflix@premium.com",
                login_password="BobPassword123",
                status="active",
                subscription_start_at=start_date,
                subscription_expires_at=expiry_date,
                duration_days=30,
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
            json={"email": "alice@example.com", "password": "password123"},
        )
        self.assertEqual(res.status_code, 200)

    def _login_admin(self):
        with patch.dict(os.environ, {"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": "adminpassword"}):
            res = self.client.post(
                "/admin/login",
                data={"username": "admin", "password": "adminpassword"},
                follow_redirects=False,
            )
            self.assertIn(res.status_code, [200, 302, 303])

    # 1. Logged-out user cannot access claims
    def test_01_logged_out_user_cannot_access_claims(self):
        res = self.client.get("/api/web/account/claims")
        self.assertEqual(res.status_code, 401)

    # 2. Customer cannot submit claim for another customer's account
    def test_02_customer_cannot_claim_another_account(self):
        self._login_alice()
        res = self.client.post(
            "/api/web/account/claims",
            json={
                "granted_account_id": self.acc_bob_id,
                "resolution_preference": "replacement",
                "stopped_working_at": datetime.utcnow().strftime("%Y-%m-%d"),
                "description": "I am trying to claim Bob's account credentials.",
            },
        )
        self.assertEqual(res.status_code, 403)

    # 3. Future date is rejected
    def test_03_future_stopped_working_date_is_rejected(self):
        self._login_alice()
        future_date = (datetime.utcnow() + timedelta(days=5)).strftime("%Y-%m-%d")
        res = self.client.post(
            "/api/web/account/claims",
            json={
                "granted_account_id": self.acc_alice_id,
                "resolution_preference": "replacement",
                "stopped_working_at": future_date,
                "description": "It stopped working in the future mysteriously.",
            },
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("future", res.json()["detail"].lower())

    # 4. Date before subscription start is rejected
    def test_04_date_before_subscription_start_is_rejected(self):
        self._login_alice()
        past_date = (datetime.utcnow() - timedelta(days=60)).strftime("%Y-%m-%d")
        res = self.client.post(
            "/api/web/account/claims",
            json={
                "granted_account_id": self.acc_alice_id,
                "resolution_preference": "replacement",
                "stopped_working_at": past_date,
                "description": "It stopped working before I even bought the account.",
            },
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("subscription start date", res.json()["detail"].lower())

    # 5. Customer can submit claim for own active account & account freezes
    def test_05_valid_claim_submission_freezes_account(self):
        self._login_alice()
        today = datetime.utcnow().strftime("%Y-%m-%d")
        res = self.client.post(
            "/api/web/account/claims",
            json={
                "granted_account_id": self.acc_alice_id,
                "resolution_preference": "replacement",
                "stopped_working_at": today,
                "description": "Password was changed by Netflix. Screen shows invalid password error.",
            },
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["ok"])
        claim = data["claim"]
        self.assertTrue(claim["claim_code"].startswith("CLM-"))
        self.assertEqual(claim["status"], "pending_review")
        self.assertEqual(claim["resolution_preference"], "replacement")

        # Verify account was frozen in database
        db = self.TestingSessionLocal()
        try:
            account = db.query(GrantedAccount).filter(GrantedAccount.id == self.acc_alice_id).first()
            self.assertEqual(account.status, "frozen")
        finally:
            db.close()

    # 6. Duplicate open claim is blocked
    def test_06_duplicate_open_claim_is_blocked(self):
        self._login_alice()
        today = datetime.utcnow().strftime("%Y-%m-%d")
        res = self.client.post(
            "/api/web/account/claims",
            json={
                "granted_account_id": self.acc_alice_id,
                "resolution_preference": "refund",
                "stopped_working_at": today,
                "description": "Submitting a second duplicate claim while first is open.",
            },
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("already in progress", res.json()["detail"])

    # 7. Customer can view claims in /account/claims
    def test_07_customer_can_list_and_view_claim(self):
        self._login_alice()
        res = self.client.get("/api/web/account/claims")
        self.assertEqual(res.status_code, 200)
        claims = res.json().get("claims", [])
        self.assertGreaterEqual(len(claims), 1)
        first_claim = claims[0]
        self.assertEqual(first_claim["granted_account_id"], self.acc_alice_id)

        # View specific claim
        res_detail = self.client.get(f"/api/web/account/claims/{first_claim['id']}")
        self.assertEqual(res_detail.status_code, 200)
        detail = res_detail.json().get("claim", {})
        self.assertEqual(detail["claim_code"], first_claim["claim_code"])

    # 8. Customer cannot access admin claims endpoints
    def test_08_customer_cannot_access_admin_claim_endpoint(self):
        self._login_alice()
        db = self.TestingSessionLocal()
        try:
            claim = db.query(Claim).filter(Claim.granted_account_id == self.acc_alice_id).first()
            claim_id = claim.id
        finally:
            db.close()

        res = self.client.post(f"/admin/claims/{claim_id}/approve-replacement", follow_redirects=False)
        self.assertIn(res.status_code, [302, 303, 401, 403])

    # 9. Evidence upload security: approved types pass, unapproved fail
    def test_09_evidence_upload_validation(self):
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (10, 10), color="blue").save(buf, format="PNG")
        valid_png = buf.getvalue()

        ok, err = validate_claim_evidence_upload(valid_png, "screenshot.png")
        self.assertTrue(ok)
        self.assertEqual(err, "Valid")

        # Disallowed exe
        ok, err = validate_claim_evidence_upload(b"MZ\x90\x00", "payload.exe")
        self.assertFalse(ok)
        self.assertIn("not allowed", err.lower())

        # Disallowed script
        ok, err = validate_claim_evidence_upload(b"<script>alert(1)</script>", "script.html")
        self.assertFalse(ok)

        # Oversized file (>10MB)
        huge_file = b"0" * (11 * 1024 * 1024)
        ok, err = validate_claim_evidence_upload(huge_file, "huge.pdf")
        self.assertFalse(ok)
        self.assertIn("exceeds limit", err.lower())

    # 10. Multipart form-data upload with evidence in POST /api/web/account/claims
    def test_10_claim_submission_with_multipart_evidence(self):
        db = self.TestingSessionLocal()
        try:
            # Create another active account for Bob to test upload
            service = db.query(Service).filter(Service.id == self.service_id).first()
            acc = GrantedAccount(
                user_id=self.bob_id,
                order_id=self.order_bob_id,
                service_id=service.id,
                login_email="bob_upload_test@premium.com",
                login_password="BobPassword456",
                status="active",
                subscription_start_at=datetime.utcnow() - timedelta(days=5),
                subscription_expires_at=datetime.utcnow() + timedelta(days=25),
                duration_days=30,
            )
            db.add(acc)
            db.commit()
            test_acc_id = acc.id
        finally:
            db.close()

        # Login as Bob
        self.client.post("/api/web/login", json={"email": "bob@example.com", "password": "password456"})

        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (10, 10), color="red").save(buf, format="PNG")
        valid_png = buf.getvalue()

        files = {"evidence": ("error_screen.png", io.BytesIO(valid_png), "image/png")}
        data = {
            "granted_account_id": str(test_acc_id),
            "resolution_preference": "support",
            "stopped_working_at": datetime.utcnow().strftime("%Y-%m-%d"),
            "description": "Attached screenshot showing error code 403 on login page.",
        }
        res = self.client.post("/api/web/account/claims", data=data, files=files)
        self.assertEqual(res.status_code, 200)
        resp_data = res.json()
        self.assertTrue(resp_data["ok"])
        self.assertIsNotNone(resp_data["claim"]["evidence_url"])
        self.assertIn("claims", resp_data["claim"]["evidence_url"])

    # 11. Admin can view estimate and approve replacement
    def test_11_admin_replacement_workflow(self):
        db = self.TestingSessionLocal()
        try:
            claim = db.query(Claim).filter(Claim.granted_account_id == self.acc_alice_id).first()
            claim_id = claim.id
            account_id = claim.granted_account_id
        finally:
            db.close()

        # Check server estimate
        db = self.TestingSessionLocal()
        try:
            acc = db.query(GrantedAccount).filter(GrantedAccount.id == account_id).first()
            est = calculate_account_refund_estimate(acc)
            self.assertGreater(est["estimated_refund"], 0.0)
            orig_expiry = acc.subscription_expires_at
        finally:
            db.close()

        # Approve replacement using workflow helper
        db = self.TestingSessionLocal()
        try:
            claim = db.query(Claim).filter(Claim.id == claim_id).first()
            new_acc, source_method = resolve_claim_with_replacement(db, claim=claim, admin_note="Replaced from active inventory.")
            self.assertIsNotNone(new_acc)
            db.commit()

            # Verify old account is replaced
            old_acc = db.query(GrantedAccount).filter(GrantedAccount.id == account_id).first()
            self.assertEqual(old_acc.status, "replaced")
            self.assertEqual(old_acc.replaced_by_account_id, new_acc.id)
            # Old credentials NOT destroyed
            self.assertEqual(old_acc.login_email, "alice_netflix@premium.com")

            # Verify new account inherits original expiry date (Requirement 14)
            self.assertEqual(new_acc.user_id, self.alice_id)
            self.assertEqual(new_acc.status, "active")
            self.assertEqual(new_acc.replacement_for_account_id, old_acc.id)
            self.assertEqual(new_acc.subscription_expires_at, orig_expiry)
        finally:
            db.close()

    # 12. Support & Fix unfreezes account
    def test_12_support_fix_unfreezes_account(self):
        db = self.TestingSessionLocal()
        try:
            # Create a frozen claim for Bob
            acc = db.query(GrantedAccount).filter(GrantedAccount.id == self.acc_bob_id).first()
            acc.status = "frozen"
            claim = Claim(
                claim_code="CLM-BOB-SUPP",
                user_id=self.bob_id,
                granted_account_id=acc.id,
                order_id=self.order_bob_id,
                order_code="ORD-BOB-001",
                service_id=self.service_id,
                resolution_preference="support",
                stopped_working_at=datetime.utcnow().date(),
                description="Needs profile reset.",
                status="under_review",
            )
            db.add(claim)
            db.commit()
            claim_id = claim.id
        finally:
            db.close()

        # Resolve via Support & Fix
        db = self.TestingSessionLocal()
        try:
            claim = db.query(Claim).filter(Claim.id == claim_id).first()
            res_claim = resolve_claim_with_support_fix(db, claim=claim, resolution_note="Profile reset applied.")
            self.assertEqual(res_claim.status, "resolved")
            db.commit()

            acc = db.query(GrantedAccount).filter(GrantedAccount.id == self.acc_bob_id).first()
            self.assertEqual(acc.status, "active")
            self.assertEqual(claim.status, "resolved")
            self.assertEqual(claim.resolution_type, "support_fixed")
        finally:
            db.close()

    # 13. Claim Rejection restores account and does not refund
    def test_13_claim_rejection_restores_account(self):
        db = self.TestingSessionLocal()
        try:
            acc = db.query(GrantedAccount).filter(GrantedAccount.id == self.acc_bob_id).first()
            acc.status = "frozen"
            claim = Claim(
                claim_code="CLM-BOB-REJ",
                user_id=self.bob_id,
                granted_account_id=acc.id,
                order_id=self.order_bob_id,
                order_code="ORD-BOB-001",
                service_id=self.service_id,
                resolution_preference="refund",
                stopped_working_at=datetime.utcnow().date(),
                description="Invalid complaint.",
                status="pending_review",
            )
            db.add(claim)
            db.commit()
            claim_id = claim.id
        finally:
            db.close()

        db = self.TestingSessionLocal()
        try:
            claim = db.query(Claim).filter(Claim.id == claim_id).first()
            rej_claim = reject_claim(db, claim=claim, reason="Account verified working normally.")
            self.assertEqual(rej_claim.status, "rejected")
            db.commit()

            acc = db.query(GrantedAccount).filter(GrantedAccount.id == self.acc_bob_id).first()
            self.assertEqual(acc.status, "active")
            self.assertEqual(claim.status, "rejected")
            self.assertIsNone(claim.refund_amount)
        finally:
            db.close()

    # 14. Pro-Rata Refund to wallet is atomic and double-refund protected
    def test_14_wallet_refund_atomic_and_idempotent(self):
        db = self.TestingSessionLocal()
        try:
            # Create account for refund test
            acc = GrantedAccount(
                user_id=self.bob_id,
                order_id=self.order_bob_id,
                service_id=self.service_id,
                login_email="bob_refund@premium.com",
                login_password="BobPassword789",
                status="frozen",
                subscription_start_at=datetime.utcnow() - timedelta(days=15),
                subscription_expires_at=datetime.utcnow() + timedelta(days=15),
                duration_days=30,
            )
            db.add(acc)
            db.flush()

            claim = Claim(
                claim_code="CLM-BOB-REFUND-001",
                user_id=self.bob_id,
                granted_account_id=acc.id,
                order_id=self.order_bob_id,
                order_code="ORD-BOB-001",
                service_id=self.service_id,
                resolution_preference="refund",
                stopped_working_at=datetime.utcnow().date(),
                description="Requesting pro-rata refund for remaining 15 days.",
                status="under_review",
            )
            db.add(claim)
            db.commit()

            claim_id = claim.id
            acc_id = acc.id
            initial_bob_wallet = db.query(User).filter(User.id == self.bob_id).first().wallet_usdt
        finally:
            db.close()

        # First refund attempt: should succeed
        db = self.TestingSessionLocal()
        try:
            claim = db.query(Claim).filter(Claim.id == claim_id).first()
            res_refund = resolve_claim_with_refund(db, claim=claim, refund_method="wallet", admin_note="Approved pro-rata wallet refund.")
            self.assertTrue(res_refund["ok"])
            amt = res_refund["refund_amount"]
            self.assertGreater(amt, 0.0)
            db.commit()

            # Verify wallet credited
            bob_user = db.query(User).filter(User.id == self.bob_id).first()
            self.assertAlmostEqual(bob_user.wallet_usdt, initial_bob_wallet + amt, places=2)

            # Verify transaction record created
            tx = (
                db.query(Transaction)
                .filter(Transaction.user_id == self.bob_id, Transaction.tx_type == "refund")
                .order_by(Transaction.id.desc())
                .first()
            )
            self.assertIsNotNone(tx)
            self.assertAlmostEqual(tx.amount, amt, places=2)
            self.assertIn(claim.claim_code, tx.note)

            # Verify account status is refunded
            acc = db.query(GrantedAccount).filter(GrantedAccount.id == acc_id).first()
            self.assertEqual(acc.status, "refunded")
        finally:
            db.close()

        # Second refund attempt: MUST FAIL (Double-refund protection / Idempotency guard)
        db = self.TestingSessionLocal()
        try:
            claim = db.query(Claim).filter(Claim.id == claim_id).first()
            with self.assertRaises(ValueError):
                resolve_claim_with_refund(db, claim=claim, refund_method="wallet", admin_note="Duplicate click attempt.")

            # Wallet balance did NOT change
            bob_user = db.query(User).filter(User.id == self.bob_id).first()
            self.assertAlmostEqual(bob_user.wallet_usdt, initial_bob_wallet + amt, places=2)
        finally:
            db.close()

    # 15. Telegram notification failure does not break transaction
    def test_15_telegram_notification_fault_tolerance(self):
        # Call all notification helpers with invalid/unreachable bot - should log and not raise
        import asyncio

        async def _run_all():
            await notify_claim_submitted(None, "CLM-TEST-999", "Netflix 4K", "Instant Replacement")
            await notify_claim_approved_replacement(None, "CLM-TEST-999", "Netflix 4K")
            await notify_claim_refunded(None, "CLM-TEST-999", 15.0, "wallet", 40.0)
            await notify_claim_resolved_support(None, "CLM-TEST-999", "Netflix 4K")
            await notify_claim_rejected(None, "CLM-TEST-999", "Invalid claim")
            await notify_claim_evidence_requested(None, "CLM-TEST-999", "Please send screenshot")

        try:
            asyncio.run(_run_all())
        except Exception as exc:
            self.fail(f"Notification helpers must never raise uncaught exceptions: {exc}")


if __name__ == "__main__":
    unittest.main()
