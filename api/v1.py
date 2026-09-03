from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.auth import get_api_key_record, get_current_api_user, get_usage_stats
from database.models import ApiKey, Order, Service, Transaction, User, Webhook, get_db
from utils.helpers import generate_order_code
from utils.notifications import notify_admin_new_order, notify_channel_order_completed
from utils.provider_api import ProviderApiError, place_order
from utils.stock_manager import InsufficientStockError, complete_reserved_stock, consume_stock_account, reserve_stock


router = APIRouter(prefix="/api/v1", tags=["api-v1"])


class VerifyRequest(BaseModel):
    api_key: str


class OrderCreateRequest(BaseModel):
    sku: str
    quantity: int = Field(gt=0)
    link: str = Field(min_length=3, max_length=1000)
    webhook_url: str | None = None


class WebhookRequest(BaseModel):
    webhook_url: str
    event_types: list[str]


def service_payload(service: Service) -> dict:
    stock = service.stock
    available = stock.available_qty if stock else 0
    return {
        "id": service.id,
        "sku": service.sku,
        "name": service.name,
        "description": service.description,
        "price": service.sell_price,
        "category": service.category.name if service.category else None,
        "min_qty": service.min_qty,
        "max_qty": service.max_qty,
        "stock": available,
        "is_in_stock": available > 0,
        "is_active": service.is_active,
    }


@router.post("/auth/verify")
def verify_api_key(payload: VerifyRequest, db: Session = Depends(get_db)) -> dict:
    from utils.security import constant_time_compare

    key = db.query(ApiKey).filter(ApiKey.api_key == payload.api_key, ApiKey.is_active.is_(True)).first()
    if not key or not constant_time_compare(key.api_key, payload.api_key) or (key.expires_at and key.expires_at < datetime.utcnow()):
        return {"valid": False}
    return {
        "valid": True,
        "user": {"id": key.user.id, "telegram_id": key.user.telegram_id, "username": key.user.username},
        "rate_limit": key.rate_limit,
    }


@router.get("/products")
def list_products(
    current_user: Annotated[User, Depends(get_current_api_user)],
    db: Session = Depends(get_db),
    category: str | None = None,
    active: bool = True,
) -> list[dict]:
    query = db.query(Service)
    if active:
        query = query.filter(Service.is_active.is_(True))
    if category:
        query = query.join(Service.category).filter_by(name=category)
    return [service_payload(service) for service in query.order_by(Service.sort_order.asc(), Service.name.asc()).all()]


@router.get("/products/{sku}")
def get_product(sku: str, current_user: Annotated[User, Depends(get_current_api_user)], db: Session = Depends(get_db)) -> dict:
    service = db.query(Service).filter(Service.sku == sku, Service.is_active.is_(True)).first()
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    return service_payload(service)


