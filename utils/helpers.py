import hashlib
import html
import os
import re
import secrets
import string
from datetime import datetime

from sqlalchemy.orm import Session

from database.models import ApiKey, BotConfig, Order, ReferralCode, User

BRAND_NAME = "SMF SHOP"
DEFAULT_WELCOME = f"Welcome to {BRAND_NAME}!"
_OLD_WELCOME_MARKERS = (
    "smm reseller",
    "aurex",
    "welcome to the smm",
)
_TG_EMOJI_SPLIT = re.compile(r'(<tg-emoji emoji-id="\d+">.*?</tg-emoji>)', re.DOTALL)


def is_legacy_welcome(msg: str | None) -> bool:
    text = (msg or "").strip().lower()
    if not text:
        return True
    return any(marker in text for marker in _OLD_WELCOME_MARKERS)


def resolve_welcome_msg(config: BotConfig | None, db: Session | None = None) -> str:
    """Always prefer SMF SHOP when DB still has Aurex / SMM Reseller welcome text.

    Persists the fix when a Session is passed so the next /start is clean too.
    """
    raw = (config.welcome_msg if config else None) or ""
    if not is_legacy_welcome(raw):
        return raw.strip()
    if config is not None and db is not None and (config.welcome_msg or "").strip() != DEFAULT_WELCOME:
        config.welcome_msg = DEFAULT_WELCOME
        try:
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
    return DEFAULT_WELCOME


def _normalize_icon_fallback(display: str | None, fallback: str) -> str:
    """Avoid plain text like 'NEW'/'OFF' as Telegram custom-emoji fallback.

    Telegram can show the fallback text when a premium emoji does not resolve;
    if admins saved words instead of a real emoji, the UI looks broken and may
    appear as if animation disappeared. In that case prefer the contextual
    fallback emoji passed by the caller.
    """
    value = (display or "").strip()
    if not value:
        return fallback
    # Plain ASCII words/digits are poor emoji fallbacks; use caller fallback.
    if re.fullmatch(r"[A-Za-z0-9 _-]+", value):
        return fallback
    return value


def parse_icon(value: str | None, fallback: str = "📦") -> tuple[str | None, str]:
    """Icon field ko (custom_emoji_id, display_emoji) mein todta hai.

    Admin panel me icon field 2 tarah se fill ki ja sakti hai:
      1. Normal emoji, jaise "📦" ya "💳"
      2. Telegram Premium custom emoji: CUSTOM_EMOJI_ID|fallback_emoji
         e.g. "5368324170671202286|🔥"

    Returns:
      (emoji_id_or_None, fallback_or_plain_emoji)
    """
    value = (value or "").strip()
    if not value:
        return None, fallback

    if "|" in value:
        emoji_id, _, fallback_emoji = value.partition("|")
        emoji_id = emoji_id.strip()
        fallback_emoji = _normalize_icon_fallback(fallback_emoji.strip(), fallback)
        if emoji_id.isdigit():
            return emoji_id, fallback_emoji
        # Galat format - crash hone ke bajaye fallback dikha do.
        return None, fallback_emoji

    if value.isdigit():
        return value, fallback

    return None, _normalize_icon_fallback(value, fallback)


def extract_icon_from_rich_text(text: str | None, fallback: str = "📦") -> str:
    """Pull ID|fallback (or plain emoji) from rich name/description for Category.emoji / buttons."""
    value = text or ""
    match = re.search(
        r'<tg-emoji\s+emoji-id="(\d+)">([^<]*)</tg-emoji>',
        value,
        flags=re.IGNORECASE,
    )
    if match:
        fb = _normalize_icon_fallback((match.group(2) or "").strip(), fallback)
        return f"{match.group(1)}|{fb}"

    # Combined format already in the string
    combined = re.search(r"\b(\d{10,})\|([^\s<]{1,20})", value)
    if combined:
        return f"{combined.group(1)}|{_normalize_icon_fallback(combined.group(2), fallback)}"

    # Leading plain emoji / symbol before letters
    leading = re.match(r"^(\W+)(\w|$)", value.strip(), flags=re.UNICODE)
    if leading:
        glyph = leading.group(1).strip()
        if glyph and not glyph.isdigit():
            return _normalize_icon_fallback(glyph, fallback)

    return fallback


def rich_name_with_icon(name: str | None, icon_value: str | None, fallback: str = "📦") -> str:
    """Build editor value: keep embedded tg-emoji, else prefix from ID|fallback icon field."""
    raw = (name or "").strip()
    if "<tg-emoji" in raw.lower():
        return raw
    emoji_id, display = parse_icon(icon_value, fallback)
    plain = strip_html_tags(raw).strip() or raw
    if emoji_id:
        return f'<tg-emoji emoji-id="{emoji_id}">{html.escape(display)}</tg-emoji> {plain}'.strip()
    if display and display not in plain:
        return f"{display} {plain}".strip()
    return plain or fallback


