"""Resolve unit price for a user — includes personal product discounts."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_UP, Decimal

from sqlalchemy.orm import Session

from database.models import Service, User, UserProductDiscount


def api_computed_sell_price(
    cost_price: float,
    commission_pct: float,
    fixed_usdt: float = 0.0,
) -> float:
    """Sell price from API cost + % markup + optional fixed USDT, always 2 decimals.

    Extra fraction rounds UP so clients never see/pay a truncated under-price
    (e.g. 0.5625 → 0.57). Used for Import Products + provider sync only.
    """
    cost = Decimal(str(cost_price or 0))
    pct = Decimal(str(commission_pct or 0))
    fixed = Decimal(str(fixed_usdt or 0))
    raw = cost + (cost * pct / Decimal("100")) + fixed
    if raw <= 0:
        return 0.0
    return float(raw.quantize(Decimal("0.01"), rounding=ROUND_UP))


def derive_api_markup_from_sell(
    cost_price: float,
    sell_price: float,
    prefer_commission_pct: float = 0.0,
) -> tuple[float, float, float]:
    """Turn a chosen sell price into lasting profit settings for sync.

    Keeps prefer_commission_pct when possible and puts the rest in fixed USDT.
    If sell is below cost+% , stores an equivalent % instead (fixed=0).

    Returns (commission_pct, markup_fixed_usdt, computed_sell_price).
    """
    cost = Decimal(str(cost_price or 0))
    sell = max(Decimal(str(sell_price or 0)), Decimal("0"))
    pct = max(Decimal(str(prefer_commission_pct or 0)), Decimal("0"))

    if cost <= 0:
        fixed = float(sell)
        return 0.0, fixed, api_computed_sell_price(0.0, 0.0, fixed)

    after_pct = cost + (cost * pct / Decimal("100"))
    fixed = sell - after_pct
    if fixed < 0:
        # Sell below the % markup → encode profit purely as %
        new_pct = max((sell / cost - Decimal("1")) * Decimal("100"), Decimal("0"))
        new_pct_f = float(new_pct)
        return new_pct_f, 0.0, api_computed_sell_price(float(cost), new_pct_f, 0.0)

    # Quantize fixed to cents so ROUND_UP on the final sell does not drift.
    fixed = max(fixed, Decimal("0")).quantize(Decimal("0.01"), rounding=ROUND_UP)
    # If cost+%+fixed rounds above target sell, nudge fixed down one cent when possible.
    candidate = api_computed_sell_price(float(cost), float(pct), float(fixed))
    if candidate > float(sell) + 1e-9 and fixed >= Decimal("0.01"):
        fixed = fixed - Decimal("0.01")
        candidate = api_computed_sell_price(float(cost), float(pct), float(fixed))
    return float(pct), float(fixed), candidate


@dataclass
class PriceQuote:
    unit_price: float
    list_price: float
    discount: UserProductDiscount | None = None
    discount_pct: float = 0.0
    discount_source: str = "none"  # "none", "loyalty", "personal", "sale"
    loyalty_tier: dict | None = None
    custom_label: str | None = None

    @property
    def has_discount(self) -> bool:
        return self.unit_price < self.list_price - 1e-9

    @property
    def discount_label(self) -> str | None:
        if self.custom_label:
            return self.custom_label
        if self.discount_source == "loyalty" and self.loyalty_tier:
            return f"{self.loyalty_tier['short_name']} Member • {self.discount_pct:g}% off"
        if not self.discount:
            return None
        d = self.discount
        if d.discount_type == "percent":
            return f"{float(d.value):g}% off"
        if d.discount_type == "fixed":
            return f"${float(d.value):.2f} off"
        return f"Special ${self.unit_price:.2f}"


def get_active_user_discount(
    db: Session,
    *,
    user_id: int | None,
    service_id: int,
) -> UserProductDiscount | None:
    if not user_id:
        return None
    return (
        db.query(UserProductDiscount)
        .filter(
            UserProductDiscount.user_id == int(user_id),
            UserProductDiscount.service_id == int(service_id),
            UserProductDiscount.is_active.is_(True),
        )
        .order_by(UserProductDiscount.id.desc())
        .first()
    )


def apply_discount_to_price(list_price: float, discount: UserProductDiscount | None) -> float:
    """Apply a personal discount on top of the current list/sale price."""
    base = max(float(list_price or 0), 0.0)
    if not discount:
        return round(base, 6)
    dtype = (discount.discount_type or "percent").lower()
    value = float(discount.value or 0)
    if dtype == "price":
        return round(max(value, 0.0), 6)
    if dtype == "fixed":
        return round(max(base - value, 0.0), 6)
    # percent
    pct = max(min(value, 100.0), 0.0)
    return round(max(base * (1.0 - pct / 100.0), 0.0), 6)


def resolve_unit_price(
    db: Session,
    service: Service,
    user: User | None = None,
) -> PriceQuote:
    """Current sell_price (already includes active ProductSale) + optional personal/loyalty discount.
    
    Precedence rule:
    Final customer discount = whichever is higher: Personal Admin Discount OR Loyalty Tier Discount.
    No automatic stacking (prevents margin compounding).
    """
    from utils.loyalty import compute_customer_loyalty

    list_price = round(float(getattr(service, "sell_price", 0) or 0), 6)
    if user is None:
        return PriceQuote(unit_price=list_price, list_price=list_price)

    # 1. Personal discount
    personal_disc = get_active_user_discount(db, user_id=user.id, service_id=service.id)
    price_after_personal = apply_discount_to_price(list_price, personal_disc)
    personal_pct = 0.0
    if personal_disc:
        if personal_disc.discount_type == "percent":
            personal_pct = float(personal_disc.value or 0)
        elif list_price > 0:
            personal_pct = round(((list_price - price_after_personal) / list_price) * 100.0, 2)

    # 2. Loyalty tier discount
    loyalty = compute_customer_loyalty(db, user)
    loyalty_pct = float(loyalty.get("discount_pct", 0.0) or 0.0)
    price_after_loyalty = round(max(0.0, list_price * (1.0 - loyalty_pct / 100.0)), 6)

    # 3. Precedence: Whichever discount is higher (more favorable to customer)
    if personal_pct > loyalty_pct and personal_disc is not None:
        unit = price_after_personal
        source = "personal"
        eff_pct = personal_pct
        label = f"Personal {personal_pct:g}% off"
    elif loyalty_pct > 0.0:
        unit = price_after_loyalty
        source = "loyalty"
        eff_pct = loyalty_pct
        label = f"{loyalty['short_name']} Member • {loyalty_pct:g}% off"
    else:
        unit = list_price
        source = "none"
        eff_pct = 0.0
        label = None

    if unit >= list_price - 1e-9:
        return PriceQuote(unit_price=list_price, list_price=list_price, loyalty_tier=loyalty)

    return PriceQuote(
        unit_price=unit,
        list_price=list_price,
        discount=personal_disc if source == "personal" else None,
        discount_pct=eff_pct,
        discount_source=source,
        loyalty_tier=loyalty,
        custom_label=label,
    )


def service_unit_prices(
    db: Session,
    services: list[Service],
    user: User | None = None,
) -> dict[int, float]:
    """Map service_id → unit price for this user (personal discount when set)."""
    return {
        int(service.id): resolve_unit_price(db, service, user).unit_price
        for service in services
    }
