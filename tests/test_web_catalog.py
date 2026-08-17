import os
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.web import (
    category_payload,
    cors_allow_origins,
    product_payload,
    router,
)
from database.models import get_db
from utils.helpers import normalize_mini_app_url


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
        self.assertEqual(
            payload["image_url"],
            "https://shop.example.com/admin/static/uploads/services/svc.png",
        )

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


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def options(self, *args, **kwargs):
        return self

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar(self):
        return self._rows[0] if self._rows else 0


class _FakeDB:
    def __init__(self, services, categories):
        self.services = services
        self.categories = categories
        self._count_values = [2, 5]

    def query(self, model):
        name = getattr(model, "__name__", "")
        if name == "Category":
            return _FakeQuery(self.categories)
        if name == "Service":
            return _FakeQuery(self.services)
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
        self.assertEqual(response.json(), {"customers": 2, "orders_completed": 5})


if __name__ == "__main__":
    unittest.main()
