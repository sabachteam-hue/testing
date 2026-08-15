from datetime import datetime

from sqlalchemy.orm import Session

from database.models import Service, Stock


class InsufficientStockError(ValueError):
    pass


def get_or_create_stock(db: Session, service_id: int) -> Stock:
    stock = db.query(Stock).filter(Stock.service_id == service_id).first()
    if stock:
        return stock
    stock = Stock(service_id=service_id, quantity=0, reserved_qty=0)
    db.add(stock)
    db.flush()
    return stock


def _append_login_details(existing: str | None, addition: str | None) -> str | None:
    """Naye login/account lines ko purani list ke *neeche* jorta hai (replace nahi
    karta), taake baar baar stock add karne par pehle wale accounts na mit jayen."""
    addition = (addition or "").strip()
    if not addition:
        return existing
    existing_lines = [line for line in (existing or "").splitlines() if line.strip()]
    new_lines = [line for line in addition.splitlines() if line.strip()]
    combined = existing_lines + new_lines
    return "\n".join(combined) if combined else None


def add_stock(db: Session, service_id: int, quantity: int, notes: str | None = None, login_details: str | None = None) -> Stock:
    if quantity <= 0:
        raise ValueError("Quantity must be greater than zero")
    stock = get_or_create_stock(db, service_id)
    stock.quantity += quantity
    stock.notes = notes
    stock.login_details = _append_login_details(stock.login_details, login_details)
    stock.last_updated = datetime.utcnow()
    service = db.get(Service, service_id)
    if service and stock.quantity > 0:
        service.is_active = True
    db.commit()
    db.refresh(stock)
    return stock


def set_stock(
    db: Session,
    service_id: int,
    quantity: int,
    reserved_qty: int = 0,
    notes: str | None = None,
    login_details: str | None = None,
    *,
    low_stock_threshold: int | None = None,
    replace_login_details: bool = False,
) -> Stock:
    if quantity < 0 or reserved_qty < 0:
        raise ValueError("Stock values cannot be negative")
    if reserved_qty > quantity:
        raise ValueError("Reserved quantity cannot exceed total quantity")
    stock = get_or_create_stock(db, service_id)
    stock.quantity = quantity
    stock.reserved_qty = reserved_qty
    if notes is not None:
        stock.notes = notes.strip() or None
    if low_stock_threshold is not None:
        stock.low_stock_threshold = max(int(low_stock_threshold), 0)
    # "Set Stock" override: blank keeps current list unless replace_login_details.
    if replace_login_details:
        stock.login_details = (login_details or "").strip() or None
    elif login_details is not None and login_details.strip():
        stock.login_details = login_details.strip()
    stock.last_updated = datetime.utcnow()
    service = db.get(Service, service_id)
    if service:
        service.is_active = quantity > 0
    db.commit()
    db.refresh(stock)
    return stock


def consume_stock_account(db: Session, service_id: int, quantity: int) -> list[str] | None:
    """Stock ke login_details block se upar ki `quantity` lines nikal ke deta hai
    aur unhe stock se hata deta hai (taake wahi account dobara kisi aur order ko
    deliver na ho). Agar itni lines available na hon to None return karta hai —
    caller iss case me manual fulfillment par fall back kare."""
    from utils.stock_display import align_quantity_to_login_lines

    stock = get_or_create_stock(db, service_id)
    lines = [line for line in (stock.login_details or "").splitlines() if line.strip()]
    if len(lines) < quantity:
        # Heal stale quantity so catalog stops showing In Stock with 0 accounts.
        align_quantity_to_login_lines(stock)
        service = db.get(Service, service_id)
        if service and int(stock.available_qty or 0) <= 0 and not lines:
            service.is_active = False
        db.flush()
        return None
    delivered, remaining = lines[:quantity], lines[quantity:]
    stock.login_details = "\n".join(remaining) if remaining else None
    stock.last_updated = datetime.utcnow()
    db.flush()
    return delivered


def reserve_stock(db: Session, service_id: int, quantity: int) -> Stock:
    from utils.stock_display import effective_available_qty

    stock = get_or_create_stock(db, service_id)
    service = db.get(Service, service_id)
    available = effective_available_qty(service) if service is not None else int(stock.available_qty or 0)
    if available < quantity:
        raise InsufficientStockError(f"Only {available} units are available")
    stock.reserved_qty += quantity
    stock.last_updated = datetime.utcnow()
    db.flush()
    return stock


def release_stock(db: Session, service_id: int, quantity: int) -> Stock:
    stock = get_or_create_stock(db, service_id)
    stock.reserved_qty = max(stock.reserved_qty - quantity, 0)
    stock.last_updated = datetime.utcnow()
    db.flush()
    return stock


def complete_reserved_stock(db: Session, service_id: int, quantity: int) -> Stock:
    from utils.stock_display import align_quantity_to_login_lines, login_detail_lines

    stock = get_or_create_stock(db, service_id)
    if stock.reserved_qty < quantity:
        raise InsufficientStockError("Reserved quantity is lower than completion quantity")
    stock.quantity = max(stock.quantity - quantity, 0)
    stock.reserved_qty = max(stock.reserved_qty - quantity, 0)
    stock.last_updated = datetime.utcnow()
    service = db.get(Service, service_id)
    # Stock-delivery products: keep quantity aligned to remaining login lines.
    if service and getattr(service, "fulfillment_type", None) == "stock":
        align_quantity_to_login_lines(stock)
    if service and (stock.quantity <= 0 or (getattr(service, "fulfillment_type", None) == "stock" and not login_detail_lines(stock))):
        service.is_active = False
    db.flush()
    return stock
