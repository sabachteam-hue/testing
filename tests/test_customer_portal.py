import logging
import os
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

logging.getLogger("httpx").setLevel(logging.WARNING)

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import Base, GrantedAccount, Order, PaymentMethod, Service, Stock, Transaction, User, get_db
from main import app
from utils.rate_limiter import _memory_failures, _memory_lockouts, _memory_windows
from utils.security import hash_password


class CustomerPortalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # In-memory SQLite with StaticPool so all connections share the same in-memory database
        cls.engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        cls.TestingSessionLocal = sessionmaker(bind=cls.engine, autoflush=False, autocommit=False)
        Base.metadata.create_all(bind=cls.engine)

        # Seed test payment method and service with stock
        db = cls.TestingSessionLocal()
        try:
            method = PaymentMethod(
                name="USDT TRC20",
                code="TRC20",
                method_type="crypto",
                network="Tron (TRC20)",
                address="TXYZ1234567890",
                is_active=True,
            )
            db.add(method)

            service = Service(
                sku="CHATGPT-PLUS-TEST",
                name="ChatGPT Plus Test",
                sell_price=10.0,
                is_active=True,
                is_deleted=False,
                min_qty=1,
                max_qty=5,
            )
            db.add(service)
            db.flush()

            stock = Stock(
                service_id=service.id,
                quantity=100,
                reserved_qty=0,
            )
            db.add(stock)

            # Customer A
            user_a = User(
                telegram_id="web:alice@example.com",
                username="alice",
                full_name="Alice Smith",
                email="alice@example.com",
                password_hash=hash_password("password123"),
                wallet_usdt=50.0,
            )
            db.add(user_a)

            # Customer B
            user_b = User(
                telegram_id="web:bob@example.com",
                username="bob",
                full_name="Bob Jones",
                email="bob@example.com",
                password_hash=hash_password("password456"),
                wallet_usdt=15.0,
            )
            db.add(user_b)
            db.commit()

            cls.user_a_id = user_a.id
            cls.user_b_id = user_b.id

            # Orders for Customer A (2 orders)
            order1 = Order(
                order_code="SMF-TEST-001",
                user_id=cls.user_a_id,
                service_id=service.id,
                link="web_order",
                quantity=1,
                amount_usdt=10.0,
                status="completed",
                payment_method="TRC20",
            )
            order2 = Order(
                order_code="SMF-TEST-002",
                user_id=cls.user_a_id,
                service_id=service.id,
                link="web_order",
                quantity=2,
                amount_usdt=20.0,
                status="pending",
                payment_method="TRC20",
            )
            # Order for Customer B (1 order)
            order3 = Order(
                order_code="SMF-TEST-003",
                user_id=cls.user_b_id,
                service_id=service.id,
                link="web_order",
                quantity=1,
                amount_usdt=10.0,
                status="pending",
                payment_method="TRC20",
            )
            db.add_all([order1, order2, order3])
            db.commit()
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

    # 1. Existing customer can log in
    def test_01_existing_customer_can_login(self):
        res = self.client.post(
            "/api/web/login",
            json={"email": "alice@example.com", "password": "password123"},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["user"]["email"], "alice@example.com")
        self.assertEqual(data["user"]["name"], "Alice Smith")
        # Ensure password_hash is never returned in API responses
        self.assertNotIn("password_hash", data["user"])
        # Session cookie was set
        self.assertTrue(any(c.name == "session" for c in self.client.cookies.jar))

    # 2. New customer can register
    def test_02_new_customer_can_register(self):
        client = TestClient(app)
        res = client.post(
            "/api/web/signup",
            json={"name": "Charlie", "email": "charlie@example.com", "password": "securepass123"},
        )
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["user"]["email"], "charlie@example.com")
        self.assertEqual(data["user"]["name"], "Charlie")
        self.assertNotIn("password_hash", data["user"])

        # Check DB user was created with hashed password
        db = self.TestingSessionLocal()
        try:
            charlie = db.query(User).filter(User.email == "charlie@example.com").first()
            self.assertIsNotNone(charlie)
            self.assertNotEqual(charlie.password_hash, "securepass123")
            self.assertTrue(charlie.password_hash.startswith(("$argon2", "$2b$", "pbkdf2:")))
        finally:
            db.close()

    # 3. Invalid password is rejected
    def test_03_invalid_password_rejected(self):
        client = TestClient(app)
        res = client.post(
            "/api/web/login",
            json={"email": "alice@example.com", "password": "wrongpassword"},
        )
        self.assertEqual(res.status_code, 401)
        # Session should not be authenticated
        me_res = client.get("/api/web/me")
        self.assertEqual(me_res.status_code, 200)
        self.assertFalse(me_res.json()["authenticated"])

    # 4. Logged-out user cannot access protected account APIs
    def test_04_unauthenticated_cannot_access_dashboard(self):
        client = TestClient(app)
        res = client.get("/api/web/account/dashboard")
        self.assertEqual(res.status_code, 401)
        self.assertIn("detail", res.json())

    # 5. Customer dashboard loads only their own information
    def test_05_customer_dashboard_loads_own_information(self):
        client = TestClient(app)
        # Log in as Alice
        login_res = client.post(
            "/api/web/login",
            json={"email": "alice@example.com", "password": "password123"},
        )
        self.assertEqual(login_res.status_code, 200)

        dash_res = client.get("/api/web/account/dashboard")
        self.assertEqual(dash_res.status_code, 200)
        data = dash_res.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["customer"]["email"], "alice@example.com")
        self.assertEqual(data["customer"]["name"], "Alice Smith")
        # Alice has exactly 2 orders in DB
        self.assertEqual(data["stats"]["total_orders"], 2)
        self.assertEqual(data["stats"]["wallet_balance"], 50.0)
        self.assertEqual(data["stats"]["active_accounts"], 0)
        self.assertEqual(data["stats"]["open_claims"], 0)
        # Recent orders belong to Alice
        self.assertEqual(len(data["recent_orders"]), 2)
        order_codes = [o["order_code"] for o in data["recent_orders"]]
        self.assertIn("SMF-TEST-001", order_codes)
        self.assertIn("SMF-TEST-002", order_codes)
        self.assertNotIn("SMF-TEST-003", order_codes)

    # 6. Customer cannot access another customer's data by changing request parameters
    def test_06_customer_cannot_tamper_to_access_other_data(self):
        client = TestClient(app)
        # Log in as Bob
        client.post(
            "/api/web/login",
            json={"email": "bob@example.com", "password": "password456"},
        )

        # Attempt to pass query parameters or tampering with user_id/email
        dash_res = client.get(
            "/api/web/account/dashboard",
            params={"user_id": self.user_a_id, "email": "alice@example.com"},
        )
        self.assertEqual(dash_res.status_code, 200)
        data = dash_res.json()
        # Bob must only see Bob's data
        self.assertEqual(data["customer"]["email"], "bob@example.com")
        self.assertEqual(data["customer"]["name"], "Bob Jones")
        self.assertEqual(data["stats"]["total_orders"], 1)
        self.assertEqual(data["stats"]["wallet_balance"], 15.0)
        order_codes = [o["order_code"] for o in data["recent_orders"]]
        self.assertIn("SMF-TEST-003", order_codes)
        self.assertNotIn("SMF-TEST-001", order_codes)
        self.assertNotIn("SMF-TEST-002", order_codes)

    # 7. Logout invalidates/removes customer session correctly
    def test_07_logout_invalidates_session(self):
        client = TestClient(app)
        client.post(
            "/api/web/login",
            json={"email": "alice@example.com", "password": "password123"},
        )
        # Confirm logged in
        dash_res = client.get("/api/web/account/dashboard")
        self.assertEqual(dash_res.status_code, 200)

        # Log out
        logout_res = client.post("/api/web/logout")
        self.assertEqual(logout_res.status_code, 200)
        self.assertTrue(logout_res.json()["ok"])

        # Dashboard call must now be rejected
        dash_after = client.get("/api/web/account/dashboard")
        self.assertEqual(dash_after.status_code, 401)

        # /api/web/me must return unauthenticated
        me_res = client.get("/api/web/me")
        self.assertFalse(me_res.json()["authenticated"])

    # 8. Existing storefront still loads
    def test_08_storefront_still_loads(self):
        res = self.client.get("/mini")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/html", res.headers.get("content-type", ""))

        shop_res = self.client.get("/api/web/shop")
        self.assertEqual(shop_res.status_code, 200)
        self.assertIn("name", shop_res.json())

    # 9. Customer portal route loads
    def test_09_account_portal_route_loads(self):
        res = self.client.get("/account")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/html", res.headers.get("content-type", ""))
        # Check security headers / CSP frame-ancestors for Telegram Mini App
        csp = res.headers.get("content-security-policy", "")
        self.assertIn("frame-ancestors 'self' https://web.telegram.org", csp)

    # 10. Checkout works for both guest and authenticated customer
    def test_10_checkout_guest_and_authenticated(self):
        # Authenticated checkout
        client = TestClient(app)
        client.post(
            "/api/web/login",
            json={"email": "alice@example.com", "password": "password123"},
        )
        res_auth = client.post(
            "/api/web/checkout",
            json={
                "email": "alice@example.com",
                "payment_method": "TRC20",
                "items": [{"sku": "CHATGPT-PLUS-TEST", "qty": 1}],
            },
        )
        self.assertEqual(res_auth.status_code, 200)
        auth_data = res_auth.json()
        self.assertTrue(auth_data["ok"])
        self.assertEqual(auth_data["user"]["email"], "alice@example.com")

        # Guest checkout (new email)
        client_guest = TestClient(app)
        res_guest = client_guest.post(
            "/api/web/checkout",
            json={
                "email": "guest123@example.com",
                "name": "Guest Customer",
                "payment_method": "TRC20",
                "items": [{"sku": "CHATGPT-PLUS-TEST", "qty": 1}],
            },
        )
        self.assertEqual(res_guest.status_code, 200)
        guest_data = res_guest.json()
        self.assertTrue(guest_data["ok"])
        self.assertEqual(guest_data["user"]["email"], "guest123@example.com")

    # 11. Admin panel route still loads
    def test_11_admin_panel_still_loads(self):
        res = self.client.get("/admin/login")
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/html", res.headers.get("content-type", ""))

    # 12. Database integrity: existing customer account remains intact
    def test_12_database_integrity_preserved(self):
        db = self.TestingSessionLocal()
        try:
            alice = db.query(User).filter(User.email == "alice@example.com").first()
            self.assertIsNotNone(alice)
            self.assertEqual(alice.wallet_usdt, 50.0)
            self.assertEqual(alice.full_name, "Alice Smith")
        finally:
            db.close()