def render_icon(value: str | None, fallback: str = "📦", html_mode: bool = True) -> str:
    """Category.emoji / Service.emoji / PaymentMethod.icon ko render karta hai.

    html_mode=True  -> message text ke liye <tg-emoji> tag (parse_mode=\"HTML\" zaroori).
    html_mode=False -> sirf fallback/plain emoji string (legacy button text ke liye).

    Buttons pe asal premium emoji dikhane ke liye `icon_button()` / `icon_custom_emoji_id`
    use karo — Bot API 9.4+ pe inline buttons custom emoji support karti hain.
    """
    emoji_id, display = parse_icon(value, fallback)
    if emoji_id and html_mode:
        return f'<tg-emoji emoji-id="{emoji_id}">{html.escape(display)}</tg-emoji>'
    return display


def render_rich_html(text: str | None) -> str:
    """Escape plain text but keep admin-inserted <tg-emoji> premium tags intact."""
    value = text or ""
    if not value:
        return ""
    if "<tg-emoji" not in value:
        return html.escape(value)
    parts = _TG_EMOJI_SPLIT.split(value)
    out: list[str] = []
    for index, part in enumerate(parts):
        if index % 2 == 1:
            out.append(part)
        else:
            out.append(html.escape(part))
    return "".join(out)


_TG_EMOJI_INNER = re.compile(
    r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>',
    re.DOTALL,
)


def strip_tg_emoji_html(text: str | None) -> str:
    """Replace <tg-emoji>…</tg-emoji> with inner fallback text (for plain send retry)."""
    value = text or ""
    if "<tg-emoji" not in value:
        return value
    return _TG_EMOJI_INNER.sub(r"\1", value)


def strip_html_tags(text: str | None) -> str:
    """Remove remaining HTML tags after tg-emoji has been flattened."""
    value = strip_tg_emoji_html(text)
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"</p\s*>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", "", value)
    return html.unescape(value)


# Leading "Description:" line — plain, emoji, or <tg-emoji>…</tg-emoji> prefix.
# Product card already prints that label; body must not repeat it.
_LEADING_DESCRIPTION_LABEL = re.compile(
    r"""
    ^\s*
    (?:
        <tg-emoji\b[^>]*>.*?</tg-emoji>   # premium emoji HTML
        |
        [^\w<\n]{1,12}                    # plain emoji / symbols
    )?
    \s*
    Description:\s*
    (?:\r?\n)?
    """,
    re.IGNORECASE | re.DOTALL | re.VERBOSE,
)


def strip_leading_description_label(text: str | None) -> str:
    """Remove a leading Description: header so the product card does not show it twice."""
    raw = (text or "").strip()
    return _LEADING_DESCRIPTION_LABEL.sub("", raw, count=1).strip()


def format_description_block(
    description: str | None,
    *,
    label: str = "",
    label_icon_html: str = "",
) -> str:
    """Product description ko Telegram blockquote (green quote box) mein wrap karta hai.

    Empty label → body only inside the box (matches product-card box style).
    """
    raw = strip_leading_description_label(description)
    body = render_rich_html(raw) or "No description provided."
    label_text = (label or "").strip()
    if not label_text:
        return f"<blockquote>{body}</blockquote>"
    prefix = f"{label_icon_html} " if label_icon_html else ""
    return f"<blockquote>{prefix}<b>{html.escape(label_text)}</b>\n{body}</blockquote>"


def icon_to_tg_tag(value: str | None, fallback: str = "✨") -> str:
    """Convert ID|fallback (or plain emoji) into an HTML tg-emoji / plain fragment."""
    return render_icon(value, fallback=fallback, html_mode=True)


def icon_custom_emoji_id(value: str | None) -> str | None:
    """Agar value premium custom emoji ID hold karti hai to woh ID, warna None."""
    emoji_id, _ = parse_icon(value)
    return emoji_id


def icon_button(
    text: str,
    *,
    icon_value: str | None = None,
    icon_fallback: str = "📦",
    prefix_fallback: bool = True,
    **kwargs,
):
    """InlineKeyboardButton banata hai — premium icon set ho to `icon_custom_emoji_id`
    lagata hai (SMF / Bot API 9.4+ jaisa), warna text ke aage normal emoji.

    prefix_fallback=True: jab premium ID na ho to button text se pehle fallback
    emoji (📦/💳) lagata hai. Premium ID ho to text clean rehta hai aur Telegram
    button pe asal custom emoji icon dikhata hai.
    """
    from aiogram.types import InlineKeyboardButton

    emoji_id, display = parse_icon(icon_value, icon_fallback)
    if emoji_id:
        return InlineKeyboardButton(text=text, icon_custom_emoji_id=emoji_id, **kwargs)
    if prefix_fallback and display:
        return InlineKeyboardButton(text=f"{display} {text}", **kwargs)
    return InlineKeyboardButton(text=text, **kwargs)


