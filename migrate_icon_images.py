"""
YEH SCRIPT SIRF EK BAAR CHALANI HAI (existing/live database par).

Real logo/icon IMAGE feature (Products/Categories/Payment Methods sab mein
"asy icons" jaisa reseller-shop-style catalog) ke liye 2 naye columns add
karti hai:

  1. services.image_path        -> product ka real logo (categories.image_path
                                     pehle se maujood tha, ab services mein bhi).
  2. payment_methods.image_path -> payment method ka real logo (Binance,
                                     USDT, JazzCash wagera).

Agar column already maujood hai to chup chaap skip kar deti hai — dobara
chalane se koi nuksan nahi hoga.

CHALANE KA TAREEQA:
    Railway / server / apne local machine par, project folder ke andar:
        python migrate_icon_images.py

    (Wahi environment/DATABASE_URL use hoga jo bot khud use karta hai, kyunke
    yeh database/models.py wala hi engine import karti hai.)
"""

from sqlalchemy import text

from database.models import engine


def column_exists(conn, table: str, column: str) -> bool:
    dialect = engine.dialect.name
    if dialect == "sqlite":
        rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
        return any(row[1] == column for row in rows)
    # postgres
    result = conn.execute(
        text(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_name = :table AND column_name = :column"
        ),
        {"table": table, "column": column},
    )
    return result.first() is not None


def run() -> None:
    with engine.begin() as conn:
        if not column_exists(conn, "services", "image_path"):
            print("Adding services.image_path column...")
            conn.execute(text("ALTER TABLE services ADD COLUMN image_path VARCHAR(500)"))
        else:
            print("services.image_path already exists, skipping.")

        if not column_exists(conn, "payment_methods", "image_path"):
            print("Adding payment_methods.image_path column...")
            conn.execute(text("ALTER TABLE payment_methods ADD COLUMN image_path VARCHAR(500)"))
        else:
            print("payment_methods.image_path already exists, skipping.")

    print("Done! Real logo/icon image support is ready for products, categories and payment methods.")


if __name__ == "__main__":
    run()