class CustomerOrdersMissionLogsTests(unittest.TestCase):
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
            method = PaymentMethod(
                name="USDT TRC20",
                code="TRC20",
                method_type="crypto",
                network="Tron (TRC20)",
                address="TXYZ9876543210",
                is_active=True,
            )
            db.add(method)

            service = Service(
                sku="CURSOR-PRO-TEST",
                name="Cursor Pro Subscription",
                sell_price=20.0,
                is_active=True,
                is_deleted=False,
                min_qty=1,
                max_qty=5,
            )
            db.add(service)
            db.flush()

            # Alice
            alice = User(
                telegram_id="web:alice_logs@example.com",
                username="alice_logs",
                full_name="Alice Logs",
                email="alice_logs@example.com",
                password_hash=hash_password("password123"),
                wallet_usdt=100.0,
            )
            # Bob
            bob = User(
                telegram_id="web:bob_logs@example.com",
                username="bob_logs",
                full_name="Bob Logs",
                email="bob_logs@example.com",
                password_hash=hash_password("password456"),
                wallet_usdt=50.0,
            )
            db.add_all([alice, bob])
            db.flush()

            cls.alice_id = alice.id
            cls.bob_id = bob.id

            # Alice orders: completed, pending, preorder, refunded, cancelled
            ord1 = Order(
                order_code="ORD-A-COMPL",
                user_id=alice.id,
                service_id=service.id,
                link="web_order",
                quantity=1,
                amount_usdt=20.0,
                status="completed",
                payment_method="TRC20",
                delivered_info="user:pass123",
            )
            ord2 = Order(
                order_code="ORD-A-PEND",
                user_id=alice.id,
                service_id=service.id,
                link="web_order",
                quantity=1,
                amount_usdt=20.0,
                status="pending",
                payment_method="TRC20",
            )
            ord3 = Order(
                order_code="ORD-A-PRE",
                user_id=alice.id,
                service_id=service.id,
                link="web_order",
                quantity=1,
                amount_usdt=20.0,
                status="preorder_waiting",
                is_preorder=True,
                payment_method="TRC20",
            )
            ord4 = Order(
                order_code="ORD-A-REF",
                user_id=alice.id,
                service_id=service.id,
                link="web_order",
                quantity=1,
                amount_usdt=20.0,
                status="refunded",
                refund_amount=20.0,
                refund_method="wallet",
                refunded_at=datetime.utcnow(),
                payment_method="TRC20",
            )
            ord5 = Order(
                order_code="ORD-A-CANC",
                user_id=alice.id,
                service_id=service.id,
                link="web_order",
                quantity=1,
                amount_usdt=20.0,
                status="cancelled",
                payment_method="TRC20",
            )
            # Bob order
            ord_bob = Order(
                order_code="ORD-B-COMPL",
                user_id=bob.id,
                service_id=service.id,
                link="web_order",
                quantity=1,
                amount_usdt=20.0,
                status="completed",
                payment_method="TRC20",
                delivered_info="bob_user:bob_pass",
            )
            db.add_all([ord1, ord2, ord3, ord4, ord5, ord_bob])
            db.commit()
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

    def _login_alice(self) -> TestClient:
        client = TestClient(app)
        res = client.post(
            "/api/web/login",
            json={"email": "alice_logs@example.com", "password": "password123"},
        )
        self.assertEqual(res.status_code, 200)
        return client

    def _login_bob(self) -> TestClient:
        client = TestClient(app)
        res = client.post(
            "/api/web/login",
            json={"email": "bob_logs@example.com", "password": "password456"},
        )
        self.assertEqual(res.status_code, 200)
        return client

    # 1. Unauthenticated customer cannot access order list or order details
    def test_orders_unauthenticated_blocked(self):
        client = TestClient(app)
        res_list = client.get("/api/web/account/orders")
        self.assertEqual(res_list.status_code, 401)

        res_detail = client.get("/api/web/account/orders/ORD-A-COMPL")
        self.assertEqual(res_detail.status_code, 401)

    # 2. Customer sees only their own orders
    def test_customer_sees_only_own_orders(self):
        client = self._login_alice()
        res = client.get("/api/web/account/orders")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["total"], 5)

        codes = [o["order_code"] for o in data["orders"]]
        # Alice's orders must be present
        self.assertIn("ORD-A-COMPL", codes)
        self.assertIn("ORD-A-PEND", codes)
        self.assertIn("ORD-A-PRE", codes)
        self.assertIn("ORD-A-REF", codes)
        self.assertIn("ORD-A-CANC", codes)
        # Bob's order must NOT be present
        self.assertNotIn("ORD-B-COMPL", codes)

    # 3. Customer orders are sorted newest first
    def test_customer_orders_newest_first(self):
        client = self._login_alice()
        res = client.get("/api/web/account/orders")
        self.assertEqual(res.status_code, 200)
        orders = res.json()["orders"]
        self.assertGreaterEqual(len(orders), 2)
        # ORD-A-CANC was inserted last, so it must be first
        self.assertEqual(orders[0]["order_code"], "ORD-A-CANC")

    # 4. Bob sees only Bob's orders
    def test_other_customer_sees_only_their_orders(self):
        client = self._login_bob()
        res = client.get("/api/web/account/orders")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(len(data["orders"]), 1)
        self.assertEqual(data["orders"][0]["order_code"], "ORD-B-COMPL")
        # None of Alice's orders
        codes = [o["order_code"] for o in data["orders"]]
        self.assertNotIn("ORD-A-COMPL", codes)

    # 5. Tamper attempt: Alice cannot access Bob's order detail (returns 404)
    def test_tamper_attempt_prevented_on_order_detail(self):
        client = self._login_alice()
        # Alice tries to request Bob's order detail
        res = client.get("/api/web/account/orders/ORD-B-COMPL")
        self.assertEqual(res.status_code, 404)

        # Alice tries tampering with query params to access Bob's data
        res_tamper = client.get(
            "/api/web/account/orders",
            params={"user_id": self.bob_id, "email": "bob_logs@example.com"},
        )
        self.assertEqual(res_tamper.status_code, 200)
        codes = [o["order_code"] for o in res_tamper.json()["orders"]]
        self.assertNotIn("ORD-B-COMPL", codes)
        self.assertIn("ORD-A-COMPL", codes)

    # 6. Status filtering works correctly
    def test_status_filtering(self):
        client = self._login_alice()

        # Active: pending, preorder_waiting
        res_act = client.get("/api/web/account/orders", params={"status_filter": "active"})
        self.assertEqual(res_act.status_code, 200)
        act_codes = [o["order_code"] for o in res_act.json()["orders"]]
        self.assertIn("ORD-A-PEND", act_codes)
        self.assertIn("ORD-A-PRE", act_codes)
        self.assertNotIn("ORD-A-COMPL", act_codes)
        self.assertNotIn("ORD-A-REF", act_codes)
        self.assertNotIn("ORD-A-CANC", act_codes)

        # Completed
        res_comp = client.get("/api/web/account/orders", params={"status_filter": "completed"})
        self.assertEqual(res_comp.status_code, 200)
        comp_codes = [o["order_code"] for o in res_comp.json()["orders"]]
        self.assertEqual(comp_codes, ["ORD-A-COMPL"])

        # Refunded
        res_ref = client.get("/api/web/account/orders", params={"status_filter": "refunded"})
        self.assertEqual(res_ref.status_code, 200)
        ref_codes = [o["order_code"] for o in res_ref.json()["orders"]]
        self.assertEqual(ref_codes, ["ORD-A-REF"])

        # Cancelled
        res_canc = client.get("/api/web/account/orders", params={"status_filter": "cancelled"})
        self.assertEqual(res_canc.status_code, 200)
        canc_codes = [o["order_code"] for o in res_canc.json()["orders"]]
        self.assertEqual(canc_codes, ["ORD-A-CANC"])

    # 7. Preorder order presentation and detail
    def test_preorder_display_and_detail(self):
        client = self._login_alice()
        res = client.get("/api/web/account/orders/ORD-A-PRE")
        self.assertEqual(res.status_code, 200)
        order = res.json()["order"]
        self.assertTrue(order["is_preorder"])
        self.assertEqual(order["status_badge"], "preorder")
        self.assertIn("Pre-order", order["status_label"])
        self.assertEqual(order["delivery_status"], "Pre-order Waiting")
        self.assertIn("stock", order["delivery_info"].lower())

    # 8. Refunded order presentation and detail
    def test_refunded_order_display_and_detail(self):
        client = self._login_alice()
        res = client.get("/api/web/account/orders/ORD-A-REF")
        self.assertEqual(res.status_code, 200)
        order = res.json()["order"]
        self.assertTrue(order["is_refunded"])
        self.assertEqual(order["status_badge"], "refunded")
        self.assertEqual(order["refund_amount"], 20.0)
        self.assertEqual(order["refund_method"], "wallet")
        self.assertEqual(order["delivery_status"], "Refunded")
        self.assertIn("refunded", order["delivery_info"].lower())

    # 9. Completed order delivery info returned safely
    def test_completed_order_delivery_info(self):
        client = self._login_alice()
        res = client.get("/api/web/account/orders/ORD-A-COMPL")
        self.assertEqual(res.status_code, 200)
        order = res.json()["order"]
        self.assertEqual(order["status_badge"], "completed")
        self.assertEqual(order["delivery_status"], "Delivered")
        self.assertEqual(order["delivery_info"], "user:pass123")

    # 10. Public order lookup still works without authentication
    def test_public_order_lookup_still_works(self):
        client = TestClient(app)
        res = client.get("/api/web/orders/ORD-A-COMPL")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["order_code"], "ORD-A-COMPL")
        self.assertEqual(data["amount"], 20.0)
        self.assertEqual(data["status"], "completed")

        # Nonexistent returns 404
        res_nonexistent = client.get("/api/web/orders/NONEXISTENT-CODE")
        self.assertEqual(res_nonexistent.status_code, 404)


