"""Products / Categories / Payment methods ki list ab ek clean TEXT message
ke through dikhayi jaati hai — koi generated PNG image nahi.

PEHLE yeh module Pillow se ek "card style" catalog IMAGE banata tha (har item
apni alag color wali row mein). Wo hata diya gaya hai kyunke:
  1. Telegram Premium "custom emoji" kabhi bhi ek image ke andar render nahi
     ho sakti — yeh sirf real message TEXT (ya photo caption TEXT) mein hoti
     hai. Image wale approach mein customer ko hamesha fallback letter/logo
     hi dikhta, asal custom emoji kabhi nahi.
  2. Ek hi listing do jagah (image ke andar row + neeche text mein dobara)
     dikhana confusing tha.

Ab seedha ek text message jaata hai jismein har item ki apni icon (agar
custom emoji ID set hai to ASAL Telegram premium emoji, warna normal emoji)
+ naam + subtitle hota hai, aur uske neeche asli tappable inline-keyboard
buttons (jo caller ne banaye) attach hote hain — buttons pe bhi premium
custom emoji `icon_custom_emoji_id` se dikhti hai jab ID set ho.
"""

from __future__ import annotations

import html
from dataclasses import dataclass

# Telegram normal text message ki hard limit 4096 characters hai. Itna bada
# catalog aam taur par nahi banta, lekin safety ke liye trim kar dete hain.
TEXT_MESSAGE_LIMIT = 4000


@dataclass
class CatalogItem:
    title: str
    subtitle: str = ""
    emoji_value: str | None = None     # raw Category.emoji / Service.emoji / PaymentMethod.icon value
    # (plain emoji, ya Telegram Premium custom-emoji "ID|fallback" format) -
    # render_icon() isi se asal <tg-emoji> tag banata hai jo Telegram khud
    # parse karke real premium emoji dikha deta hai (parse_mode="HTML" ke
    # sath bheja gaya text/caption).


def category_catalog_item(category) -> "CatalogItem":
    return CatalogItem(
        title=category.name,
        subtitle="",
        emoji_value=getattr(category, "emoji", None),
    )


def service_catalog_item(service) -> "CatalogItem":
    return CatalogItem(
        title=service.name,
        subtitle="",
        emoji_value=getattr(service, "emoji", None),
    )


def payment_method_catalog_item(method) -> "CatalogItem":
    subtitle = method.network or ("Auto-verify" if method.method_type == "auto" else "Manual")
    return CatalogItem(
        title=method.name,
        subtitle=subtitle,
        emoji_value=getattr(method, "icon", None),
    )


def format_catalog_text(items: list[CatalogItem], title: str, caption: str = "") -> str:
    """Sirf ek chhota sa header text banata hai: bold title, uske neeche
    caption (agar di ho). Har item ka naam/price/stock ab dobara text mein
    NAHI dikhaya jaata — wo sab info pehle se hi neeche wale tappable
    buttons par maujood hai (icon + naam), isliye yahan repeat karna
    zaroorat nahi.
    """
    from utils.helpers import render_rich_html

    # render_rich_html keeps <tg-emoji> premium icons intact in the title.
    lines = [f"<b>{render_rich_html(title)}</b>"]
    if caption:
        lines.append(render_rich_html(caption))
    if not items:
        lines.append("Nothing here yet.")

    text = "\n".join(lines)
    if len(text) > TEXT_MESSAGE_LIMIT:
        text = text[: TEXT_MESSAGE_LIMIT - 1] + "…"
    return text


async def send_catalog_photo(
    target,
    items: list[CatalogItem],
    title: str,
    caption: str = "",
    reply_markup=None,
    parse_mode: str | None = "HTML",
):
    """Naam purana hai (backward-compatible - existing handlers isi naam se
    call karte hain), lekin ab yeh koi photo nahi bhejta — seedha ek text
    message bhejta hai jismein har item ki icon + naam + subtitle hota hai,
    neeche `reply_markup` (asal tappable buttons) attach hote hain.

    `target` koi bhi cheez ho sakti hai jis par `.answer(...)` call ho sake —
    aiogram `Message` (message.answer) ya `CallbackQuery.message`
    (callback.message.answer).
    """
    text = format_catalog_text(items, title, caption)
    await target.answer(text, reply_markup=reply_markup, parse_mode=parse_mode)
