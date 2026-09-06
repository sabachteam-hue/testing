import os
import re
import unittest
from fastapi.testclient import TestClient
from main import app
from database.models import SessionLocal, Service


class TestAdminCSRFFix(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        admin_pass = os.getenv("ADMIN_PASSWORD", "admin123")
        res = self.client.post(
            "/admin/login",
            data={"username": "admin", "password": admin_pass},
            follow_redirects=True,
        )
        self.assertEqual(res.status_code, 200)

    def tearDown(self):
        db = SessionLocal()
        for name in ("Test Auto Service A", "Test Auto Service B"):
            svc = db.query(Service).filter(Service.name == name).first()
            if svc:
                if svc.stock:
                    db.delete(svc.stock)
                db.delete(svc)
        db.commit()
        db.close()

    def test_add_service_with_form_csrf_token(self):
        # Fetch /admin/services/new
        page = self.client.get("/admin/services/new")
        self.assertEqual(page.status_code, 200)
        
        # Verify csrf_token is present in HTML form
        self.assertIn('name="csrf_token"', page.text)
        token_match = re.search(r'name="csrf_token"\s+value="([^"]+)"', page.text)
        self.assertIsNotNone(token_match)
        token = token_match.group(1)

        # Submit form with csrf_token
        res = self.client.post(
            "/admin/services",
            data={
                "name": "Test Auto Service A",
                "csrf_token": token,
                "description": "Product with template csrf token",
                "fulfillment_type": "auto",
                "sell_price": "19.99",
                "cost_price": "10.00",
                "initial_stock": "5",
            },
            follow_redirects=False,
        )
        # Should redirect to /admin/services on success (302 or 303), NOT return 403 CSRF failed!
        self.assertIn(res.status_code, (302, 303))
        self.assertEqual(res.headers.get("location"), "/admin/services")

        # Verify product created
        db = SessionLocal()
        svc = db.query(Service).filter(Service.name == "Test Auto Service A").first()
        self.assertIsNotNone(svc)
        self.assertEqual(svc.sell_price, 19.99)
        db.close()

    def test_add_service_with_browser_origin(self):
        # Real browser form POST always includes origin / referer header
        res = self.client.post(
            "/admin/services",
            data={
                "name": "Test Auto Service B",
                "description": "Product with browser Origin header",
                "fulfillment_type": "auto",
                "sell_price": "29.99",
                "cost_price": "15.00",
                "initial_stock": "3",
            },
            headers={"origin": "http://testserver", "referer": "http://testserver/admin/services/new"},
            follow_redirects=False,
        )
        self.assertIn(res.status_code, (302, 303))
        self.assertEqual(res.headers.get("location"), "/admin/services")

        # Verify product created
        db = SessionLocal()
        svc = db.query(Service).filter(Service.name == "Test Auto Service B").first()
        self.assertIsNotNone(svc)
        self.assertEqual(svc.sell_price, 29.99)
        db.close()


if __name__ == "__main__":
    unittest.main()
