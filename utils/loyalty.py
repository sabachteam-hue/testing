"""Customer loyalty tier calculation and dynamic discount engine (Phase 6).

Authoritative rules:
1. Loyalty tier is calculated from successful completed non-fully-refunded orders.
2. Orders with status 'cancelled', 'canceled', 'failed', 'pending', or orders that are
   refunded (status == 'refunded' or refunded_at is not None) are excluded from the tier count.
3. Default Tiers:
   - Level 1 — Starter: 0–2 completed orders, 0% discount
   - Level 2 — Bronze: 3–5 completed orders, 2% discount
   - Level 3 — Silver: 6–10 completed orders, 4% discount
   - Level 4 — Gold: 11–20 completed orders, 6% discount
   - Level 5 — Platinum: 21+ completed orders, 8% discount
4. Admin Override:
   If a customer has user.loyalty_tier_override set (e.g. 'gold'), that tier takes precedence.
5. Discount Precedence:
   Final discount = max(personal_discount_pct, loyalty_discount_pct). No compounding/stacking.
"""

from __future__ import annotations

import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from database.models import Order, User, UserProductDiscount

logger = logging.getLogger(__name__)

# Centralized, configurable loyalty tiers
LOYALTY_TIERS = [
    {
        "level": 1,
        "id": "starter",
        "name": "Level 1 — Starter",
        "short_name": "Starter",
        "min_orders": 0,
        "max_orders": 2,
        "discount_pct": 0.0,
        "badge_color": "#94a3b8",
        "icon": "🌱",
        "description": "Standard member pricing.",
    },
    {
        "level": 2,
        "id": "bronze",
        "name": "Level 2 — Bronze",
        "short_name": "Bronze",
        "min_orders": 3,
        "max_orders": 5,
        "discount_pct": 2.0,
        "badge_color": "#cd7f32",
        "icon": "🥉",
        "description": "2% dynamic discount on all storefront products.",
    },
    {
        "level": 3,
        "id": "silver",
        "name": "Level 3 — Silver",
        "short_name": "Silver",
        "min_orders": 6,
        "max_orders": 10,
        "discount_pct": 4.0,
        "badge_color": "#e2e8f0",
        "icon": "🥈",
        "description": "4% dynamic discount on all storefront products.",
    },
    {
        "level": 4,
        "id": "gold",
        "name": "Level 4 — Gold",
        "short_name": "Gold",
        "min_orders": 11,
        "max_orders": 20,
        "discount_pct": 6.0,
        "badge_color": "#fbbf24",
        "icon": "🥇",
        "description": "6% dynamic discount on all storefront products.",
    },
    {
        "level": 5,
        "id": "platinum",
        "name": "Level 5 — Platinum",
        "short_name": "Platinum",
        "min_orders": 21,
        "max_orders": None,
        "discount_pct": 8.0,
        "badge_color": "#c084fc",
        "icon": "💎",
        "description": "8% VIP dynamic discount on all storefront products.",
    },
]

TIER_BY_ID = {t["id"]: t for t in LOYALTY_TIERS}
TIER_BY_LEVEL = {t["level"]: t for t in LOYALTY_TIERS}


def get_tier_by_order_count(completed_orders_count: int) -> dict:
    """Determine loyalty tier from completed order count."""
    count = max(0, int(completed_orders_count or 0))
    selected = LOYALTY_TIERS[0]
    for tier in LOYALTY_TIERS:
        if count >= tier["min_orders"]:
            selected = tier
    return selected


def calculate_customer_order_metrics(db: Session, user_id: int) -> dict:
    """Calculate completed order count and net lifetime spend for a user.

    Strict rules:
    - Counts only completed / delivered orders.
    - Excludes cancelled, failed, or fully refunded orders.
    - Net spend subtracts partial refund amounts recorded on the order.
    """
    if not user_id:
        return {"completed_orders": 0, "lifetime_spend": 0.0}

    # Successful non-fully-refunded orders query
    query = (
        db.query(Order)
        .filter(
            Order.user_id == user_id,
            Order.status.in_(["completed", "delivered"]),
            Order.status != "refunded",
            Order.refunded_at.is_(None),
        )
    )

    orders = query.all()
    completed_count = len(orders)
    total_spend = Decimal("0.00")
    for o in orders:
        amt = Decimal(str(getattr(o, "amount_usdt", 0) or 0))
        ref = Decimal(str(getattr(o, "refund_amount", 0) or 0))
        net = max(Decimal("0.00"), amt - ref)
        total_spend += net

    return {
        "completed_orders": completed_count,
        "lifetime_spend": float(total_spend.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)),
    }