@router.post("/orders/create")
async def create_order(
    payload: OrderCreateRequest,
    current_user: Annotated[User, Depends(get_current_api_user)],
    db: Session = Depends(get_db),
) -> dict:
    service = db.query(Service).filter(Service.sku == payload.sku, Service.is_active.is_(True)).first()
    if not service:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    if payload.quantity < service.min_qty or payload.quantity > service.max_qty:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Quantity must be between {service.min_qty} and {service.max_qty}")

    from utils.pricing import resolve_unit_price

    quote = resolve_unit_price(db, service, current_user)
    amount = round(quote.unit_price * payload.quantity, 6)
    if current_user.wallet_usdt < amount:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Insufficient wallet balance")

    try:
        reserve_stock(db, service.id, payload.quantity)
    except InsufficientStockError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    current_user.wallet_usdt -= amount
    order = Order(
        order_code=generate_order_code(db),
        user_id=current_user.id,
        service_id=service.id,
        link=payload.link,
        quantity=payload.quantity,
        amount_usdt=amount,
        status="manual_pending",
        order_type="api",
        payment_method="WALLET",
        note="Paid with wallet via API.",
    )
    db.add(order)
    db.add(Transaction(user_id=current_user.id, amount=amount, tx_type="deduct", status="confirmed", blockchain_status="confirmed", note=f"Order {order.order_code}"))
    db.flush()

    provider = service.provider
    fulfillment_type = getattr(service, "fulfillment_type", "auto")
    if fulfillment_type == "stock":
        delivered = consume_stock_account(db, service.id, payload.quantity)
        if delivered is None:
            order.status = "manual_pending"
            order.note = "Stock delivery: not enough account details entered in stock yet."
        else:
            order.delivered_info = "\n".join(delivered)
            order.status = "completed"
            order.completed_at = datetime.utcnow()
            order.note = "Delivered automatically from stock."
            order.order_type = "stock"
            complete_reserved_stock(db, order.service_id, order.quantity)
    elif provider and provider.type == "api" and service.provider_service_id:
        try:
            provider_response = await place_order(
                provider,
                service.provider_service_id,
                payload.link,
                payload.quantity,
                external_order_id=order.order_code,
            )
            from utils.provider_delivery import extract_provider_order_id

            order.provider_order_id = extract_provider_order_id(provider_response) or order.order_code
            order.status = "processing"
            order.order_type = "api"
        except ProviderApiError as exc:
            order.note = str(exc)
            order.status = "manual_pending"

    if payload.webhook_url:
        for event_type in ("order_completed", "order_failed"):
            existing_hook = (
                db.query(Webhook)
                .filter(
                    Webhook.user_id == current_user.id,
                    Webhook.webhook_url == payload.webhook_url,
                    Webhook.event_type == event_type,
                )
                .first()
            )
            if existing_hook:
                existing_hook.is_active = True
            else:
                db.add(Webhook(user_id=current_user.id, webhook_url=payload.webhook_url, event_type=event_type))

    db.commit()
    db.refresh(order)
    await notify_admin_new_order(order, current_user, service)
    if order.status == "completed":
        await notify_channel_order_completed(order, service, db)
    return {"order_code": order.order_code, "status": order.status, "created_at": order.created_at.isoformat()}


@router.get("/orders/{order_code}")
def get_order(order_code: str, current_user: Annotated[User, Depends(get_current_api_user)], db: Session = Depends(get_db)) -> dict:
    order = db.query(Order).filter(Order.order_code == order_code, Order.user_id == current_user.id).first()
    if not order:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    return {
        "order_code": order.order_code,
        "status": order.status,
        "provider_status": order.provider_status,
        "service": order.service.name,
        "quantity": order.quantity,
        "amount": order.amount_usdt,
        "delivered_info": order.delivered_info,
        "note": order.note,
        "created_at": order.created_at.isoformat(),
        "completed_at": order.completed_at.isoformat() if order.completed_at else None,
    }


@router.get("/account/balance")
def get_balance(current_user: Annotated[User, Depends(get_current_api_user)]) -> dict:
    return {"balance": current_user.wallet_usdt, "currency": "USDT"}


@router.post("/webhooks/register")
def register_webhooks(
    payload: WebhookRequest,
    current_user: Annotated[User, Depends(get_current_api_user)],
    db: Session = Depends(get_db),
) -> dict:
    created = []
    for event_type in payload.event_types:
        existing_hook = (
            db.query(Webhook)
            .filter(
                Webhook.user_id == current_user.id,
                Webhook.webhook_url == payload.webhook_url,
                Webhook.event_type == event_type,
            )
            .first()
        )
        if existing_hook:
            existing_hook.is_active = True
        else:
            db.add(Webhook(user_id=current_user.id, webhook_url=payload.webhook_url, event_type=event_type))
        created.append(event_type)
    db.commit()
    return {"registered": created, "webhook_url": payload.webhook_url}


@router.get("/stats")
def stats(key: Annotated[ApiKey, Depends(get_api_key_record)]) -> dict:
    return get_usage_stats(key)
