"""
YEH SCRIPT SIRF EK BAAR CHALANI HAI (existing/live database par).

Ye 3 kaam karti hai taake Telegram Premium "custom emoji" har jaga (Category,
Product/Service, Payment Method) use ho sakein:

  1. services table mein naya column "emoji" add karti hai (agar pehle se nahi hai).
  2. categories.emoji aur payment_methods.icon columns ko bada (widen) karti hai,
     taake lambi custom-emoji ID (jaise "5368324170671202286|🔥") us mein fit ho jaye.
  3. Agar kuch bhi already up-to-date hai to chup chaap skip kar deti hai — dobara
     chalane se koi nuksan nahi hoga.

CHALANE KA TAREEQA:
    Railway / server / apne local machine par, project folder ke andar:
        python migrate_custom_emoji.py

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
    dialect = engine.dialect.name
    with engine.begin() as conn:
        # 1) services.emoji column
        if not column_exists(conn, "services", "emoji"):
            print("Adding services.emoji column...")
            conn.execute(text("ALTER TABLE services ADD COLUMN emoji VARCHAR(60)"))
        else:
            print("services.emoji already exists, skipping.")

        # 2) widen categories.emoji + payment_methods.icon (only matters on
        # Postgres — sqlite doesn't actually enforce VARCHAR length, so this
        # step is a no-op there but harmless to run).
        if dialect.startswith("postgresql"):
            print("Widening categories.emoji to VARCHAR(60)...")
            conn.execute(text("ALTER TABLE categories ALTER COLUMN emoji TYPE VARCHAR(60)"))
            print("Widening payment_methods.icon to VARCHAR(60)...")
            conn.execute(text("ALTER TABLE payment_methods ALTER COLUMN icon TYPE VARCHAR(60)"))
        else:
            print("SQLite database detected — column width isn't enforced, no ALTER needed.")

    print("Done! Custom emoji support is ready.")


if __name__ == "__main__":
    run()
