"""Period activity stats for Admin Dashboard / Users / Orders / Transactions.

Professional count cards only — presets 7 / 30 / 60 / 90 days, plus custom
calendar range. No daily date bars.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func
from sqlalchemy.orm import Session

from database.models import Order, Transaction, User

PERIOD_OPTIONS = (
    ("7", "7 days"),
    ("30", "30 days"),
    ("60", "60 days"),
    ("90", "90 days"),
)


def parse_period(raw: str | None) -> str:
    value = (raw or "30").strip().lower()
    if value in {"7", "7d", "week"}:
        return "7"
    if value in {"60", "60d"}:
        return "60"
    if value in {"90", "90d"}:
        return "90"
    if value in {"custom", "range"}:
        return "custom"
    return "30"


def _parse_date(raw: str | None) -> datetime | None:
    value = (raw or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value[:10] if fmt == "%Y-%m-%d" else value, fmt if fmt != "%Y-%m-%d" else "%Y-%m-%d")
        except ValueError:
            continue
    try:
        return datetime.strptime(value[:10], "%Y-%m-%d")
    except ValueError:
        return None


def resolve_range(
    period: str | None,
    date_from: str | None = None,
    date_to: str | None = None,
    *,
    now: datetime | None = None,
) -> tuple[str, datetime, datetime, str]:
    """Return (period_key, start, end_exclusive, label)."""
    now = now or datetime.utcnow()
    today_end = now.replace(hour=23, minute=59, second=59, microsecond=999999)
    period_key = parse_period(period)

    start_custom = _parse_date(date_from)
    end_custom = _parse_date(date_to)
    if period_key == "custom" or (start_custom and end_custom):
        if not start_custom or not end_custom:
            # Incomplete custom → fall back to 30 days
            period_key = "30"
        else:
            if end_custom.date() > now.date():
                end_custom = now.replace(hour=0, minute=0, second=0, microsecond=0)
            if start_custom > end_custom:
                start_custom, end_custom = end_custom, start_custom
            start = start_custom.replace(hour=0, minute=0, second=0, microsecond=0)
            end = end_custom.replace(hour=23, minute=59, second=59, microsecond=999999)
            label = f"{start.strftime('%d %b %Y')} – {end.strftime('%d %b %Y')}"
            return "custom", start, end, label

    days = {"7": 7, "30": 30, "60": 60, "90": 90}.get(period_key, 30)
    start = (now - timedelta(days=days - 1)).replace(hour=0, minute=0, second=0, microsecond=0)
    label = dict(PERIOD_OPTIONS).get(period_key, "30 days")
    return period_key, start, today_end, label


def _count_between(db: Session, model, date_col, start: datetime, end: datetime) -> int:
    return (
        db.query(model)
        .filter(date_col >= start, date_col <= end)
        .count()
    )


def users_period_stats(
    db: Session,
    period: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    period_key, start, end, label = resolve_range(period, date_from, date_to)
    in_period = _count_between(db, User, User.joined_at, start, end)
    return {
        "kind": "users",
        "title": "Users",
        "metric_label": "Users joined",
        "period": period_key,
        "period_label": label,
        "period_options": PERIOD_OPTIONS,
        "date_from": start.strftime("%Y-%m-%d"),
        "date_to": end.strftime("%Y-%m-%d"),
        "in_period": in_period,
        "total_all_time": db.query(User).count(),
        "base_path": "/admin/users",
        "cards": [
            {"label": "Users joined", "value": in_period},
            {"label": "All time", "value": db.query(User).count()},
        ],
    }


def orders_period_stats(
    db: Session,
    period: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    period_key, start, end, label = resolve_range(period, date_from, date_to)
    in_period = _count_between(db, Order, Order.created_at, start, end)
    completed = (
        db.query(Order)
        .filter(Order.created_at >= start, Order.created_at <= end, Order.status == "completed")
        .count()
    )
    expired = (
        db.query(Order)
        .filter(Order.created_at >= start, Order.created_at <= end, Order.status == "expired")
        .count()
    )
    pending = (
        db.query(Order)
        .filter(
            Order.created_at >= start,
            Order.created_at <= end,
            Order.status.in_(["pending", "manual_pending", "processing"]),
        )
        .count()
    )
    return {
        "kind": "orders",
        "title": "Orders",
        "metric_label": "Orders",
        "period": period_key,
        "period_label": label,
        "period_options": PERIOD_OPTIONS,
        "date_from": start.strftime("%Y-%m-%d"),
        "date_to": end.strftime("%Y-%m-%d"),
        "in_period": in_period,
        "completed": completed,
        "expired": expired,
        "total_all_time": db.query(Order).count(),
        "base_path": "/admin/orders",
        "cards": [
            {"label": "Orders", "value": in_period},
            {"label": "Completed", "value": completed},
            {"label": "Pending", "value": pending},
            {"label": "Expired", "value": expired},
            {"label": "All time", "value": db.query(Order).count()},
        ],
    }


def transactions_period_stats(
    db: Session,
    period: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    period_key, start, end, label = resolve_range(period, date_from, date_to)
    in_period = _count_between(db, Transaction, Transaction.created_at, start, end)
    deposits = (
        db.query(Transaction)
        .filter(
            Transaction.created_at >= start,
            Transaction.created_at <= end,
            Transaction.tx_type == "deposit",
        )
        .count()
    )
    confirmed = (
        db.query(Transaction)
        .filter(
            Transaction.created_at >= start,
            Transaction.created_at <= end,
            Transaction.status == "confirmed",
        )
        .count()
    )
    pending = (
        db.query(Transaction)
        .filter(
            Transaction.created_at >= start,
            Transaction.created_at <= end,
            Transaction.status == "pending",
        )
        .count()
    )
    return {
        "kind": "transactions",
        "title": "Transactions",
        "metric_label": "Transactions",
        "period": period_key,
        "period_label": label,
        "period_options": PERIOD_OPTIONS,
        "date_from": start.strftime("%Y-%m-%d"),
        "date_to": end.strftime("%Y-%m-%d"),
        "in_period": in_period,
        "deposits": deposits,
        "confirmed": confirmed,
        "total_all_time": db.query(Transaction).count(),
        "base_path": "/admin/transactions",
        "cards": [
            {"label": "Transactions", "value": in_period},
            {"label": "Deposits", "value": deposits},
            {"label": "Confirmed", "value": confirmed},
            {"label": "Pending", "value": pending},
            {"label": "All time", "value": db.query(Transaction).count()},
        ],
    }


def dashboard_period_stats(
    db: Session,
    period: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    from database.models import Service, Stock

    period_key, start, end, label = resolve_range(period, date_from, date_to)
    users = _count_between(db, User, User.joined_at, start, end)
    orders = _count_between(db, Order, Order.created_at, start, end)
    completed = (
        db.query(Order)
        .filter(Order.created_at >= start, Order.created_at <= end, Order.status == "completed")
        .count()
    )
    revenue = (
        db.query(func.coalesce(func.sum(Order.amount_usdt), 0.0))
        .filter(Order.created_at >= start, Order.created_at <= end, Order.status == "completed")
        .scalar()
    )
    # Profit = what customer paid − product cost × qty (uses each product's cost_price).
    profit = (
        db.query(
            func.coalesce(
                func.sum(Order.amount_usdt - func.coalesce(Service.cost_price, 0.0) * Order.quantity),
                0.0,
            )
        )
        .select_from(Order)
        .join(Service, Service.id == Order.service_id)
        .filter(Order.created_at >= start, Order.created_at <= end, Order.status == "completed")
        .scalar()
    )
    deposits = (
        db.query(Transaction)
        .filter(
            Transaction.created_at >= start,
            Transaction.created_at <= end,
            Transaction.tx_type == "deposit",
            Transaction.status == "confirmed",
        )
        .count()
    )
    pending_orders = db.query(Order).filter(Order.status.in_(["pending", "manual_pending", "processing"])).count()
    return {
        "kind": "dashboard",
        "title": "Dashboard",
        "period": period_key,
        "period_label": label,
        "period_options": PERIOD_OPTIONS,
        "date_from": start.strftime("%Y-%m-%d"),
        "date_to": end.strftime("%Y-%m-%d"),
        "base_path": "/admin/dashboard",
        "cards": [
            {"label": "Users joined", "value": users},
            {"label": "Orders", "value": orders},
            {"label": "Completed", "value": completed},
            {"label": "Revenue (USDT)", "value": f"{float(revenue or 0):.2f}"},
            {"label": "Profit (USDT)", "value": f"{float(profit or 0):.2f}"},
            {"label": "Confirmed deposits", "value": deposits},
            {"label": "Pending orders", "value": pending_orders},
            {"label": "Active products", "value": db.query(Service).filter(Service.is_active.is_(True), Service.is_deleted.is_(False)).count()},
            {
                "label": "Stock available",
                "value": int(db.query(func.coalesce(func.sum(Stock.quantity - Stock.reserved_qty), 0)).scalar() or 0),
            },
        ],
    }


def sold_accounts_period_stats(
    db: Session,
    period: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    *,
    service_id: int | None = None,
) -> dict:
    """Completed-order sold units (accounts) by period — same calendar as Orders."""
    import re

    from database.models import Service

    period_key, start, end, label = resolve_range(period, date_from, date_to)

    filters = [
        Order.status == "completed",
        Order.created_at >= start,
        Order.created_at <= end,
    ]
    all_time_filters = [Order.status == "completed"]
    if service_id:
        filters.append(Order.service_id == service_id)
        all_time_filters.append(Order.service_id == service_id)

    sold_units = int(
        db.query(func.coalesce(func.sum(Order.quantity), 0)).filter(*filters).scalar() or 0
    )
    completed_orders = db.query(Order).filter(*filters).count()
    revenue = float(
        db.query(func.coalesce(func.sum(Order.amount_usdt), 0.0)).filter(*filters).scalar() or 0
    )
    products_with_sales = int(
        db.query(func.count(func.distinct(Order.service_id))).filter(*filters).scalar() or 0
    )
    all_time_sold = int(
        db.query(func.coalesce(func.sum(Order.quantity), 0)).filter(*all_time_filters).scalar() or 0
    )

    title = "Sold Accounts"
    if service_id:
        svc = db.get(Service, service_id)
        if svc:
            plain = re.sub(r"</?tg-emoji[^>]*>", "", svc.name or "")
            plain = " ".join(plain.split()).strip() or f"#{service_id}"
            title = f"Sold · {plain}"

    return {
        "kind": "sold_accounts",
        "title": title,
        "metric_label": "Accounts sold",
        "period": period_key,
        "period_label": label,
        "period_options": PERIOD_OPTIONS,
        "date_from": start.strftime("%Y-%m-%d"),
        "date_to": end.strftime("%Y-%m-%d"),
        "base_path": "/admin/sold-accounts",
        "cards": [
            {"label": "Accounts sold", "value": sold_units},
            {"label": "Completed orders", "value": completed_orders},
            {"label": "Products with sales", "value": products_with_sales},
            {"label": "Revenue (USDT)", "value": f"{revenue:.2f}"},
            {"label": "All-time sold", "value": all_time_sold},
        ],
        "_range": (start, end),
    }
