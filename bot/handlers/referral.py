from datetime import datetime, timedelta

from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message

from database.models import ReferralEarning, SessionLocal, User
from utils.helpers import (
    build_referral_link,
    ensure_referral_code,
    format_commission,
    format_usdt,
    get_or_create_user,
    get_referral_settings,
)
from utils.ui_icons import label_icons

router = Router()


@router.message(Command("referral"))
async def referral_command(message: Message) -> None:
    db = SessionLocal()
    try:
        icons = label_icons(db)
        settings = get_referral_settings(db)
        if not settings["enabled"]:
            await message.answer(
                f"{icons['announce']} Referral Program\n\n"
                "The referral program is currently paused. Please check back later — "
                "we'll announce here as soon as it's active again.",
                parse_mode="HTML",
            )
            return

        user = get_or_create_user(db, str(message.from_user.id), message.from_user.username, message.from_user.full_name)
        referral = ensure_referral_code(db, user)
        bot_username = (await message.bot.get_me()).username
        link = build_referral_link(bot_username, referral.code)

        now = datetime.utcnow()
        referred_query = db.query(User).filter(User.referrer_id == user.id)
        referred_total = referred_query.count()
        referred_24h = referred_query.filter(User.joined_at >= now - timedelta(hours=24)).count()
        referred_7d = referred_query.filter(User.joined_at >= now - timedelta(days=7)).count()

        commission_label = format_commission(settings["commission_type"], settings["commission_value"])
        if settings["program_type"] == "per_purchase":
            mode_line = f"Program: Per purchase — {commission_label} on every order they complete."
        else:
            mode_line = f"Program: Per referral joined — {commission_label} once they become active (first deposit or purchase)."

        lines = [
            f"{icons['star']} Refer & Earn",
            "",
            f"{icons['users']} Referred (24h): {referred_24h}",
            f"{icons['users']} Referred (7d): {referred_7d}",
            f"{icons['users']} Referred (Total): {referred_total}",
            "",
            f"{icons['orders']} Total earned on this code: {format_usdt(referral.total_earned)}",
            f"{icons['referral']} Referral balance: {format_usdt(user.referral_wallet)}",
            "",
            mode_line,
            "",
            f"{icons['link']} Your referral link:\n{link}",
        ]

        recent = (
            db.query(ReferralEarning)
            .filter(ReferralEarning.referrer_id == user.id)
            .order_by(ReferralEarning.created_at.desc())
            .limit(5)
            .all()
        )
        if recent:
            lines.append("\nRecent earnings:")
            for earning in recent:
                lines.append(f"{format_usdt(earning.amount_earned)} - {earning.status}")
    finally:
        db.close()
    await message.answer("\n".join(lines), parse_mode="HTML")
