from sqlalchemy import func

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from database.models import Order, SessionLocal
from utils.helpers import build_referral_link, ensure_referral_code, format_usdt, get_or_create_user
from utils.ui_icons import label_icons

router = Router()


async def build_profile_text(bot_username: str, message_from_user) -> str:
    db = SessionLocal()
    try:
        user = get_or_create_user(
            db,
            str(message_from_user.id),
            message_from_user.username,
            message_from_user.full_name,
        )
        completed_orders = db.query(Order).filter(Order.user_id == user.id, Order.status == "completed").count()
        total_spent = (
            db.query(func.coalesce(func.sum(Order.amount_usdt), 0.0))
            .filter(Order.user_id == user.id, Order.status == "completed")
            .scalar()
        )
        referral = ensure_referral_code(db, user)
        referral_link = build_referral_link(bot_username, referral.code)
        icons = label_icons(db)

        lines = [
            f"{icons['member']} Customer profile",
            "",
            f"Name: {user.full_name or '-'}",
            f"Username: @{user.username}" if user.username else "Username: (not set)",
            f"User ID: {user.telegram_id}",
            f"{icons['price']} Total spent: {format_usdt(total_spent)}",
            f"{icons['orders']} Completed orders: {completed_orders}",
            "",
            f"{icons['wallet']} Wallet balance: {format_usdt(user.wallet_usdt)}",
            f"{icons['referral']} Referral balance: {format_usdt(user.referral_wallet)}",
            f"{icons['link']} Your referral link:\n{referral_link}",
        ]
        return "\n".join(lines)
    finally:
        db.close()


@router.message(Command("profile"))
async def profile_command(message: Message) -> None:
    me = await message.bot.get_me()
    text = await build_profile_text(me.username, message.from_user)
    await message.answer(text, parse_mode="HTML")