def compute_customer_loyalty(db: Session, user: User) -> dict:
    """Compute full loyalty tier payload for a customer including next-tier progress."""
    if not user:
        tier = LOYALTY_TIERS[0]
        return {
            "level": tier["level"],
            "id": tier["id"],
            "name": tier["name"],
            "short_name": tier["short_name"],
            "discount_pct": tier["discount_pct"],
            "badge_color": tier["badge_color"],
            "icon": tier["icon"],
            "description": tier["description"],
            "completed_orders": 0,
            "lifetime_spend": 0.0,
            "is_override": False,
            "is_highest": False,
            "next_tier_name": LOYALTY_TIERS[1]["short_name"],
            "next_tier_level": LOYALTY_TIERS[1]["level"],
            "next_tier_orders": LOYALTY_TIERS[1]["min_orders"],
            "orders_to_next_tier": LOYALTY_TIERS[1]["min_orders"],
            "progress_percent": 0.0,
            "summary_text": f"0 / {LOYALTY_TIERS[1]['min_orders']} completed orders to reach {LOYALTY_TIERS[1]['short_name']}",
        }

    metrics = calculate_customer_order_metrics(db, user.id)
    completed_orders = metrics["completed_orders"]
    lifetime_spend = metrics["lifetime_spend"]

    # Check admin override
    override_id = getattr(user, "loyalty_tier_override", None)
    is_override = False
    if override_id and str(override_id).lower() in TIER_BY_ID:
        tier = TIER_BY_ID[str(override_id).lower()]
        is_override = True
    else:
        tier = get_tier_by_order_count(completed_orders)

    curr_level = tier["level"]
    is_highest = (curr_level == len(LOYALTY_TIERS))

    next_tier = None
    orders_to_next = 0
    progress_pct = 100.0
    summary_text = f"Highest loyalty level reached ({tier['discount_pct']:g}% VIP discount)"

    if not is_highest:
        next_tier = LOYALTY_TIERS[curr_level]  # 0-indexed: tier with level = curr_level + 1
        orders_to_next = max(0, next_tier["min_orders"] - completed_orders)
        
        # Calculate progress between current tier threshold and next tier threshold
        tier_floor = tier["min_orders"]
        tier_ceil = next_tier["min_orders"]
        span = max(1, tier_ceil - tier_floor)
        progress_in_span = max(0, min(span, completed_orders - tier_floor))
        progress_pct = round((progress_in_span / span) * 100.0, 1)
        summary_text = f"{completed_orders} / {tier_ceil} completed orders to reach {next_tier['short_name']}"

    return {
        "level": tier["level"],
        "id": tier["id"],
        "name": tier["name"],
        "short_name": tier["short_name"],
        "discount_pct": float(tier["discount_pct"]),
        "badge_color": tier["badge_color"],
        "icon": tier["icon"],
        "description": tier["description"],
        "completed_orders": completed_orders,
        "lifetime_spend": lifetime_spend,
        "is_override": is_override,
        "is_highest": is_highest,
        "next_tier_name": next_tier["short_name"] if next_tier else None,
        "next_tier_level": next_tier["level"] if next_tier else None,
        "next_tier_orders": next_tier["min_orders"] if next_tier else None,
        "orders_to_next_tier": orders_to_next,
        "progress_percent": progress_pct,
        "summary_text": summary_text,
    }


def get_effective_customer_discount(
    db: Session,
    user: User | None,
    service_id: int | None = None,
) -> dict:
    """Determine effective discount percentage and source based on precedence rules.

    Precedence:
    Whichever is higher:
    - Personal Admin Discount (UserProductDiscount)
    - Loyalty Tier Discount

    Returns:
    {
        "discount_pct": float,
        "discount_source": "loyalty" | "personal" | "none",
        "loyalty_tier": dict | None,
        "personal_discount": UserProductDiscount | None,
        "label": str | None,
    }
    """
    if not user:
        return {
            "discount_pct": 0.0,
            "discount_source": "none",
            "loyalty_tier": None,
            "personal_discount": None,
            "label": None,
        }

    # 1. Loyalty Tier Discount
    loyalty = compute_customer_loyalty(db, user)
    loyalty_pct = float(loyalty.get("discount_pct", 0.0) or 0.0)

    # 2. Personal Discount (if service_id provided)
    personal_pct = 0.0
    personal_disc = None
    if service_id:
        personal_disc = (
            db.query(UserProductDiscount)
            .filter(
                UserProductDiscount.user_id == user.id,
                UserProductDiscount.service_id == service_id,
                UserProductDiscount.is_active.is_(True),
            )
            .order_by(UserProductDiscount.id.desc())
            .first()
        )
        if personal_disc and personal_disc.discount_type == "percent":
            personal_pct = float(personal_disc.value or 0.0)

    # 3. Apply higher discount precedence (Requirement 6)
    if personal_pct > loyalty_pct:
        return {
            "discount_pct": personal_pct,
            "discount_source": "personal",
            "loyalty_tier": loyalty,
            "personal_discount": personal_disc,
            "label": f"Personal {personal_pct:g}% off",
        }
    elif loyalty_pct > 0.0:
        return {
            "discount_pct": loyalty_pct,
            "discount_source": "loyalty",
            "loyalty_tier": loyalty,
            "personal_discount": personal_disc,
            "label": f"{loyalty['short_name']} Member • {loyalty_pct:g}% off",
        }
    else:
        return {
            "discount_pct": 0.0,
            "discount_source": "none",
            "loyalty_tier": loyalty,
            "personal_discount": personal_disc,
            "label": None,
        }