class CustomerGrantedAccountsTests(unittest.TestCase):
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
            method = PaymentMethod(
                name="USDT TRC20",
                code="TRC20",
                method_type="crypto",
                network="Tron (TRC20)",
                address="TXYZ111122223333",
                is_active=True,
            )
            db.add(method)

            # Service with duration_days
            svc_chatgpt = Service(
                sku="CHATGPT-SUB-TEST",
                name="ChatGPT Plus Subscription",
                sell_price=20.0,
                duration_days=30,
                warranty="30 Days",
                is_active=True,
                is_deleted=False,
            )
            # Service with warranty text only (1 Year)
            svc_netflix = Service(
                sku="NETFLIX-1Y-TEST",
                name="Netflix Premium 1 Year",
                sell_price=50.0,
                warranty="1 Year",
                is_active=True,
                is_deleted=False,
            )
            db.add_all([svc_chatgpt, svc_netflix])
            db.flush()

            # Alice
            alice = User(
                telegram_id="web:alice_subs@example.com",
                username="alice_subs",
                full_name="Alice Subs",
                email="alice_subs@example.com",
                password_hash=hash_password("password123"),
                wallet_usdt=200.0,
            )
            # Bob
            bob = User(
                telegram_id="web:bob_subs@example.com",
                username="bob_subs",
                full_name="Bob Subs",
                email="bob_subs@example.com",
                password_hash=hash_password("password456"),
                wallet_usdt=100.0,
            )
            db.add_all([alice, bob])
            db.flush()

            cls.alice_id = alice.id
            cls.bob_id = bob.id

            now = datetime.utcnow()

            # Alice Order 1: Active 30-day sub completed 10 days ago (20 days remaining)
            ord_active = Order(
                order_code="ORD-ALICE-ACTIVE",
                user_id=alice.id,
                service_id=svc_chatgpt.id,
                link="web_order",
                quantity=1,
                amount_usdt=20.0,
                status="completed",
                payment_method="TRC20",
                delivered_info="alice_user@openai.com:SecretPass123",
                completed_at=now - timedelta(days=10),
            )
            # Alice Order 2: Expired 30-day sub completed 40 days ago (0 days remaining)
            ord_expired = Order(
                order_code="ORD-ALICE-EXPIRED",
                user_id=alice.id,
                service_id=svc_chatgpt.id,
                link="web_order",
                quantity=1,
                amount_usdt=20.0,
                status="completed",
                payment_method="TRC20",
                delivered_info="expired_user:OldPass999",
                completed_at=now - timedelta(days=40),
            )
            # Alice Order 3: Refunded sub
            ord_refunded = Order(
                order_code="ORD-ALICE-REFUNDED",
                user_id=alice.id,
                service_id=svc_chatgpt.id,
                link="web_order",
                quantity=1,
                amount_usdt=20.0,
                status="refunded",
                refund_amount=20.0,
                refund_method="wallet",
                refunded_at=now,
                payment_method="TRC20",
                delivered_info="refunded_user:RefPass111",
                completed_at=now - timedelta(days=5),
            )
            # Alice Order 4: Multi-quantity sub (quantity=2, 2 account lines)
            ord_multi = Order(
                order_code="ORD-ALICE-MULTI",
                user_id=alice.id,
                service_id=svc_netflix.id,
                link="web_order",
                quantity=2,
                amount_usdt=100.0,
                status="completed",
                payment_method="TRC20",
                delivered_info="multi_user1@netflix.com:netpass1:PIN:1234\nmulti_user2@netflix.com:netpass2:PIN:5678",
                completed_at=now - timedelta(days=2),
            )
            # Alice Order 5: Pending order (no credentials, pending)
            ord_pending = Order(
                order_code="ORD-ALICE-PENDING",
                user_id=alice.id,
                service_id=svc_chatgpt.id,
                link="web_order",
                quantity=1,
                amount_usdt=20.0,
                status="pending",
                payment_method="TRC20",
            )

            # Bob Order 1: Completed active sub
            ord_bob = Order(
                order_code="ORD-BOB-ACTIVE",
                user_id=bob.id,
                service_id=svc_netflix.id,
                link="web_order",
                quantity=1,
                amount_usdt=50.0,
                status="completed",
                payment_method="TRC20",
                delivered_info="bob_user@netflix.com:BobSecret777",
                completed_at=now - timedelta(days=1),
            )

            db.add_all([ord_active, ord_expired, ord_refunded, ord_multi, ord_pending, ord_bob])
            db.commit()

            # Pre-sync granted accounts
            from utils.granted_accounts import sync_user_granted_accounts
            sync_user_granted_accounts(db, alice.id)
            sync_user_granted_accounts(db, bob.id)

            bob_acc = db.query(GrantedAccount).filter(GrantedAccount.user_id == bob.id).first()
            cls.bob_acc_id = bob_acc.id if bob_acc else 0
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

    def _login_alice(self) -> TestClient:
        client = TestClient(app)
        res = client.post(
            "/api/web/login",
            json={"email": "alice_subs@example.com", "password": "password123"},
        )
        self.assertEqual(res.status_code, 200)
        return client

    def _login_bob(self) -> TestClient:
        client = TestClient(app)
        res = client.post(
            "/api/web/login",
            json={"email": "bob_subs@example.com", "password": "password456"},
        )
        self.assertEqual(res.status_code, 200)
        return client

    # 1. Unauthenticated customer blocked from accessing granted accounts
    def test_unauthenticated_blocked(self):
        client = TestClient(app)
        res_list = client.get("/api/web/account/granted-accounts")
        self.assertEqual(res_list.status_code, 401)

        res_detail = client.get("/api/web/account/granted-accounts/1")
        self.assertEqual(res_detail.status_code, 401)

    # 2. Customer sees only their own accounts
    def test_customer_sees_only_own_accounts(self):
        client = self._login_alice()
        res = client.get("/api/web/account/granted-accounts")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["ok"])
        # Alice has 5 accounts: 1 active, 1 expired, 1 refunded, 2 multi
        self.assertEqual(data["total"], 5)

        emails = [a["login_email"] for a in data["accounts"]]
        self.assertIn("alice_user@openai.com", emails)
        self.assertIn("expired_user", emails)
        self.assertIn("refunded_user", emails)
        self.assertIn("multi_user1@netflix.com", emails)
        self.assertIn("multi_user2@netflix.com", emails)
        # Bob's account must NOT be present
        self.assertNotIn("bob_user@netflix.com", emails)

    # 3. Bob sees only Bob's accounts
    def test_other_customer_sees_only_own_accounts(self):
        client = self._login_bob()
        res = client.get("/api/web/account/granted-accounts")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["accounts"][0]["login_email"], "bob_user@netflix.com")
        self.assertNotIn("alice_user@openai.com", [a["login_email"] for a in data["accounts"]])

    # 4. Tamper protection: Alice cannot access Bob's account detail
    def test_tamper_protection_on_account_detail(self):
        client = self._login_alice()
        res = client.get(f"/api/web/account/granted-accounts/{self.bob_acc_id}")
        self.assertEqual(res.status_code, 404)

    # 5. Pending order does not create granted account
    def test_pending_order_does_not_create_granted_account(self):
        client = self._login_alice()
        res = client.get("/api/web/account/granted-accounts?order_code=ORD-ALICE-PENDING")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["total"], 0)

    # 6. Multiple quantity order creates independent accounts with PIN/profile
    def test_multiple_quantity_creates_independent_accounts(self):
        client = self._login_alice()
        res = client.get("/api/web/account/granted-accounts?order_code=ORD-ALICE-MULTI")
        self.assertEqual(res.status_code, 200)
        accounts = res.json()["accounts"]
        self.assertEqual(len(accounts), 2)
        indices = [a["account_index"] for a in accounts]
        self.assertIn(0, indices)
        self.assertIn(1, indices)
        emails = [a["login_email"] for a in accounts]
        self.assertIn("multi_user1@netflix.com", emails)
        self.assertIn("multi_user2@netflix.com", emails)

    # 7. Subscription lifecycle calculations
    def test_lifecycle_metrics_calculation(self):
        client = self._login_alice()
        # Active account: 30 days total, started 10 days ago -> ~20 days remaining
        res_act = client.get("/api/web/account/granted-accounts?order_code=ORD-ALICE-ACTIVE")
        act = res_act.json()["accounts"][0]
        self.assertEqual(act["status"], "active")
        self.assertEqual(act["total_days"], 30)
        self.assertGreaterEqual(act["days_remaining"], 19)
        self.assertLessEqual(act["days_remaining"], 21)
        self.assertTrue(act["is_active"])
        self.assertFalse(act["is_expired"])

        # Expired account: 30 days total, started 40 days ago -> 0 days remaining, 100% progress
        res_exp = client.get("/api/web/account/granted-accounts?order_code=ORD-ALICE-EXPIRED")
        exp = res_exp.json()["accounts"][0]
        self.assertEqual(exp["status"], "expired")
        self.assertEqual(exp["days_remaining"], 0)
        self.assertEqual(exp["progress_percent"], 100.0)
        self.assertTrue(exp["is_expired"])

        # Refunded account
        res_ref = client.get("/api/web/account/granted-accounts?order_code=ORD-ALICE-REFUNDED")
        ref = res_ref.json()["accounts"][0]
        self.assertEqual(ref["status"], "refunded")
        self.assertTrue(ref["is_refunded"])

    # 8. Status filtering on granted accounts
    def test_status_filtering(self):
        client = self._login_alice()

        res_active = client.get("/api/web/account/granted-accounts?status_filter=active")
        self.assertEqual(res_active.status_code, 200)
        for a in res_active.json()["accounts"]:
            self.assertEqual(a["status"], "active")

        res_expired = client.get("/api/web/account/granted-accounts?status_filter=expired")
        self.assertEqual(res_expired.status_code, 200)
        for a in res_expired.json()["accounts"]:
            self.assertEqual(a["status"], "expired")

        res_refunded = client.get("/api/web/account/granted-accounts?status_filter=refunded")
        self.assertEqual(res_refunded.status_code, 200)
        for a in res_refunded.json()["accounts"]:
            self.assertEqual(a["status"], "refunded")

    # 9. Idempotency: re-running sync creates 0 duplicate records
    def test_idempotent_sync_no_duplicates(self):
        db = self.TestingSessionLocal()
        try:
            from utils.granted_accounts import sync_user_granted_accounts
            count_before = db.query(GrantedAccount).filter(GrantedAccount.user_id == self.alice_id).count()
            # Run sync again
            sync_user_granted_accounts(db, self.alice_id)
            count_after = db.query(GrantedAccount).filter(GrantedAccount.user_id == self.alice_id).count()
            self.assertEqual(count_before, count_after)
        finally:
            db.close()

    # 10. Dashboard active accounts count matches real active count
    def test_dashboard_active_accounts_count(self):
        client = self._login_alice()
        res = client.get("/api/web/account/dashboard")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        # Alice has 3 active accounts: 1 active chatgpt + 2 active netflix (ORD-ALICE-MULTI)
        self.assertEqual(data["stats"]["active_accounts"], 3)


