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
    Order,
    PaymentMethod,
    Service,
    Stock,
    Transaction,
    User,
    UserProductDiscount,
    get_db,
)
from main import app
from utils.loyalty import (
    LOYALTY_TIERS,
    calculate_customer_order_metrics,
    compute_customer_loyalty,
    get_effective_customer_discount,
    get_tier_by_order_count,
)
from utils.pricing import resolve_unit_price
from utils.security import hash_password


class LoyaltyAndDiscountsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # In-memory SQLite database using StaticPool for isolation
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
                name="USDT TRC20",
                code="TRC20",
                method_type="crypto",
                network="Tron (TRC20)",
                address="TLoyaltyTestAddress123",
                is_active=True,
            )
            db.add(method)

            # Test Service ($100.00 base sell price)
            service = Service(
                sku="PRO-SUBSCRIPTION-100",
                name="Pro Subscription 100",
                sell_price=100.0,
                is_active=True,
                is_deleted=False,
                min_qty=1,
                max_qty=10,
            )
            db.add(service)
            db.flush()

            # Stock
            stock = Stock(
                service_id=service.id,
                quantity=500,
                reserved_qty=0,
            )
            db.add(stock)

            # Test Customer: John Doe
            user = User(
                telegram_id="web:john@example.com",
                username="johndoe",
                full_name="John Doe",
                email="john@example.com",
                password_hash=hash_password("password123"),
                wallet_usdt=200.0,
            )
            db.add(user)
            db.commit()

            cls.service_id = service.id
            cls.user_id = user.id
        finally:
            db.close()

        def override_get_db():
            session = cls.TestingSessionLocal()
            try:
                yield session
            finally:
                session.close()

        app.dependency_overrides[get_db] = override_get_db
        cls.client = TestClient(app)

    @classmethod
    def tearDownClass(cls):
        app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=cls.engine)

    def _login_john(self) -> TestClient:
        client = TestClient(app)
        res = client.post(
            "/api/web/login",
            json={"email": "john@example.com", "password": "password123"},
        )
        self.assertEqual(res.status_code, 200, f"Login failed: {res.text}")
        return client

    # 1. Test tier determination by completed order count
    def test_loyalty_tier_thresholds(self):
        self.assertEqual(get_tier_by_order_count(0)["id"], "starter")
        self.assertEqual(get_tier_by_order_count(2)["id"], "starter")
        self.assertEqual(get_tier_by_order_count(3)["id"], "bronze")
        self.assertEqual(get_tier_by_order_count(5)["id"], "bronze")
        self.assertEqual(get_tier_by_order_count(6)["id"], "silver")
        self.assertEqual(get_tier_by_order_count(10)["id"], "silver")
        self.assertEqual(get_tier_by_order_count(11)["id"], "gold")
        self.assertEqual(get_tier_by_order_count(20)["id"], "gold")
        self.assertEqual(get_tier_by_order_count(21)["id"], "platinum")
        self.assertEqual(get_tier_by_order_count(100)["id"], "platinum")

    # 2. Test exclusions: non-completed or refunded orders do not count
    def test_order_exclusions_and_metrics(self):
        db = self.TestingSessionLocal()
        try:
            cust = User(
                telegram_id="web:metrics@example.com",
                username="metricsuser",
                email="metrics@example.com",
            )
            db.add(cust)
            db.flush()

            # Completed order ($20)
            db.add(Order(
                order_code="ORD-COMP-1", user_id=cust.id, service_id=self.service_id, link="web_order",
                quantity=1, amount_usdt=20.0, status="completed"
            ))
            # Delivered order ($30)
            db.add(Order(
                order_code="ORD-DELIV-1", user_id=cust.id, service_id=self.service_id, link="web_order",
                quantity=1, amount_usdt=30.0, status="delivered"
            ))
            # Pending order (should be excluded)
            db.add(Order(
                order_code="ORD-PEND-1", user_id=cust.id, service_id=self.service_id, link="web_order",
                quantity=1, amount_usdt=50.0, status="pending"
            ))
            # Cancelled order (should be excluded)
            db.add(Order(
                order_code="ORD-CANC-1", user_id=cust.id, service_id=self.service_id, link="web_order",
                quantity=1, amount_usdt=50.0, status="cancelled"
            ))
            # Fully refunded order (status == refunded) (should be excluded)
            db.add(Order(
                order_code="ORD-REF-1", user_id=cust.id, service_id=self.service_id, link="web_order",
                quantity=1, amount_usdt=40.0, status="refunded"
            ))
            # Order with refunded_at set (should be excluded)
            db.add(Order(
                order_code="ORD-REF-2", user_id=cust.id, service_id=self.service_id, link="web_order",
                quantity=1, amount_usdt=40.0, status="completed", refunded_at=datetime.utcnow()
            ))
            # Partial refund on completed order: amount=50, refund=15 -> net=35
            db.add(Order(
                order_code="ORD-PART-1", user_id=cust.id, service_id=self.service_id, link="web_order",
                quantity=1, amount_usdt=50.0, refund_amount=15.0, status="completed"
            ))
            db.commit()

            metrics = calculate_customer_order_metrics(db, cust.id)
            # Total completed non-fully-refunded = 1 (completed) + 1 (delivered) + 1 (partially refunded) = 3 orders
            self.assertEqual(metrics["completed_orders"], 3)
            # Net spend = 20 + 30 + (50 - 15) = 85.0
            self.assertEqual(metrics["lifetime_spend"], 85.0)

            # Customer with 3 completed orders reaches Bronze
            loyalty = compute_customer_loyalty(db, cust)
            self.assertEqual(loyalty["id"], "bronze")
            self.assertEqual(loyalty["level"], 2)
            self.assertEqual(loyalty["discount_pct"], 2.0)
            self.assertEqual(loyalty["next_tier_name"], "Silver")
            self.assertEqual(loyalty["orders_to_next_tier"], 3)  # Silver requires 6 (6 - 3 = 3)
        finally:
            db.close()

    # 3. Test Admin Loyalty Tier Override
    def test_admin_loyalty_override(self):
        db = self.TestingSessionLocal()
        try:
            cust = User(
                telegram_id="web:override@example.com",
                username="overrideuser",
                email="override@example.com",
                loyalty_tier_override="platinum",  # Force Platinum on 0 orders
            )
            db.add(cust)
            db.commit()

            loyalty = compute_customer_loyalty(db, cust)
            self.assertEqual(loyalty["id"], "platinum")
            self.assertEqual(loyalty["level"], 5)
            self.assertEqual(loyalty["discount_pct"], 8.0)
            self.assertTrue(loyalty["is_override"])
            self.assertTrue(loyalty["is_highest"])

            # Remove override -> should revert to Starter (0 completed orders)
            cust.loyalty_tier_override = None
            db.commit()

            loyalty_reverted = compute_customer_loyalty(db, cust)
            self.assertEqual(loyalty_reverted["id"], "starter")
            self.assertEqual(loyalty_reverted["discount_pct"], 0.0)
            self.assertFalse(loyalty_reverted["is_override"])
        finally:
            db.close()

    # 4. Test Discount Precedence Rules (Whichever is higher, no stacking)
    def test_discount_precedence_rules(self):
        db = self.TestingSessionLocal()
        try:
            service = db.get(Service, self.service_id)  # $100 list price

            # Case A: User has 4% loyalty (Silver) and 10% personal discount
            # Personal discount is higher -> Personal discount wins
            cust_a = User(
                telegram_id="web:preced_a@example.com",
                email="preced_a@example.com",
                loyalty_tier_override="silver",  # 4%
            )
            db.add(cust_a)
            db.flush()
            disc_a = UserProductDiscount(
                user_id=cust_a.id,
                service_id=service.id,
                discount_type="percent",
                value=10.0,
                is_active=True,
            )
            db.add(disc_a)
            db.commit()

            quote_a = resolve_unit_price(db, service, cust_a)
            self.assertEqual(quote_a.discount_source, "personal")
            self.assertEqual(quote_a.discount_pct, 10.0)
            self.assertEqual(quote_a.unit_price, 90.0)  # 100 - 10%

            # Case B: User has 6% loyalty (Gold) and 2% personal discount
            # Loyalty discount is higher -> Loyalty discount wins
            cust_b = User(
                telegram_id="web:preced_b@example.com",
                email="preced_b@example.com",
                loyalty_tier_override="gold",  # 6%
            )
            db.add(cust_b)
            db.flush()
            disc_b = UserProductDiscount(
                user_id=cust_b.id,
                service_id=service.id,
                discount_type="percent",
                value=2.0,
                is_active=True,
            )
            db.add(disc_b)
            db.commit()

            quote_b = resolve_unit_price(db, service, cust_b)
            self.assertEqual(quote_b.discount_source, "loyalty")
            self.assertEqual(quote_b.discount_pct, 6.0)
            self.assertEqual(quote_b.unit_price, 94.0)  # 100 - 6% = 94.00

            # Case C: No stacking occurred
            self.assertNotEqual(quote_b.unit_price, 92.0)  # NOT 100 - (6+2)%
        finally:
            db.close()

    # 5. Test API /api/web/account/dashboard includes loyalty payload
    def test_api_account_dashboard_loyalty(self):
        client = self._login_john()
        res = client.get("/api/web/account/dashboard")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertIn("loyalty", data)
        loyalty = data["loyalty"]
        self.assertIn("id", loyalty)
        self.assertIn("discount_pct", loyalty)
        self.assertIn("progress_percent", loyalty)
        self.assertIn("summary_text", loyalty)
        self.assertIn("active_accounts_preview", data)
        self.assertIn("open_claims_preview", data)

    # 6. Test API /api/web/products returns dynamic loyalty price
    def test_api_products_with_loyalty_discount(self):
        db = self.TestingSessionLocal()
        try:
            john = db.get(User, self.user_id)
            john.loyalty_tier_override = "gold"  # 6% discount
            db.commit()
        finally:
            db.close()

        try:
            client = self._login_john()
            res = client.get(f"/api/web/products/PRO-SUBSCRIPTION-100")
            self.assertEqual(res.status_code, 200)
            data = res.json()
            # Sell price should be $94.00 (100 - 6%)
            self.assertEqual(data["sell_price"], 94.0)
            self.assertEqual(data["original_price"], 100.0)
            self.assertEqual(data["discount_pct"], 6.0)
            self.assertEqual(data["discount_source"], "loyalty")
            self.assertIn("Gold Member", data["loyalty_badge"])
        finally:
            # Revert override
            db = self.TestingSessionLocal()
            try:
                john = db.get(User, self.user_id)
                john.loyalty_tier_override = None
                db.commit()
            finally:
                db.close()

    # 7. Test API /api/web/checkout applies and records loyalty discount
    def test_api_web_checkout_with_loyalty_discount(self):
        db = self.TestingSessionLocal()
        try:
            john = db.get(User, self.user_id)
            john.loyalty_tier_override = "silver"  # 4% discount
            db.commit()
        finally:
            db.close()

        try:
            client = self._login_john()
            payload = {
                "email": "john@example.com",
                "items": [{"sku": "PRO-SUBSCRIPTION-100", "qty": 1}],
                "payment_method": "TRC20",
            }
            res = client.post("/api/web/checkout", json=payload)
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertTrue(data["ok"])
            order_item = data["orders"][0]
            # $100 - 4% = $96.00
            self.assertEqual(order_item["amount"], 96.0)
            self.assertEqual(order_item["unit_price"], 96.0)
            self.assertEqual(order_item["discount_pct"], 4.0)
            self.assertEqual(order_item["discount_source"], "loyalty")

            # Check database record
            db = self.TestingSessionLocal()
            try:
                order = db.query(Order).filter(Order.order_code == data["order_code"]).first()
                self.assertIsNotNone(order)
                self.assertEqual(order.applied_discount_pct, 4.0)
                self.assertEqual(order.discount_source, "loyalty")
                self.assertEqual(order.amount_usdt, 96.0)
                self.assertIn("Silver Member • 4% off", order.note)
            finally:
                db.close()
        finally:
            # Revert override
            db = self.TestingSessionLocal()
            try:
                john = db.get(User, self.user_id)
                john.loyalty_tier_override = None
                db.commit()
            finally:
                db.close()

    # 8. Test Admin Set Loyalty Tier Route
    def test_admin_set_loyalty_tier_route(self):
        async def _bypass_csrf(req, nxt):
            return await nxt(req)

        with patch("admin.routes.admin_required", return_value=None), \
             patch("main.AdminCSRFMiddleware.dispatch", side_effect=_bypass_csrf):
            client = TestClient(app)
            res = client.post(
                f"/admin/users/{self.user_id}/set-loyalty-tier",
                data={"loyalty_tier": "gold"},
                follow_redirects=False,
            )
            self.assertEqual(res.status_code, 303)

            db = self.TestingSessionLocal()
            try:
                user = db.get(User, self.user_id)
                self.assertEqual(user.loyalty_tier_override, "gold")
                loyalty = compute_customer_loyalty(db, user)
                self.assertEqual(loyalty["id"], "gold")
                self.assertEqual(loyalty["discount_pct"], 6.0)

                # Reset to Auto
                res2 = client.post(
                    f"/admin/users/{self.user_id}/set-loyalty-tier",
                    data={"loyalty_tier": ""},
                    follow_redirects=False,
                )
                self.assertEqual(res2.status_code, 303)
                db.refresh(user)
                self.assertIsNone(user.loyalty_tier_override)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
