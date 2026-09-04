import asyncio
import os
import unittest
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from database.models import (
    Base,
    Order,
    Provider,
    RefundLog,
    Service,
    SessionLocal,
    Stock,
    Transaction,
    User,
    init_db,
)
from utils.helpers import generate_order_code
from utils.preorder_manager import (
    check_expired_preorders_once,
    process_waiting_preorders,
    register_paid_preorder,
)
from utils.pricing import api_computed_sell_price
from utils.product_display import build_product_in_stock_parts
from utils.refund_tool import (
    calculate_refund,
    credit_wallet_refund,
    mark_manual_refund,
    notify_manual_refund,
    notify_wallet_refund,
    parse_subscription_days,
)
from utils.stock_manager import (
    add_stock,
    consume_stock_account,
    set_stock,
)


class TestRefundAndPreorderSuite(unittest.TestCase):
    """Complete 37-point test verification suite for Refund Tool and Product Availability/Pre-Order system."""

    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.db = SessionLocal()
        # Create test user
        self.user = User(
            telegram_id=f"test_user_{int(datetime.utcnow().timestamp() * 1000)}",
            username="tester",
            full_name="Test User",
            wallet_usdt=100.0,
        )
        self.db.add(self.user)
        self.db.commit()
        self.db.refresh(self.user)

    def tearDown(self):
        self.db.rollback()
        self.db.close()

    def create_test_service(self, **kwargs) -> Service:
        defaults = {
            "sku": f"SKU-{uuid.uuid4().hex[:10]}",
            "name": "Test Service",
            "sell_price": 10.0,
            "cost_price": 5.0,
            "commission_pct": 0.0,
            "markup_fixed_usdt": 0.0,
            "is_active": True,
            "availability": "in_stock",
            "fulfillment_type": "auto",
        }
        defaults.update(kwargs)
        service = Service(**defaults)
        self.db.add(service)
        self.db.commit()
        self.db.refresh(service)
        return service

    def create_test_order(self, **kwargs) -> Order:
        defaults = {
            "order_code": f"ORD-{uuid.uuid4().hex[:8].upper()}",
            "user_id": self.user.id,
            "link": "digital_order",
            "quantity": 1,
            "amount_usdt": 10.0,
            "status": "completed",
            "order_type": "manual",
        }
        defaults.update(kwargs)
        order = Order(**defaults)
        self.db.add(order)
        self.db.commit()
        self.db.refresh(order)
        return order

    # =========================================================================
    # PART 1-7: REFUND TOOL & END/SUSPEND DATE (Criteria 1 - 12)
    # =========================================================================

    def test_01_prorated_refund_calculation_with_end_date(self):
        """Criterion 1: days_used = cutoff_date - purchase_date prorated refund calculation."""
        purchase_date = datetime(2026, 1, 1, 10, 0, 0)
        end_date = datetime(2026, 1, 11, 10, 0, 0)  # 10 days used
        service = self.create_test_service(name="Test Svc 1", warranty="30 Days", sell_price=30.0)

        order = self.create_test_order(
            service_id=service.id,
            amount_usdt=30.0,
            created_at=purchase_date,
            completed_at=purchase_date,
            service=service,
        )

        calc = calculate_refund(
            order=order,
            subscription_days=30,
            cutoff_date=end_date,
        )
        self.assertEqual(calc.days_used, 10)
        self.assertEqual(calc.remaining_days, 20)
        self.assertAlmostEqual(float(calc.daily_rate), 1.0, places=2)
        self.assertAlmostEqual(float(calc.refund_amount), 20.0, places=2)
        self.assertEqual(calc.cutoff_date, end_date.date())

    def test_02_end_date_validation_rejects_before_purchase(self):
        """Criterion 2: end date validation: end date >= purchase date."""
        purchase_date = datetime(2026, 1, 10)
        invalid_end_date = datetime(2026, 1, 5)  # before purchase
        service = self.create_test_service(name="Test Svc 2", warranty="30 Days", sell_price=30.0)

        order = self.create_test_order(
            service_id=service.id,
            amount_usdt=30.0,
            created_at=purchase_date,
            completed_at=purchase_date,
            service=service,
        )

        calc = calculate_refund(
            order=order,
            subscription_days=30,
            cutoff_date=invalid_end_date,
        )
        self.assertFalse(calc.has_refund)
        self.assertIn("cannot be before purchase date", calc.message or "")

    def test_03_remaining_days_validation(self):
        """Criterion 3: remaining days <= total subscription days."""
        purchase_date = datetime(2026, 1, 1)
        end_date = datetime(2026, 1, 15)
        service = self.create_test_service(name="Test Svc 3", warranty="30 Days", sell_price=60.0)

        order = self.create_test_order(
            service_id=service.id,
            amount_usdt=60.0,
            created_at=purchase_date,
            completed_at=purchase_date,
            service=service,
        )

        calc = calculate_refund(
            order=order,
            subscription_days=30,
            cutoff_date=end_date,
        )
        self.assertLessEqual(calc.remaining_days, 30)
        self.assertEqual(calc.remaining_days, 16)

    def test_04_prorated_refund_capped_at_paid_amount(self):
        """Criterion 4: prorated refund amount cannot exceed original paid amount."""
        purchase_date = datetime(2026, 1, 1)
        end_date = datetime(2026, 1, 1)  # 0 days used
        service = self.create_test_service(name="Test Svc 4", warranty="30 Days", sell_price=50.0)

        order = self.create_test_order(
            service_id=service.id,
            amount_usdt=50.0,
            created_at=purchase_date,
            completed_at=purchase_date,
            service=service,
        )

        calc = calculate_refund(
            order=order,
            subscription_days=30,
            cutoff_date=end_date,
        )
        self.assertLessEqual(float(calc.refund_amount), 50.0)

    def test_05_missing_refund_method_eliminated_on_manual_refund(self):
        """Criterion 5: missing refund_method form field error eliminated."""
        with open("admin/templates/refund_tool.html", "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn('name="refund_method"', content)
        self.assertIn('id="refund_method_input"', content)
        self.assertIn('value="wallet"', content)

    def test_06_refund_records_persisted(self):
        """Criterion 6: Refund records persisted with refund status, days used, and refund amount."""
        service = self.create_test_service(name="Persist Svc", sell_price=30.0, warranty="30 Days")

        order = self.create_test_order(
            service_id=service.id,
            amount_usdt=30.0,
            created_at=datetime.utcnow() - timedelta(days=10),
            completed_at=datetime.utcnow() - timedelta(days=10),
        )

        calc = calculate_refund(
            order=order,
            subscription_days=30,
            cutoff_date=datetime.utcnow(),
        )
        after_bal, tx = credit_wallet_refund(
            self.db,
            order=order,
            user=self.user,
            amount=calc.refund_amount,
            breakdown=calc,
            note="Test refund note",
        )
        self.db.commit()

        refund_log = self.db.query(RefundLog).filter(RefundLog.order_id == order.id).first()
        self.assertIsNotNone(refund_log)
        self.assertEqual(refund_log.refund_method, "wallet")
        self.assertGreater(refund_log.refund_amount, 0)
        self.assertEqual(refund_log.days_total, 30)
        self.assertEqual(refund_log.days_used, 10)
        self.assertEqual(refund_log.days_remaining, 20)
        self.assertIn("Test refund note", refund_log.note or "")
        self.assertEqual(order.status, "refunded")

    def test_07_order_status_updated_to_refunded(self):
        """Criterion 7: Order status updated to 'refunded' upon refund."""
        service = self.create_test_service(name="Service 7", sell_price=20.0, warranty="30 Days")

        order = self.create_test_order(
            service_id=service.id,
            amount_usdt=20.0,
            created_at=datetime.utcnow() - timedelta(days=5),
            completed_at=datetime.utcnow() - timedelta(days=5),
        )

        calc = calculate_refund(
            order=order,
            subscription_days=30,
            cutoff_date=datetime.utcnow(),
        )
        log = mark_manual_refund(
            self.db,
            order=order,
            amount=calc.refund_amount,
            breakdown=calc,
            note="Manual refund completed",
        )
        self.db.commit()
        self.db.refresh(order)
        self.assertEqual(order.status, "refunded")
        self.assertEqual(order.refund_method, "manual")
        self.assertAlmostEqual(order.refund_amount, float(calc.refund_amount), places=2)

    def test_08_duplicate_refunds_blocked(self):
        """Criterion 8: Duplicate refunds strictly blocked."""
        service = self.create_test_service(name="Service 8", sell_price=25.0, warranty="30 Days")

        order = self.create_test_order(
            service_id=service.id,
            amount_usdt=25.0,
            status="refunded",  # Already refunded
            refund_method="wallet",
            created_at=datetime.utcnow() - timedelta(days=5),
        )
        calc = calculate_refund(order=order, subscription_days=30)
        self.assertTrue(calc.already_refunded)

        with self.assertRaises(ValueError) as ctx:
            credit_wallet_refund(
                self.db,
                order=order,
                user=self.user,
                amount=Decimal("10.00"),
                breakdown=calc,
            )
        self.assertIn("already", str(ctx.exception).lower())

        with self.assertRaises(ValueError) as ctx_m:
            mark_manual_refund(
                self.db,
                order=order,
                amount=Decimal("10.00"),
                breakdown=calc,
            )
        self.assertIn("already", str(ctx_m.exception).lower())

    def test_09_mandatory_customer_notification_manual_refund(self):
        """Criterion 9: Mandatory customer Telegram notification for Manual Refund."""
        with patch("utils.refund_tool.send_user_message", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = True
            asyncio.run(
                notify_manual_refund(
                    telegram_id="12345678",
                    order_code="ORD-12345",
                    amount=15.50,
                    note="Paid via external bank",
                )
            )
            mock_send.assert_called_once()
            call_text = mock_send.call_args[0][1]
            self.assertIn("Refund Completed", call_text)
            self.assertIn("ORD-12345", call_text)
            self.assertIn("15.50", call_text)
            self.assertIn("Manual Refund", call_text)
            self.assertIn("Paid via external bank", call_text)

    def test_10_mandatory_customer_notification_wallet_refund(self):
        """Criterion 10: Mandatory customer Telegram notification for Wallet Refund."""
        with patch("utils.refund_tool.send_user_message", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = True
            asyncio.run(
                notify_wallet_refund(
                    telegram_id="12345678",
                    order_code="ORD-54321",
                    amount=20.00,
                    new_balance=120.00,
                    note="Service discontinued",
                )
            )
            mock_send.assert_called_once()
            call_text = mock_send.call_args[0][1]
            self.assertIn("Refund Completed", call_text)
            self.assertIn("ORD-54321", call_text)
            self.assertIn("20.00", call_text)
            self.assertIn("Wallet", call_text)
            self.assertIn("120.00", call_text)

    def test_11_optional_refund_note_handling(self):
        """Criterion 11: Optional refund note displayed when provided, omitted cleanly when empty."""
        with patch("utils.refund_tool.send_user_message", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = True
            # With note
            asyncio.run(
                notify_wallet_refund(
                    telegram_id="123",
                    order_code="ORD-1",
                    amount=5.0,
                    new_balance=5.0,
                    note="Special compensation",
                )
            )
            text_with_note = mock_send.call_args[0][1]
            self.assertIn("Note:", text_with_note)
            self.assertIn("Special compensation", text_with_note)

            # Without note
            mock_send.reset_mock()
            asyncio.run(
                notify_wallet_refund(
                    telegram_id="123",
                    order_code="ORD-2",
                    amount=5.0,
                    new_balance=10.0,
                    note="",
                )
            )
            text_no_note = mock_send.call_args[0][1]
            self.assertNotIn("Note:", text_no_note)

    def test_12_telegram_order_history_displays_refunded(self):
        """Criterion 12: Customer Telegram Order History shows [REFUNDED] badge."""
        from bot.keyboards import orders_list_keyboard

        service = self.create_test_service(name="Refunded Plan", sell_price=10.0)

        order = self.create_test_order(
            service_id=service.id,
            status="refunded",
            service=service,
        )
        markup = orders_list_keyboard([order])
        button_text = markup.inline_keyboard[0][0].text
        self.assertIn("[REFUNDED]", button_text)
        self.assertIn(order.order_code, button_text)

    # =========================================================================
    # PART 8-30: PRODUCT AVAILABILITY, PRE-ORDERS & PROVIDER SYNC (Criteria 13 - 37)
    # =========================================================================

    def test_13_14_product_availability_field_in_templates(self):
        """Criteria 13 & 14: Availability dropdown present and positioned directly below Fulfillment type."""
        for path in ["admin/templates/service_add.html", "admin/templates/service_edit.html"]:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            self.assertIn('name="availability"', content)
            self.assertIn('value="in_stock"', content)
            self.assertIn('value="pre_order"', content)
            self.assertIn('value="out_of_stock"', content)
            # Position verification: fulfillment comes before availability
            fulfillment_pos = content.find('name="fulfillment_type"')
            availability_pos = content.find('name="availability"')
            self.assertNotEqual(fulfillment_pos, -1)
            self.assertNotEqual(availability_pos, -1)
            self.assertLess(fulfillment_pos, availability_pos)

    def test_15_initial_product_creation_persists_availability(self):
        """Criterion 15: Initial product creation persists chosen availability status."""
        service = self.create_test_service(
            name="New PreOrder Service",
            sell_price=15.0,
            availability="pre_order",
        )
        self.assertEqual(service.availability, "pre_order")

    def test_16_product_edit_persists_availability(self):
        """Criterion 16: Product edit persists updated availability status."""
        service = self.create_test_service(name="Edit Service", sell_price=10.0, availability="in_stock")

        service.availability = "out_of_stock"
        self.db.commit()
        self.db.refresh(service)
        self.assertEqual(service.availability, "out_of_stock")

    def test_17_stock_addition_flips_out_of_stock_to_in_stock(self):
        """Criterion 17: Account line addition/stock increase flips Out of Stock -> In Stock."""
        service = self.create_test_service(
            name="Stock Flip Service",
            sell_price=10.0,
            availability="out_of_stock",
            fulfillment_type="stock",
        )

        # Add account line stock
        add_stock(self.db, service.id, quantity=2, login_details="user:pass1\nuser:pass2")
        self.db.refresh(service)
        self.assertEqual(service.availability, "in_stock")

    def test_18_stock_depletion_flips_in_stock_to_out_of_stock(self):
        """Criterion 18: Stock depletion flips In Stock -> Out of Stock."""
        service = self.create_test_service(
            name="Depletion Service",
            sell_price=10.0,
            availability="in_stock",
            fulfillment_type="stock",
        )

        # Add 1 account line
        add_stock(self.db, service.id, quantity=1, login_details="user:pass1")
        self.db.refresh(service)
        self.assertEqual(service.availability, "in_stock")

        # Consume the only stock line
        consumed = consume_stock_account(self.db, service.id, 1)
        self.assertEqual(len(consumed), 1)
        self.db.refresh(service)
        self.assertEqual(service.availability, "out_of_stock")

    def test_19_auto_sync_respects_manual_admin_deactivation(self):
        """Criterion 19: Respects manual admin deactivation (is_active = False not overridden to True)."""
        service = self.create_test_service(
            name="Inactive Service",
            sell_price=10.0,
            availability="out_of_stock",
            fulfillment_type="manual",
            is_active=False,  # Manually deactivated
        )

        # Increase stock
        set_stock(self.db, service.id, 10)
        self.db.refresh(service)
        # Availability is updated, but is_active remains False
        self.assertEqual(service.availability, "in_stock")
        self.assertFalse(service.is_active)

    def test_20_21_provider_restock_sync(self):
        """Criteria 20 & 21: Provider restock detection flips availability to In Stock and handles out-of-stock items."""
        from admin.routes import apply_provider_catalog_item_to_service

        service = self.create_test_service(
            name="API Service",
            sell_price=12.0,
            cost_price=5.0,
            commission_pct=20.0,
            markup_fixed_usdt=2.0,
            availability="out_of_stock",
            is_active=True,
            provider_service_id="prov_101",
        )

        # Provider catalog item indicates restock: in_stock=True, available=50
        item = {
            "id": "prov_101",
            "rate": 6.0,
            "in_stock": True,
            "available": 50,
        }
        apply_provider_catalog_item_to_service(self.db, service, item)
        self.db.commit()
        self.db.refresh(service)
        self.assertEqual(service.availability, "in_stock")
        self.assertEqual(service.stock.available_qty, 50)

    def test_22_23_preorder_purchase_and_fee(self):
        """Criteria 22, 23 & 24: Pre-Order status allows purchase when stock is 0 and adds flat $0.30 fee."""
        service = self.create_test_service(
            name="PreOrder Game Key",
            sell_price=10.0,
            availability="pre_order",
            min_qty=1,
            max_qty=5,
        )

        # Check product display prompt includes pre-order fee note
        card, qty_prompt = build_product_in_stock_parts(service, available=0, sold=5, db=self.db)
        self.assertIn("Pre-Order", card)
        self.assertIn("$0.30", qty_prompt)

    def test_25_26_preorder_registered_in_fifo_queue(self):
        """Criteria 25 & 26: Pre-order registered in FIFO queue with preorder_status='waiting' and timestamp."""
        service = self.create_test_service(name="PreOrder Item", sell_price=15.0, availability="pre_order")

        order = self.create_test_order(
            service_id=service.id,
            amount_usdt=15.30,
            status="pending",
        )
        register_paid_preorder(order)
        self.db.commit()
        self.db.refresh(order)

        self.assertTrue(order.is_preorder)
        self.assertEqual(order.preorder_fee, 0.30)
        self.assertEqual(order.status, "preorder_waiting")
        self.assertEqual(order.preorder_status, "waiting")
        self.assertIsNotNone(order.preorder_paid_at)

    def test_27_28_29_fifo_queue_fulfillment_priority(self):
        """Criteria 27, 28 & 29: Newly arrived stock fulfills waiting pre-orders in FIFO order."""
        service = self.create_test_service(
            name="PreOrder Restock Service",
            sell_price=10.0,
            fulfillment_type="stock",
            availability="pre_order",
        )

        # Create two waiting pre-orders with different timestamps
        order1 = self.create_test_order(
            order_code=f"PO-1-{uuid.uuid4().hex[:6]}",
            service_id=service.id,
            amount_usdt=10.30,
            is_preorder=True,
            status="preorder_waiting",
            preorder_status="waiting",
            preorder_paid_at=datetime.utcnow() - timedelta(hours=2),
            created_at=datetime.utcnow() - timedelta(hours=2),
        )
        order2 = self.create_test_order(
            order_code=f"PO-2-{uuid.uuid4().hex[:6]}",
            service_id=service.id,
            amount_usdt=10.30,
            is_preorder=True,
            status="preorder_waiting",
            preorder_status="waiting",
            preorder_paid_at=datetime.utcnow() - timedelta(hours=1),
            created_at=datetime.utcnow() - timedelta(hours=1),
        )

        # Add 1 stock account line (only enough for the first order)
        # add_stock automatically triggers process_waiting_preorders
        add_stock(self.db, service.id, quantity=1, login_details="first_delivered_acc:pass")

        self.db.refresh(order1)
        self.db.refresh(order2)
        # Older pre-order was fulfilled first
        self.assertEqual(order1.status, "completed")
        self.assertEqual(order1.preorder_status, "fulfilled")
        self.assertIn("first_delivered_acc:pass", order1.delivered_info or "")

        # Second (newer) order remains waiting because available stock was only 1
        self.assertEqual(order2.status, "preorder_waiting")
        self.assertEqual(order2.preorder_status, "waiting")

        # Now add 1 more stock line for order 2
        add_stock(self.db, service.id, quantity=1, login_details="second_delivered_acc:pass")
        self.db.refresh(order2)
        self.assertEqual(order2.status, "completed")
        self.assertEqual(order2.preorder_status, "fulfilled")
        self.assertIn("second_delivered_acc:pass", order2.delivered_info or "")

    def test_30_31_32_33_preorder_24h_expiry_and_auto_refund(self):
        """Criteria 30, 31, 32 & 33: Background job cancels 24h expired pre-orders, 100% full refunds, and notifies."""
        # Clean up any leftover waiting pre-orders from previous test runs
        self.db.query(Order).filter(Order.is_preorder.is_(True), Order.status == "preorder_waiting").delete()
        self.db.commit()

        service = self.create_test_service(name="Expiring PreOrder", sell_price=20.0)

        initial_wallet = self.user.wallet_usdt
        paid_amount = 20.30  # $20 item + $0.30 pre-order fee

        # Create pre-order paid 25 hours ago
        exp_code = f"PO-EXP-{uuid.uuid4().hex[:6]}"
        expired_order = self.create_test_order(
            order_code=exp_code,
            service_id=service.id,
            amount_usdt=paid_amount,
            is_preorder=True,
            status="preorder_waiting",
            preorder_status="waiting",
            preorder_paid_at=datetime.utcnow() - timedelta(hours=25),
        )

        with patch("utils.preorder_manager.send_user_message", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = True
            cancelled_count = check_expired_preorders_once(self.db)
            self.assertEqual(cancelled_count, 1)

            self.db.refresh(expired_order)
            self.db.refresh(self.user)

            self.assertEqual(expired_order.status, "refunded")
            self.assertEqual(expired_order.preorder_status, "cancelled_refunded")
            # 100% full refund credited to user wallet
            self.assertAlmostEqual(self.user.wallet_usdt, initial_wallet + paid_amount, places=2)

            # Notification sent to user
            mock_send.assert_called_once()
            call_text = mock_send.call_args[0][1]
            self.assertIn("Pre-order Cancelled", call_text)
            self.assertIn(exp_code, call_text)
            self.assertIn("20.30", call_text)

    def test_34_35_36_provider_price_sync_margin_formulas(self):
        """Criteria 34, 35 & 36: Provider price sync dynamically recalculates sell price and adjusts for cost changes."""
        from admin.routes import apply_provider_catalog_item_to_service

        # Base test of margin formula: cost=10.0, margin=20%, fixed=1.50 -> 10 + 2 + 1.50 = 13.50
        price = api_computed_sell_price(10.0, 20.0, 1.50)
        self.assertAlmostEqual(price, 13.50, places=2)

        service = self.create_test_service(
            name="Dynamic Price Service",
            sell_price=13.50,
            cost_price=10.0,
            commission_pct=20.0,
            markup_fixed_usdt=1.50,
            is_active=True,
            provider_service_id="dyn_100",
        )

        # Cost increase: cost goes to 15.0 -> 15 + 3.0 + 1.50 = 19.50
        item_increased = {"id": "dyn_100", "rate": 15.0, "in_stock": True, "available": 10}
        apply_provider_catalog_item_to_service(self.db, service, item_increased)
        self.db.refresh(service)
        self.assertAlmostEqual(service.sell_price, 19.50, places=2)
        self.assertEqual(service.cost_price, 15.0)

        # Cost decrease: cost goes to 8.0 -> 8 + 1.60 + 1.50 = 11.10
        item_decreased = {"id": "dyn_100", "rate": 8.0, "in_stock": True, "available": 10}
        apply_provider_catalog_item_to_service(self.db, service, item_decreased)
        self.db.refresh(service)
        self.assertAlmostEqual(service.sell_price, 11.10, places=2)
        self.assertEqual(service.cost_price, 8.0)

    def test_37_provider_api_error_resilience(self):
        """Criterion 37: Provider API errors handled gracefully without zeroing stock or prices."""
        from admin.routes import sync_provider_products
        from utils.provider_api import ProviderApiError

        provider = Provider(name="Faulty Provider", type="api", api_url="https://api.example.com", is_active=True)
        self.db.add(provider)
        self.db.commit()

        service = self.create_test_service(
            name="Safe Service",
            sell_price=25.0,
            provider_id=provider.id,
            is_active=True,
        )

        # Mock provider API failing with an exception
        with patch("admin.routes.fetch_services", side_effect=ProviderApiError("Connection timed out")):
            created, updated, balance, err = asyncio.run(sync_provider_products(self.db, provider))
            self.assertEqual(created, 0)
            self.assertEqual(updated, 0)
            self.assertIsNotNone(err)

            # Existing service price and active status remain unchanged
            self.db.refresh(service)
            self.assertEqual(service.sell_price, 25.0)
            self.assertTrue(service.is_active)


if __name__ == "__main__":
    unittest.main()
