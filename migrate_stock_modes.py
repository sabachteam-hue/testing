"""Add stock mode columns for Account / Quantity / Unlimited inventory."""
from sqlalchemy import inspect, text
from database.models import engine


def run():
    inspector = inspect(engine)
    if "stocks" not in inspector.get_table_names():
        return
    cols = {c["name"] for c in inspector.get_columns("stocks")}
    with engine.begin() as conn:
        if "stock_type" not in cols:
            conn.execute(text("ALTER TABLE stocks ADD COLUMN stock_type VARCHAR(20) DEFAULT 'account'"))
        if "is_unlimited" not in cols:
            # BOOLEAN works on PostgreSQL and SQLite.
            conn.execute(text("ALTER TABLE stocks ADD COLUMN is_unlimited BOOLEAN DEFAULT FALSE"))
        conn.execute(text("UPDATE stocks SET stock_type='account' WHERE stock_type IS NULL OR stock_type=''"))
        conn.execute(text("UPDATE stocks SET is_unlimited=FALSE WHERE is_unlimited IS NULL"))


if __name__ == "__main__":
    run()