class CustomerWalletAndRefundCalculatorTests(unittest.TestCase):
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
            # Create services
            svc_vpn = Service(
                sku="EXPRESS-VPN-30D",
                name="ExpressVPN 30 Days",
                sell_price=12.0,
                duration_days=30,
                warranty="30 Days",
                is_active=True,
                is_deleted=False,
            )
            svc_canva = Service(
                sku="CANVA-PRO-1Y",
                name="Canva Pro 1 Year",
                sell_price=48.0,
                duration_days=365,
                warranty="1 Year",
                is_active=True,
                is_deleted=False,
            )
            db.add_all([svc_vpn, svc_canva])
            db.flush()

            # Alice (Customer A)
            alice = User(
                telegram_id="web:alice_wallet@example.com",
                username="alice_wallet",
                full_name="Alice Wallet",
                email="alice_wallet@example.com",
                password_hash=hash_password("alicepass123"),
                wallet_usdt=150.0,
            )
            # Bob (Customer B)
            bob = User(
                telegram_id="web:bob_wallet@example.com",
                username="bob_wallet",
                full_name="Bob Wallet",
                email="bob_wallet@example.com",
                password_hash=hash_password("bobpass123"),
                wallet_usdt=75.0,
            )
            db.add_all([alice, bob])
            db.flush()

            cls.alice_id = alice.id
            cls.bob_id = bob.id

            now = datetime.utcnow()

            # Alice Transactions:
            # 1. Deposit $100
            tx_a1 = Transaction(
                user_id=alice.id,
                amount=100.0,
                tx_type="deposit",
                status="confirmed",
                created_at=now - timedelta(days=5),
                payfast_reference="PAYFAST-ALICE-100",
                note="PayFast deposit confirmed",
            )
            # 2. Purchase debit $40
            tx_a2 = Transaction(
                user_id=alice.id,
                amount=40.0,
                tx_type="deduct",
                status="confirmed",
                created_at=now - timedelta(days=4),
                note="Order SMF-ALICE-VPN",
            )
            # 3. Refund credit $10
            tx_a3 = Transaction(
                user_id=alice.id,
                amount=10.0,
                tx_type="refund",
                status="confirmed",
                created_at=now - timedelta(days=2),
                note="Refund for SMF-ALICE-OLD",
            )
            # 4. Admin credit $80
            tx_a4 = Transaction(
                user_id=alice.id,
                amount=80.0,
                tx_type="admin_credit",
                status="confirmed",
                created_at=now - timedelta(days=1),
                note="Admin balance credit",
            )

            # Bob Transactions:
            # 1. Deposit $50
            tx_b1 = Transaction(
                user_id=bob.id,
                amount=50.0,
                tx_type="deposit",
                status="confirmed",
                created_at=now - timedelta(days=3),
                payfast_reference="PAYFAST-BOB-50",
                note="Deposit Bob",
            )
            # 2. Purchase debit $20
            tx_b2 = Transaction(
                user_id=bob.id,
                amount=20.0,
                tx_type="deduct",
                status="confirmed",
                created_at=now - timedelta(days=2),
                note="Order SMF-BOB-VPN",
            )
            # 3. Admin debit $5
            tx_b3 = Transaction(
                user_id=bob.id,
                amount=5.0,
                tx_type="admin_debit",
                status="confirmed",
                created_at=now - timedelta(days=1),
                note="Correction debit",
            )

            db.add_all([tx_a1, tx_a2, tx_a3, tx_a4, tx_b1, tx_b2, tx_b3])
            db.flush()

            # Orders & Granted Accounts for Pro-Rata Refund Calculator testing:
            # Alice Order 1: Active 30-day sub, completed 10 days ago (20 days remaining)
            # Paid $12.00. 12 / 30 * 20 = $8.00 refund!
            ord_alice_active = Order(
                order_code="ORD-ALICE-CALC-ACTIVE",
                user_id=alice.id,
                service_id=svc_vpn.id,
                link="web",
                quantity=1,
                amount_usdt=12.0,
                status="completed",
                payment_method="WALLET",
                delivered_info="alice_vpn:vpnpass123",
                completed_at=now - timedelta(days=10),
            )
            # Alice Order 2: Expired 30-day sub, completed 40 days ago (0 days remaining)
            ord_alice_expired = Order(
                order_code="ORD-ALICE-CALC-EXP",
                user_id=alice.id,
                service_id=svc_vpn.id,
                link="web",
                quantity=1,
                amount_usdt=12.0,
                status="completed",
                payment_method="WALLET",
                delivered_info="expired_vpn:oldpass456",
                completed_at=now - timedelta(days=40),
            )
            # Alice Order 3: Refunded order ($12.00 refunded via wallet)
            ord_alice_refunded = Order(
                order_code="ORD-ALICE-CALC-REF",
                user_id=alice.id,
                service_id=svc_vpn.id,
                link="web",
                quantity=1,
                amount_usdt=12.0,
                status="refunded",
                refund_amount=12.0,
                refund_method="wallet",
                refunded_at=now - timedelta(days=1),
                payment_method="WALLET",
                delivered_info="refunded_vpn:refpass789",
                completed_at=now - timedelta(days=5),
            )
            # Alice Order 4: Multi-quantity order (qty=2, $48.00 total -> $24.00 each)
            ord_alice_multi = Order(
                order_code="ORD-ALICE-CALC-MULTI",
                user_id=alice.id,
                service_id=svc_canva.id,
                link="web",
                quantity=2,
                amount_usdt=48.0,
                status="completed",
                payment_method="WALLET",
                delivered_info="canva1@test.com:pass1\ncanva2@test.com:pass2",
                completed_at=now - timedelta(days=5),
            )
            # Bob Order: Bob's active order
            ord_bob_active = Order(
                order_code="ORD-BOB-CALC-ACTIVE",
                user_id=bob.id,
                service_id=svc_vpn.id,
                link="web",
                quantity=1,
                amount_usdt=12.0,
                status="completed",
                payment_method="WALLET",
                delivered_info="bob_vpn:bobpass999",
                completed_at=now - timedelta(days=5),
            )

            db.add_all([ord_alice_active, ord_alice_expired, ord_alice_refunded, ord_alice_multi, ord_bob_active])
            db.commit()

            # Sync granted accounts
            from utils.granted_accounts import sync_granted_accounts_for_order
            cls.acc_alice_active = sync_granted_accounts_for_order(db, ord_alice_active)[0].id
            cls.acc_alice_expired = sync_granted_accounts_for_order(db, ord_alice_expired)[0].id
            cls.acc_alice_refunded = sync_granted_accounts_for_order(db, ord_alice_refunded)[0].id
            multi_accs = sync_granted_accounts_for_order(db, ord_alice_multi)
            cls.acc_alice_multi_1 = multi_accs[0].id
            cls.acc_alice_multi_2 = multi_accs[1].id
            cls.acc_bob_active = sync_granted_accounts_for_order(db, ord_bob_active)[0].id
            db.commit()
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

    def tearDown(self):
        app.dependency_overrides.clear()

    def _login_alice(self):
        client = TestClient(app)
        res = client.post("/api/web/login", json={"email": "alice_wallet@example.com", "password": "alicepass123"})
        self.assertEqual(res.status_code, 200)
        return client

    def _login_bob(self):
        client = TestClient(app)
        res = client.post("/api/web/login", json={"email": "bob_wallet@example.com", "password": "bobpass123"})
        self.assertEqual(res.status_code, 200)
        return client

    # 1. Unauthenticated request to wallet is blocked
    def test_wallet_unauthenticated_blocked(self):
        client = TestClient(app)
        res = client.get("/api/web/account/wallet")
        self.assertEqual(res.status_code, 401)

    # 2. Customer sees correct wallet balance and summary totals
    def test_customer_wallet_balance_and_summary(self):
        client = self._login_alice()
        res = client.get("/api/web/account/wallet")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["balance"], 150.0)
        self.assertEqual(data["currency"], "USDT")
        # Total credits: 100 deposit + 10 refund + 80 admin_credit = 190.0
        self.assertEqual(data["total_credits"], 190.0)
        # Total debits: 40 deduct = 40.0
        self.assertEqual(data["total_debits"], 40.0)
        # Total refunds: 10.0
        self.assertEqual(data["total_refunds"], 10.0)
        self.assertEqual(data["total"], 4)

    # 3. Customer sees only their own transactions (strict isolation)
    def test_customer_sees_only_own_transactions(self):
        client = self._login_alice()
        res = client.get("/api/web/account/wallet")
        self.assertEqual(res.status_code, 200)
        txs = res.json()["transactions"]
        self.assertEqual(len(txs), 4)
        for tx in txs:
            # None of Bob's transactions
            self.assertNotIn("Bob", tx["description"])
            self.assertNotIn("PAYFAST-BOB", str(tx.get("reference") or ""))

    # 4. Transactions in descending order (newest first)
    def test_wallet_transactions_newest_first(self):
        client = self._login_alice()
        res = client.get("/api/web/account/wallet")
        self.assertEqual(res.status_code, 200)
        txs = res.json()["transactions"]
        # tx_a4 (1 day ago), tx_a3 (2 days ago), tx_a2 (4 days ago), tx_a1 (5 days ago)
        self.assertEqual(txs[0]["tx_type"], "admin_credit")
        self.assertEqual(txs[1]["tx_type"], "refund")
        self.assertEqual(txs[2]["tx_type"], "deduct")
        self.assertEqual(txs[3]["tx_type"], "deposit")

    # 5. Filter by type: credits, debits, refunds
    def test_wallet_type_filtering(self):
        client = self._login_alice()

        res_credits = client.get("/api/web/account/wallet?type_filter=credits")
        self.assertEqual(res_credits.status_code, 200)
        self.assertEqual(len(res_credits.json()["transactions"]), 3)
        for tx in res_credits.json()["transactions"]:
            self.assertEqual(tx["direction"], "credit")

        res_debits = client.get("/api/web/account/wallet?type_filter=debits")
        self.assertEqual(res_debits.status_code, 200)
        self.assertEqual(len(res_debits.json()["transactions"]), 1)
        self.assertEqual(res_debits.json()["transactions"][0]["amount"], 40.0)

        res_refunds = client.get("/api/web/account/wallet?type_filter=refunds")
        self.assertEqual(res_refunds.status_code, 200)
        self.assertEqual(len(res_refunds.json()["transactions"]), 1)
        self.assertEqual(res_refunds.json()["transactions"][0]["tx_type"], "refund")

    # 6. Customer B sees only Customer B's wallet
    def test_customer_b_wallet_isolation(self):
        client = self._login_bob()
        res = client.get("/api/web/account/wallet")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["balance"], 75.0)
        self.assertEqual(data["total_credits"], 50.0)
        self.assertEqual(data["total_debits"], 25.0)  # 20 purchase + 5 admin debit
        self.assertEqual(data["total"], 3)

    # 7. Admin balance updates reflect directly on customer wallet
    def test_admin_balance_update_reflected(self):
        db = self.TestingSessionLocal()
        tx_id = None
        try:
            user = db.get(User, self.alice_id)
            user.wallet_usdt += 50.0
            tx = Transaction(
                user_id=user.id,
                amount=50.0,
                tx_type="admin_credit",
                status="confirmed",
                created_at=datetime.utcnow(),
                note="Admin top-up bonus",
            )
            db.add(tx)
            db.commit()
            tx_id = tx.id
        finally:
            db.close()

        try:
            client = self._login_alice()
            res = client.get("/api/web/account/wallet")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertEqual(data["balance"], 200.0)
            self.assertEqual(data["total_credits"], 240.0)
        finally:
            # Revert mutation so subsequent tests remain unaffected
            db = self.TestingSessionLocal()
            try:
                user = db.get(User, self.alice_id)
                user.wallet_usdt -= 50.0
                if tx_id:
                    tx = db.get(Transaction, tx_id)
                    if tx:
                        db.delete(tx)
                db.commit()
            finally:
                db.close()

    # 8. Active subscription pro-rata refund estimate
    def test_pro_rata_refund_estimate_active_account(self):
        client = self._login_alice()
        res = client.get(f"/api/web/account/granted-accounts/{self.acc_alice_active}/refund-estimate")
        self.assertEqual(res.status_code, 200)
        est = res.json()["estimate"]
        self.assertTrue(est["is_eligible"])
        self.assertEqual(est["effective_status"], "active")
        self.assertEqual(est["amount_paid"], 12.0)
        self.assertEqual(est["total_days"], 30)
        self.assertGreaterEqual(est["days_remaining"], 19)
        self.assertLessEqual(est["days_remaining"], 21)
        self.assertEqual(est["daily_rate"], 0.4)
        # 12 / 30 * 20 = $8.00 (allow 1 day tolerance: 19*0.4=7.60 to 20*0.4=8.00)
        self.assertGreaterEqual(est["estimated_refund"], 7.60)
        self.assertLessEqual(est["estimated_refund"], 8.00)
        self.assertFalse(est["already_refunded"])

    # 9. Expired subscription pro-rata refund estimate is 0
    def test_pro_rata_refund_estimate_expired_account(self):
        client = self._login_alice()
        res = client.get(f"/api/web/account/granted-accounts/{self.acc_alice_expired}/refund-estimate")
        self.assertEqual(res.status_code, 200)
        est = res.json()["estimate"]
        self.assertFalse(est["is_eligible"])
        self.assertEqual(est["effective_status"], "expired")
        self.assertEqual(est["days_remaining"], 0)
        self.assertEqual(est["estimated_refund"], 0.0)

    # 10. Already-refunded subscription estimate returns already_refunded
    def test_pro_rata_refund_estimate_already_refunded(self):
        client = self._login_alice()
        res = client.get(f"/api/web/account/granted-accounts/{self.acc_alice_refunded}/refund-estimate")
        self.assertEqual(res.status_code, 200)
        est = res.json()["estimate"]
        self.assertFalse(est["is_eligible"])
        self.assertTrue(est["already_refunded"])
        self.assertEqual(est["effective_status"], "refunded")
        self.assertEqual(est["estimated_refund"], 0.0)
        self.assertIsNotNone(est["historical_refund"])
        self.assertEqual(est["historical_refund"]["refund_amount"], 12.0)
        self.assertEqual(est["historical_refund"]["refund_method"], "wallet")

    # 11. Multi-quantity order allocates price per account
    def test_pro_rata_refund_estimate_multi_quantity_allocated(self):
        client = self._login_alice()
        res = client.get(f"/api/web/account/granted-accounts/{self.acc_alice_multi_1}/refund-estimate")
        self.assertEqual(res.status_code, 200)
        est = res.json()["estimate"]
        # Order was $48 for 2 items -> per unit paid = $24.00
        self.assertEqual(est["amount_paid"], 24.0)
        self.assertEqual(est["total_order_amount"], 48.0)
        self.assertEqual(est["order_quantity"], 2)
        self.assertEqual(est["total_days"], 365)
        # Daily rate = 24 / 365 = ~0.0658
        self.assertAlmostEqual(est["daily_rate"], 0.0658, places=3)
        self.assertTrue(est["is_eligible"])
        self.assertGreater(est["estimated_refund"], 20.0)

    # 12. Customer B cannot query Customer A's refund estimate (404)
    def test_tamper_attempt_prevented_on_refund_estimate(self):
        client = self._login_bob()
        # Bob tries to access Alice's granted account refund estimate
        res = client.get(f"/api/web/account/granted-accounts/{self.acc_alice_active}/refund-estimate")
        self.assertEqual(res.status_code, 404)

    # 13. Unauthenticated cannot access refund estimate (401)
    def test_unauthenticated_blocked_on_refund_estimate(self):
        client = TestClient(app)
        res = client.get(f"/api/web/account/granted-accounts/{self.acc_alice_active}/refund-estimate")
        self.assertEqual(res.status_code, 401)


if __name__ == "__main__":
    unittest.main()


