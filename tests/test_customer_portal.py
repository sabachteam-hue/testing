import logging
import os
import unittest
from datetime import datetime
from unittest.mock import patch

logging.getLogger("httpx").setLevel(logging.WARNING)

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from database.models import Base, Order, PaymentMethod, Service, Stock, User, get_db
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


if __name__ == "__main__":
    unittest.main()

