import os
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.web import (
    category_payload,
    cors_allow_origins,
    product_payload,
    router,
    shop_payload,
)
from database.models import get_db
from utils.helpers import get_mini_app_url, normalize_mini_app_url, resolve_telegram_mini_app_url


class _FakeStock:
    def __init__(self, available=3):
        self.available_qty = available
        self.login_details = None
        self.quantity = available
        self.reserved_qty = 0


class _FakeSale:
    def __init__(self, original, sale_price, *, active=True, ended=False):
        self.is_active = active
        self.original_price = original
        self.sale_price = sale_price
        self.starts_at = None
        self.ends_at = datetime.utcnow() - timedelta(hours=1) if ended else None


class _FakeCategory:
    def __init__(self):
        self.id = 7
        self.name = "AI Tools"
        self.emoji = "5368324170671202286|🤖"
        self.description = "Premium <b>AI</b> access"
        self.sort_order = 2
        self.is_active = True


class _FakeService:
    def __init__(self, *, stock=3, deleted=False, active=True):
        self.id = 11
        self.sku = "CHATGPT-PLUS"
        self.name = "ChatGPT Plus"
        self.description = "Monthly Plus access."
        self.warranty = "2 months replacement"
        self.emoji = "✨"
        self.image_path = "/admin/static/uploads/services/svc.png"
        self.sell_price = 4.5
        self.min_qty = 1
        self.max_qty = 5
        self.sort_order = 1
        self.is_active = active
        self.is_deleted = deleted
        self.fulfillment_type = "auto"
        self.category = _FakeCategory()
        self.stock = _FakeStock(stock)
        self.sales = [_FakeSale(6.0, 4.5)]


