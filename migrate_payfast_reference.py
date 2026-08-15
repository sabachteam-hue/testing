"""
YEH SCRIPT SIRF EK BAAR CHALANI HAI (existing/live database par).

Secure PayFast Order ID / reference verification feature ke liye naya column
add karti hai:

  1. transactions.payfast_reference -> customer-facing PayFast checkout
     reference (e.g. "SMFSHOP-A7K29Q"). Cryptographically random, DB-unique,
     aur guessable transaction id se decoupled (purana "SMFSHOP<id>" format
     sequential/guessable tha).

Agar column already maujood hai to chup chaap skip kar deti hai — dobara
chalane se koi nuksan nahi hoga.

CHALANE KA TAREEQA:
    Railway / server / apne local machine par, project folder ke andar:
        python migrate_payfast_reference.py

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


def index_exists(conn, index_name: str) -> bool:
    dialect = engine.dialect.name
    if dialect == "sqlite":
        rows = conn.execute(text("PRAGMA index_list(transactions)")).fetchall()
        return any(row[1] == index_name for row in rows)
    result = conn.execute(
        text("SELECT 1 FROM pg_indexes WHERE indexname = :name"),
        {"name": index_name},
    )
    return result.first() is not None


def run() -> None:
    with engine.begin() as conn:
        if not column_exists(conn, "transactions", "payfast_reference"):
            print("Adding transactions.payfast_reference column...")
            conn.execute(text("ALTER TABLE transactions ADD COLUMN payfast_reference VARCHAR(40)"))
        else:
            print("transactions.payfast_reference already exists, skipping.")

        index_name = "ix_transactions_payfast_reference"
        if not index_exists(conn, index_name):
            print("Adding unique index on transactions.payfast_reference...")
            conn.execute(
                text(
                    f"CREATE UNIQUE INDEX {index_name} "
                    "ON transactions (payfast_reference) "
                    "WHERE payfast_reference IS NOT NULL"
                )
                if engine.dialect.name != "sqlite"
                else text(
                    f"CREATE UNIQUE INDEX {index_name} "
                    "ON transactions (payfast_reference)"
                )
            )
        else:
            print("Unique index on transactions.payfast_reference already exists, skipping.")

    print("Done! Secure PayFast reference verification is ready.")


if __name__ == "__main__":
    run()