def get_public_base_url() -> str | None:
    """Deployed app ka asal public root URL (jaise https://xxx.up.railway.app).
    Wahi env vars use karta hai jo Telegram webhook setup ke liye use hote hain
    (main.py: get_webhook_url), taake API base URL kabhi placeholder text
    ("your-railway-domain") na rahe — developer ko hamesha real, working URL mile."""
    base_url = (
        os.getenv("WEBHOOK_URL")
        or os.getenv("WEBHOOK_BASE_URL")
        or os.getenv("RAILWAY_PUBLIC_DOMAIN")
        or os.getenv("PUBLIC_BASE_URL")
    )
    if not base_url:
        return None
    # WEBHOOK_URL sometimes includes the webhook path; strip it for the site root.
    base_url = base_url.strip().rstrip("/")
    for suffix in ("/telegram/webhook", "/webhook"):
        if base_url.endswith(suffix):
            base_url = base_url[: -len(suffix)]
            break
    if not base_url.startswith("http"):
        base_url = f"https://{base_url}"
    return base_url.rstrip("/")


def normalize_mini_app_url(value: str | None) -> str | None:
    """Return a usable Mini App / storefront URL, or None if empty/invalid."""
    url = (value or "").strip().rstrip("/")
    if not url:
        return None
    if url.startswith(("https://", "http://localhost", "http://127.0.0.1")):
        return url
    if url.startswith("http://"):
        # Telegram Mini Apps require HTTPS in production; keep http only for localhost.
        return None
    if "." in url:
        return f"https://{url}"
    return None


def get_mini_app_url(db: Session | None = None) -> str | None:
    """HTTPS URL of the web Mini App.

    Admin Settings overrides MINI_APP_URL env. Vercel sample storefronts are
    rewritten to this host's live /mini catalog.
    """
    env_url = normalize_mini_app_url(os.getenv("MINI_APP_URL"))
    config = None
    close_db = False
    if db is not None:
        config = db.query(BotConfig).first()
    else:
        try:
            from database.models import SessionLocal

            db = SessionLocal()
            close_db = True
            config = db.query(BotConfig).first()
        except Exception:  # noqa: BLE001
            config = None
    try:
        db_url = normalize_mini_app_url(getattr(config, "mini_app_url", None) if config else None)
    finally:
        if close_db and db is not None:
            db.close()
    return resolve_telegram_mini_app_url(db_url or env_url)


KNOWN_SHOP_ORIGIN = "https://web-production-80fac.up.railway.app"


def hosted_mini_app_url() -> str | None:
    """Same-origin Mini App that always reads live /api/web products."""
    base = get_public_base_url() or KNOWN_SHOP_ORIGIN
    return f"{base.rstrip('/')}/mini"


def resolve_telegram_mini_app_url(
    configured: str | None,
    *,
    public_base: str | None = None,
) -> str | None:
    """Telegram Mini App URL — never the Vercel mock catalog.

    aurex-shop-web.vercel.app still shows sample data until NEXT_PUBLIC_API_BASE_URL
    is set. Open Shop must use this host's /mini, which reads live /api/web products.
    A non-Vercel custom domain from Settings is kept.
    """
    if public_base is not None:
        base = public_base.strip().rstrip("/") or None
    else:
        base = get_public_base_url()
    if not base:
        base = KNOWN_SHOP_ORIGIN
    hosted = f"{base.rstrip('/')}/mini"
    configured = normalize_mini_app_url(configured)
    if configured and "vercel.app" not in configured.lower():
        return configured
    return hosted


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def format_usdt(amount: float) -> str:
    return f"{amount:.2f} USDT"


def generate_order_code(db: Session) -> str:
    alphabet = string.ascii_uppercase + string.digits
    while True:
        code = "SMM-" + "".join(secrets.choice(alphabet) for _ in range(8))
        if not db.query(Order).filter(Order.order_code == code).first():
            return code


def generate_public_key(db: Session) -> str:
    while True:
        key = "sk_" + secrets.token_urlsafe(24)
        if not db.query(ApiKey).filter(ApiKey.api_key == key).first():
            return key


def hash_secret(secret: str) -> str:
    from utils.security import hash_password
    return hash_password(secret)