class WebCatalogPayloadTests(unittest.TestCase):
    def test_category_payload_uses_fallback_emoji_and_slug(self):
        payload = category_payload(_FakeCategory())
        self.assertEqual(payload["id"], 7)
        self.assertEqual(payload["emoji"], "🤖")
        self.assertEqual(payload["slug"], "ai-tools")
        self.assertEqual(payload["description"], "Premium AI access")

    @patch("api.web.get_public_base_url", return_value="https://shop.example.com")
    def test_product_payload_matches_mini_app_fields(self, _mock_base):
        payload = product_payload(_FakeService())
        self.assertEqual(payload["sku"], "CHATGPT-PLUS")
        self.assertEqual(payload["sell_price"], 4.5)
        self.assertEqual(payload["original_price"], 6.0)
        self.assertEqual(payload["category_id"], 7)
        self.assertEqual(payload["category"], "AI Tools")
        self.assertTrue(payload["in_stock"])
        self.assertEqual(payload["stock_label"], "In stock")
        self.assertEqual(payload["stock"], 3)
        self.assertEqual(payload["delivery_type"], "instant")
        self.assertEqual(payload["warranty_label"], "2 months replacement")
        self.assertEqual(payload["warranty_percent"], 70)
        self.assertEqual(payload["platform"], "web")
        self.assertTrue(payload["is_free"] is False)
        self.assertIn("live", payload["badges"])
        self.assertIn("hot", payload["badges"])
        self.assertEqual(
            payload["image_url"],
            "https://shop.example.com/admin/static/uploads/services/svc.png",
        )

    def test_free_product_badge(self):
        service = _FakeService()
        service.sell_price = 0
        service.sales = []
        payload = product_payload(service)
        self.assertTrue(payload["is_free"])
        self.assertNotIn("hot", payload["badges"])

    def test_out_of_stock_label(self):
        payload = product_payload(_FakeService(stock=0))
        self.assertFalse(payload["in_stock"])
        self.assertEqual(payload["stock_label"], "Out of stock")

    def test_normalize_mini_app_url(self):
        self.assertEqual(
            normalize_mini_app_url("https://shop.vercel.app/"),
            "https://shop.vercel.app",
        )
        self.assertEqual(normalize_mini_app_url("shop.vercel.app"), "https://shop.vercel.app")
        self.assertIsNone(normalize_mini_app_url("http://evil.example"))
        self.assertEqual(
            normalize_mini_app_url("http://localhost:3000"),
            "http://localhost:3000",
        )

    def test_cors_defaults_to_any_origin_for_public_catalog(self):
        with patch.dict(os.environ, {"CORS_ORIGINS": ""}, clear=False):
            self.assertEqual(cors_allow_origins(), "*")

    def test_mini_app_url_falls_back_to_mini_path(self):
        db = _FakeDB([], [])
        db.config = None
        with patch.dict(os.environ, {"MINI_APP_URL": ""}, clear=False):
            with patch("utils.helpers.get_public_base_url", return_value="https://shop.example.com"):
                self.assertEqual(get_mini_app_url(db), "https://shop.example.com/mini")


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def options(self, *args, **kwargs):
        return self

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def group_by(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._rows[0] if self._rows else 0


class _FakeLanguage:
    def __init__(self):
        self.code = "en"
        self.name = "English"
        self.flag = "🇬🇧"
        self.is_active = True
        self.sort_order = 1


class _FakeConfig:
    support_whatsapp = "923001234567"
    support_url = "https://t.me/smfshop"
    support_username = "smfshop"
    usd_to_pkr_rate = 280.0
    mini_app_url = None


class _FakePaymentMethod:
    def __init__(self):
        self.id = 1
        self.name = "PayFast"
        self.code = "PAYFAST"
        self.method_type = "auto"
        self.network = None
        self.address = None
        self.icon = "💳"
        self.image_path = None
        self.instructions = "Pay with PayFast"
        self.is_active = True
        self.sort_order = 1


class _FakeDB:
    def __init__(self, services, categories):
        self.services = services
        self.categories = categories
        self.languages = [_FakeLanguage()]
        self.config = _FakeConfig()
        self.order_rows = []
        self.users = []
        self.methods = [_FakePaymentMethod()]
        self._count_values = [2, 5]

    def add(self, obj):
        self.users.append(obj)

    def commit(self):
        return None

    def flush(self):
        return None

    def refresh(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = 1

    def rollback(self):
        return None

    def query(self, *entities):
        first = entities[0] if entities else None
        name = getattr(first, "__name__", "") or getattr(getattr(first, "class_", None), "__name__", "")
        if name == "Category":
            return _FakeQuery(self.categories)
        if name == "Service":
            return _FakeQuery(self.services)
        if name == "BotConfig":
            return _FakeQuery([self.config] if self.config else [])
        if name == "Language":
            return _FakeQuery(self.languages)
        if name == "Order":
            return _FakeQuery(self.order_rows)
        if name == "User":
            return _FakeQuery(self.users)
        if name == "PaymentMethod":
            return _FakeQuery(self.methods)
        value = self._count_values.pop(0) if self._count_values else 0
        return _FakeQuery([value])


class WebCatalogRouterTests(unittest.TestCase):
    def setUp(self):
        self.service = _FakeService()
        self.category = self.service.category
        self.db = _FakeDB([self.service], [self.category])
        app = FastAPI()
        app.include_router(router)

        def override_db():
            yield self.db

        app.dependency_overrides[get_db] = override_db
        self.client = TestClient(app)

    def test_categories_endpoint(self):
        response = self.client.get("/api/web/categories")
        self.assertEqual(response.status_code, 200)
        rows = response.json()
        self.assertEqual(rows[0]["slug"], "ai-tools")
        self.assertEqual(rows[0]["emoji"], "🤖")

    def test_products_endpoint(self):
        response = self.client.get("/api/web/products")
        self.assertEqual(response.status_code, 200)
        rows = response.json()
        self.assertEqual(rows[0]["sku"], "CHATGPT-PLUS")
        self.assertEqual(rows[0]["in_stock"], True)

    def test_product_by_sku(self):
        response = self.client.get("/api/web/products/CHATGPT-PLUS")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["name"], "ChatGPT Plus")

    def test_product_missing(self):
        self.db.services = []
        response = self.client.get("/api/web/products/MISSING")
        self.assertEqual(response.status_code, 404)

    def test_stats_endpoint(self):
        response = self.client.get("/api/web/stats")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {"customers": 2, "orders_completed": 5, "usd_to_pkr_rate": 280.0},
        )

    def test_shop_endpoint_hides_fx_ticker(self):
        response = self.client.get("/api/web/shop")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["name"], "SMF SHOP")
        self.assertEqual(body["whatsapp_url"], "https://wa.me/923001234567")
        self.assertEqual(body["currency"]["code"], "USD")
        self.assertNotIn("1 USD", str(body))
        self.assertTrue(body["headline"])
        self.assertNotIn("Unlock Premium Access", body["headline"])

    def test_featured_splits_live_hot_best_seller(self):
        response = self.client.get("/api/web/featured")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("live", body)
        self.assertIn("hot", body)
        self.assertIn("best_seller", body)
        self.assertEqual(body["live"][0]["sku"], "CHATGPT-PLUS")
        self.assertEqual(body["hot"][0]["sku"], "CHATGPT-PLUS")
        self.assertEqual(body["best_seller"][0]["sell_price"], 4.5)

    def test_shop_payload_uses_admin_whatsapp(self):
        payload = shop_payload(self.db)
        self.assertEqual(payload["languages"][0]["code"], "en")
        self.assertEqual(payload["pkr_rate"], 280.0)
        self.assertEqual(payload["currencies"][0]["flag"], "🇺🇸")
        self.assertEqual(payload["currencies"][0]["flag_iso"], "us")
        self.assertEqual(payload["currencies"][1]["flag_iso"], "pk")
        self.assertEqual(payload["languages"][0]["flag_iso"], "gb")
        self.assertEqual(payload["currencies"][1]["flag"], "🇵🇰")

    def test_payment_methods_endpoint(self):
        response = self.client.get("/api/web/payment-methods")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["code"], "PAYFAST")

    @patch("api.web.reserve_stock")
    def test_checkout_creates_pending_order(self, _reserve):
        response = self.client.post(
            "/api/web/checkout",
            json={
                "email": "buyer@example.com",
                "name": "Buyer",
                "payment_method": "PAYFAST",
                "items": [{"sku": "CHATGPT-PLUS", "qty": 1}],
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["order_code"].startswith("SMM-"))
        self.assertEqual(body["payment_method"]["code"], "PAYFAST")

    def test_signup_creates_email_account(self):
        response = self.client.post(
            "/api/web/signup",
            json={"name": "Ayesha", "email": "ayesha@example.com", "password": "secret1"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["user"]["email"], "ayesha@example.com")
        self.assertEqual(body["user"]["name"], "Ayesha")


class MiniAppUrlTests(unittest.TestCase):
    def test_vercel_sample_storefront_uses_hosted_mini(self):
        self.assertEqual(
            resolve_telegram_mini_app_url(
                "https://aurex-shop-web.vercel.app",
                public_base="https://web-production-80fac.up.railway.app",
            ),
            "https://web-production-80fac.up.railway.app/mini",
        )

    def test_custom_domain_is_kept(self):
        self.assertEqual(
            resolve_telegram_mini_app_url(
                "https://shop.example.com",
                public_base="https://web-production-80fac.up.railway.app",
            ),
            "https://shop.example.com",
        )

    def test_empty_falls_back_to_hosted(self):
        self.assertEqual(
            resolve_telegram_mini_app_url(
                None,
                public_base="https://web-production-80fac.up.railway.app",
            ),
            "https://web-production-80fac.up.railway.app/mini",
        )

    def test_vercel_without_public_base_still_uses_live_host(self):
        self.assertEqual(
            resolve_telegram_mini_app_url(
                "https://aurex-shop-web.vercel.app/",
                public_base="",
            ),
            "https://web-production-80fac.up.railway.app/mini",
        )


class MiniAppDesignTests(unittest.TestCase):
    def test_live_mini_app_includes_designed_catalog(self):
        html = Path("static/mini-app/index.html").read_text(encoding="utf-8")
        css = Path("static/mini-app/styles.css").read_text(encoding="utf-8")
        js = Path("static/mini-app/app.js").read_text(encoding="utf-8")
        for needle in (
            "SMF SHOP",
            "Home",
            "Subscription",
            "Freebies",
            "Sign up",
            "Explore Products",
            "Order on WhatsApp",
            "Live",
            "Hot",
            "Best Seller",
            "btn-cart",
            "currency-btn",
            "language-btn",
            'id="cart-overlay" hidden',
            'id="signup-form"',
            "#/subscription",
            "#/freebies",
            "Place order",
            "Direct checkout",
            "flag-img",
            "/static/mini-app/flags/us.svg",
            "/static/mini-app/flags/gb.svg",
        ):
            self.assertIn(needle, html)
        self.assertNotIn("1 USD =", html)
        self.assertNotIn("Unlock Premium Access", html)
        self.assertNotIn("from the Telegram bot to sign in automatically", html)
        self.assertIn("[hidden]", css)
        self.assertIn(".product-card", css)
        self.assertIn(".btn-add-cart", css)
        self.assertTrue(Path("static/mini-app/brand/smf-logo.svg").exists())
        self.assertTrue(Path("static/mini-app/flags/pk.svg").exists())
        self.assertTrue(Path("static/mini-app/flags/us.svg").exists())
        self.assertTrue(Path("static/mini-app/flags/gb.svg").exists())
        self.assertIn("/api/web/featured", js)
        self.assertIn("/api/web/shop", js)
        self.assertIn("/api/web/checkout", js)
        self.assertIn("data-remove", js)
        self.assertIn("#/checkout", js)
        self.assertIn("Direct checkout", js)
        self.assertIn("Add to Cart", js)
        self.assertIn("VIEW NOTE", js)
        self.assertIn("product-card", js)
        self.assertIn("flag-img", js)


if __name__ == "__main__":
    unittest.main()