def generate_api_credentials(db: Session, user: User, rate_limit: int | None = None) -> tuple[ApiKey, str]:
    """Create a new API key. Does not deactivate existing keys — use regenerate_api_credentials for rotate."""
    raw_secret = secrets.token_urlsafe(32)
    key = ApiKey(
        user_id=user.id,
        api_key=generate_public_key(db),
        secret_key=hash_secret(raw_secret),
        rate_limit=rate_limit or int(os.getenv("API_RATE_LIMIT_DEFAULT", "100")),
    )
    db.add(key)
    db.commit()
    db.refresh(key)
    return key, raw_secret


def regenerate_api_credentials(db: Session, user: User, rate_limit: int | None = None) -> tuple[ApiKey, str]:
    """Revoke all active keys for the user, then issue a fresh one (client 'Generate New Key')."""
    existing = db.query(ApiKey).filter(ApiKey.user_id == user.id, ApiKey.is_active.is_(True)).all()
    for key in existing:
        key.is_active = False
    db.flush()
    # Keep the previous rate limit if regenerating and admin hadn't set a custom one.
    if rate_limit is None and existing:
        rate_limit = existing[0].rate_limit
    return generate_api_credentials(db, user, rate_limit)

def ensure_referral_code(db: Session, user: User) -> ReferralCode:
    existing = db.query(ReferralCode).filter(ReferralCode.user_id == user.id, ReferralCode.is_active.is_(True)).first()
    if existing:
        return existing
    code = f"REF_{user.id}_{secrets.token_hex(3).upper()}"
    referral = ReferralCode(
        user_id=user.id,
        code=code,
        valid_from=datetime.utcnow(),
    )
    db.add(referral)
    db.commit()
    db.refresh(referral)
    return referral


def build_referral_link(bot_username: str, code: str) -> str:
    return f"https://t.me/{bot_username}?start=ref_{code}"


def get_referral_settings(db: Session) -> dict:
    """Single source of truth for the referral program's current settings.
    Reading this fresh (instead of relying on any per-user cached value) is
    what makes every referral link/message always show the admin's latest
    program type + commission, not whatever was true when the code was made."""
    config = db.query(BotConfig).first()
    program_type = (config.referral_program_type if config else None) or "per_purchase"
    commission_type = (config.referral_commission_type if config else None) or "percent"
    commission_value = config.referral_commission_value if config and config.referral_commission_value is not None else 15.0
    # Per-link (join) bonuses only ever make sense as a flat USDT amount —
    # there's no purchase total to take a percentage of.
    if program_type == "per_link":
        commission_type = "fixed"
    return {
        "enabled": bool(config and config.referral_enabled),
        "program_type": program_type,
        "commission_type": commission_type,
        "commission_value": float(commission_value),
    }


def format_commission(commission_type: str, commission_value: float) -> str:
    if commission_type == "fixed":
        return f"{commission_value:.2f} USDT"
    return f"{commission_value:.2f}%"


def is_self_referral(db: Session, referrer: User, referred: User) -> bool:
    """Fraud check for the referral program: flags a referrer/referred pair as
    a likely self-referral (someone farming their own link with throwaway
    accounts) when they share a payment fingerprint — the same crypto deposit
    from-address, or an identical display name — the same signals a manual
    admin review would look at."""
    from database.models import PaymentVerification, Transaction

    if referrer.id == referred.id:
        return True

    if (
        referrer.full_name
        and referred.full_name
        and referrer.full_name.strip().lower() == referred.full_name.strip().lower()
    ):
        return True

    referrer_addresses = {
        row[0]
        for row in db.query(PaymentVerification.from_address)
        .join(Transaction, Transaction.id == PaymentVerification.transaction_id)
        .filter(Transaction.user_id == referrer.id, PaymentVerification.from_address.isnot(None))
        .all()
    }
    if not referrer_addresses:
        return False

    referred_addresses = {
        row[0]
        for row in db.query(PaymentVerification.from_address)
        .join(Transaction, Transaction.id == PaymentVerification.transaction_id)
        .filter(Transaction.user_id == referred.id, PaymentVerification.from_address.isnot(None))
        .all()
    }
    return bool(referrer_addresses & referred_addresses)


def get_or_create_user(
    db: Session,
    telegram_id: str,
    username: str | None = None,
    full_name: str | None = None,
    referral_code: str | None = None,
) -> User:
    user = db.query(User).filter(User.telegram_id == telegram_id).first()
    if user:
        user.username = username or user.username
        user.full_name = full_name or user.full_name
        db.commit()
        db.refresh(user)
        return user

    referrer_id = None
    if referral_code:
        referral = db.query(ReferralCode).filter(ReferralCode.code == referral_code, ReferralCode.is_active.is_(True)).first()
        if referral:
            referrer_id = referral.user_id
            referral.usage_count += 1

    user = User(telegram_id=telegram_id, username=username, full_name=full_name, referrer_id=referrer_id)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user
