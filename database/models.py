import os
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker


def build_database_url() -> str:
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        if database_url.startswith(("http://", "https://")):
            return fallback_database_url()
        if database_url.startswith("postgres://"):
            return database_url.replace("postgres://", "postgresql+psycopg://", 1)
        if database_url.startswith("postgresql://"):
            return database_url.replace("postgresql://", "postgresql+psycopg://", 1)
        if database_url.startswith(("sqlite://", "postgresql+psycopg://")):
            return database_url
        if "://" not in database_url:
            return f"sqlite:///{database_url}"
        return fallback_database_url()

    return fallback_database_url()


def fallback_database_url() -> str:
    volume_path = os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
    if volume_path:
        return f"sqlite:///{volume_path.rstrip('/')}/smm_reseller.db"

    return "sqlite:///./smm_reseller.db"


DATABASE_URL = build_database_url()

connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Provider(Base, TimestampMixin):
    __tablename__ = "providers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    type: Mapped[str] = mapped_column(String(20), default="manual")
    api_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    api_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    telegram_bot: Mapped[str | None] = mapped_column(String(120), nullable=True)
    contact: Mapped[str | None] = mapped_column(String(200), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    balance_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Upstream API wallet (synced on Sync stock/prices + background job)
    api_balance: Mapped[float | None] = mapped_column(Float, nullable=True)
    api_username: Mapped[str | None] = mapped_column(String(120), nullable=True)
    balance_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    low_balance_alert_active: Mapped[bool] = mapped_column(Boolean, default=False)

    services: Mapped[list["Service"]] = relationship(back_populates="provider")


class AuditLog(Base, TimestampMixin):
    """Admin panel activity — create / edit / delete / sync / settings, etc."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    entity_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    entity_label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    administrator: Mapped[str] = mapped_column(String(120), default="admin")
    address: Mapped[str | None] = mapped_column(String(120), nullable=True)
    change_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(500), unique=True, nullable=False)
    emoji: Mapped[str] = mapped_column(String(60), default="📦")  # normal emoji, ya premium/custom emoji "ID|fallback"
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_path: Mapped[str | None] = mapped_column(String(500), nullable=True)  # optional custom icon image
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    services: Mapped[list["Service"]] = relationship(back_populates="category")

    @property
    def display_icon(self) -> str:
        """Category button ka icon — image ho to woh use hoga, warna emoji/text."""
        return self.emoji or "📦"


class IconPreset(Base, TimestampMixin):
    """Ek dafa kisi brand ka Telegram custom emoji ID save kar do (e.g. "ChatGPT"),
    phir Category/Service/PaymentMethod edit karte waqt dropdown se seedha pick ho
    jaye — har dafa ID dobara paste karne ki zaroorat nahi (canboso.com jaisa
    ready-made icon picker)."""

    __tablename__ = "icon_presets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    emoji_id: Mapped[str] = mapped_column(String(40), nullable=False)  # Telegram custom_emoji_id (digits only)
    fallback_emoji: Mapped[str] = mapped_column(String(20), default="📦")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)

    @property
    def combined_value(self) -> str:
        """Emoji/Icon fields format: 'ID|fallback' - yehi format render_icon() padhta hai."""
        return f"{self.emoji_id}|{self.fallback_emoji}"

    @property
    def tg_tag(self) -> str:
        """HTML tag for pasting into announcements / descriptions (parse_mode=HTML)."""
        return f'<tg-emoji emoji-id="{self.emoji_id}">{self.fallback_emoji}</tg-emoji>'


class DescriptionTemplate(Base, TimestampMixin):
    """Reusable product description bodies (admin saves once, applies on Add/Edit Product)."""

    __tablename__ = "description_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)


class MenuCommand(Base, TimestampMixin):
    """Bot menu / reply keyboard command — admin can edit label + premium emoji.

    key is stable (shop, catalog, …). name is shown on inline buttons; reply_name
    overrides the bottom reply-keyboard label when set.
    icon uses the same format as other emoji fields: plain emoji OR 'ID|fallback'.
    """

    __tablename__ = "menu_commands"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    reply_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    icon: Mapped[str] = mapped_column(String(80), default="✨")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    @property
    def reply_label(self) -> str:
        return (self.reply_name or self.name or self.key).strip()


class Service(Base, TimestampMixin):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_id: Mapped[int | None] = mapped_column(ForeignKey("providers.id"), nullable=True)
    category_id: Mapped[int | None] = mapped_column(ForeignKey("categories.id"), nullable=True)
    provider_service_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    sku: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Shown in delivery copyable receipt; if empty, bot may parse Warranty from description.
    warranty: Mapped[str | None] = mapped_column(String(200), nullable=True)
    emoji: Mapped[str | None] = mapped_column(String(60), nullable=True)  # normal emoji, ya premium/custom emoji "ID|fallback"
    image_path: Mapped[str | None] = mapped_column(String(500), nullable=True)  # optional real logo/icon image
    cost_price: Mapped[float] = mapped_column(Float, default=0.0)
    sell_price: Mapped[float] = mapped_column(Float, default=0.0)
    commission_pct: Mapped[float] = mapped_column(Float, default=0.0)
    # Extra fixed USDT added on top of cost×(1+commission%) for API auto pricing.
    markup_fixed_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    # When True, provider sync updates cost/stock only — keeps admin sell_price.
    manual_sell_price: Mapped[bool] = mapped_column(Boolean, default=False)
    min_qty: Mapped[int] = mapped_column(Integer, default=1)
    max_qty: Mapped[int] = mapped_column(Integer, default=10000)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    # "auto" = provider API/instant delivery. "manual" = admin has to fulfil the
    # order by hand (DM customer for email etc.) — drives the admin notification.
    fulfillment_type: Mapped[str] = mapped_column(String(20), default="auto")
    # When True, bot asks for customer email after quantity (for invite/team plans).
    # Admin sees that email on the order and invites them, then marks completed.
    require_email: Mapped[bool] = mapped_column(Boolean, default=False)

    provider: Mapped[Provider | None] = relationship(back_populates="services")
    category: Mapped[Category | None] = relationship(back_populates="services")
    stock: Mapped["Stock | None"] = relationship(back_populates="service", uselist=False)
    orders: Mapped[list["Order"]] = relationship(back_populates="service")
    sales: Mapped[list["ProductSale"]] = relationship(back_populates="service")


class ProductSale(Base, TimestampMixin):
    """Admin-managed product promotions (Flash / Season End / Black Friday, etc.)."""

    __tablename__ = "product_sales"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"), nullable=False, index=True)
    sale_type: Mapped[str] = mapped_column(String(40), nullable=False, default="flash")
    original_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    sale_price: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    duration_hours: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    service: Mapped["Service"] = relationship(back_populates="sales")

    @property
    def sale_type_label(self) -> str:
        labels = {
            "flash": "Flash Sale",
            "season_end": "Season End Sale",
            "new_year": "New Year Sale",
            "black_friday": "Black Friday",
            "cyber_monday": "Cyber Monday",
            "clearance": "Clearance Sale",
            "weekend": "Weekend Sale",
            "summer": "Summer Sale",
            "winter": "Winter Sale",
            "spring": "Spring Sale",
            "eid": "Eid Sale",
            "valentine": "Valentine Sale",
            "christmas": "Christmas Sale",
            "mega": "Mega Sale",
            "introductory": "Introductory Offer",
            "price_drop": "Price Drop",
            "back_to_school": "Back to School",
            "anniversary": "Anniversary Sale",
            "mid_season": "Mid-Season Sale",
            "liquidation": "Liquidation Sale",
        }
        return labels.get(self.sale_type, (self.sale_type or "").replace("_", " ").title())


class UserProductDiscount(Base, TimestampMixin):
    """Per-user special price on a product (admin-granted personal discount)."""

    __tablename__ = "user_product_discounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"), nullable=False, index=True)
    # percent = % off current sell_price; fixed = USDT off; price = absolute unit price
    discount_type: Mapped[str] = mapped_column(String(20), nullable=False, default="percent")
    value: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)

    user: Mapped["User"] = relationship()
    service: Mapped["Service"] = relationship()

    @property
    def type_label(self) -> str:
        return {
            "percent": "% off",
            "fixed": "USDT off",
            "price": "Special price",
        }.get(self.discount_type, self.discount_type)


class Stock(Base):
    __tablename__ = "stocks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"), unique=True, nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, default=0)
    reserved_qty: Mapped[int] = mapped_column(Integer, default=0)
    low_stock_threshold: Mapped[int] = mapped_column(Integer, default=lambda: int(os.getenv("LOW_STOCK_THRESHOLD", "10")))
    last_updated: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Optional account/login inventory text for manual-fulfillment products
    # (e.g. "email:pass" list) so admin has it ready when completing an order.
    login_details: Mapped[str | None] = mapped_column(Text, nullable=True)

    service: Mapped[Service] = relationship(back_populates="stock")

    @property
    def available_qty(self) -> int:
        return max(self.quantity - self.reserved_qty, 0)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_id: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    username: Mapped[str | None] = mapped_column(String(120), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    wallet_usdt: Mapped[float] = mapped_column(Float, default=0.0)
    referral_wallet: Mapped[float] = mapped_column(Float, default=0.0)
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    referrer_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    # Prevents the one-off "per link" join bonus from being paid more than
    # once for the same referred user, and stops it firing until they prove
    # they're a real/active account (see credit_referral_join_bonus).
    referral_join_credited: Mapped[bool] = mapped_column(Boolean, default=False)
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    language: Mapped[str] = mapped_column(String(10), default="en")
    # Force-join: False until user taps ✅ I have joined and membership is verified.
    # Old users start False so they must confirm even if already in channel/group.
    force_join_ok: Mapped[bool] = mapped_column(Boolean, default=False)

    referrer: Mapped["User | None"] = relationship(remote_side="User.id")
    orders: Mapped[list["Order"]] = relationship(back_populates="user")
    transactions: Mapped[list["Transaction"]] = relationship(back_populates="user")
    api_keys: Mapped[list["ApiKey"]] = relationship(back_populates="user")


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_code: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"), nullable=False)
    provider_order_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    link: Mapped[str] = mapped_column(String(1000), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    amount_usdt: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="pending")
    provider_status: Mapped[str | None] = mapped_column(String(80), nullable=True)
    order_type: Mapped[str] = mapped_column(String(20), default="manual")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # How the customer paid: WALLET, PAYFAST, BEP20, TRC20, JazzCash, etc.
    payment_method: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Delivered account/login details, kept separate from `note` so admin panel
    # and the bot's order-detail view can show it as its own clean section.
    delivered_info: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Customer email collected at checkout (products with require_email=True).
    # Admin uses this to invite/add the client to a plan, then complete the order.
    customer_email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # When False, auto-expire still runs but the client is not messaged
    # (they left the payment screen and opened another command).
    expire_notify: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Prorated refund tool: status becomes "refunded"; method is wallet|manual.
    refund_method: Mapped[str | None] = mapped_column(String(20), nullable=True)
    refund_amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped[User] = relationship(back_populates="orders")
    service: Mapped[Service] = relationship(back_populates="orders")
    refund_logs: Mapped[list["RefundLog"]] = relationship(back_populates="order")


class RefundLog(Base):
    """Audit trail for admin Refund Tool actions."""

    __tablename__ = "refund_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    order_code: Mapped[str] = mapped_column(String(20), nullable=False)
    admin_name: Mapped[str] = mapped_column(String(80), default="admin")
    refund_amount: Mapped[float] = mapped_column(Float, nullable=False)
    refund_method: Mapped[str] = mapped_column(String(20), nullable=False)  # wallet | manual
    days_total: Mapped[int] = mapped_column(Integer, nullable=False)
    days_used: Mapped[int] = mapped_column(Integer, nullable=False)
    days_remaining: Mapped[int] = mapped_column(Integer, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    order: Mapped[Order] = relationship(back_populates="refund_logs")


class IssueReport(Base):
    """Client 'Report a problem' messages (sent from order-detail / post-order
    buttons). Shown to the admin on the Refund Tool page."""

    __tablename__ = "issue_reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), nullable=False)
    order_code: Mapped[str] = mapped_column(String(20), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    # Admin's reply note (also sent to the client via the Telegram bot on resolve).
    admin_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending | resolved
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    order: Mapped[Order] = relationship()
    user: Mapped[User] = relationship()


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    tx_type: Mapped[str] = mapped_column(String(40), nullable=False)
    tx_hash: Mapped[str | None] = mapped_column(String(200), nullable=True)
    blockchain_status: Mapped[str] = mapped_column(String(40), default="pending")
    status: Mapped[str] = mapped_column(String(40), default="pending")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Same meaning as Order.expire_notify — for wallet PayFast top-ups without an order.
    expire_notify: Mapped[bool] = mapped_column(Boolean, default=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # Customer-facing PayFast checkout reference (e.g. "SMFSHOP-A7K29Q").
    # Cryptographically random and DB-unique — NOT derived from this row's id,
    # so it can't be guessed/enumerated. Customers paste this back to the bot
    # to look up/recover a PayFast payment (see utils/payment_security.py and
    # api/payfast.py). Null for non-PayFast transactions.
    payfast_reference: Mapped[str | None] = mapped_column(String(40), unique=True, nullable=True, index=True)

    user: Mapped[User] = relationship(back_populates="transactions")
    verification: Mapped["PaymentVerification | None"] = relationship(back_populates="transaction", uselist=False)


class BotConfig(Base):
    __tablename__ = "bot_configs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bot_token: Mapped[str | None] = mapped_column(String(500), nullable=True)
    admin_tg_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    usdt_address: Mapped[str | None] = mapped_column(String(200), nullable=True)
    usdt_network: Mapped[str] = mapped_column(String(20), default="BEP20")
    welcome_msg: Mapped[str] = mapped_column(Text, default="Welcome to SMF SHOP!")
    min_deposit: Mapped[float] = mapped_column(Float, default=1.0)
    maintenance: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_verify_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    bscscan_api_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    tronscan_api_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    support_username: Mapped[str | None] = mapped_column(String(120), nullable=True)
    support_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    support_email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    support_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Support / channel contacts shown in the bot Support screen.
    support_whatsapp: Mapped[str | None] = mapped_column(String(40), nullable=True)  # digits, e.g. 923001234567
    tg_channel_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    whatsapp_channel_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Notify GROUP — all alerts: buy / stock / product / sale / maintenance (premium).
    orders_notify_chat_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Notify CHANNEL — announcements / flash-sale / maintenance only (no purchases).
    channel_notify_chat_id: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Global on/off switch for the whole referral program (separate from each
    # individual ReferralCode.is_active) — lets the admin pause/run a referral
    # campaign for everyone at once.
    referral_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Which referral program is running. Only ONE can be active at a time so a
    # link can never double-earn: "per_purchase" pays a cut of every completed
    # order made by someone you referred; "per_link" pays a one-off bonus once
    # the referred user proves they're real (first deposit/purchase).
    referral_program_type: Mapped[str] = mapped_column(String(20), default="per_purchase")
    # "percent" (of order amount, per_purchase only) or "fixed" (flat USDT).
    referral_commission_type: Mapped[str] = mapped_column(String(10), default="percent")
    referral_commission_value: Mapped[float] = mapped_column(Float, default=15.0)
    # PayFast charges customers in PKR while the bot's wallet/prices are in
    # USD/USDT, so we need a manual conversion rate the admin keeps updated
    # (e.g. 1 USD = 285 PKR). No live forex API is wired up - admin sets this
    # in Settings.
    usd_to_pkr_rate: Mapped[float] = mapped_column(Float, default=280.0)
    payfast_merchant_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    payfast_secured_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    payfast_store_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    payfast_base_url: Mapped[str] = mapped_column(String(200), default="https://ipg2.apps.net.pk")
    # Optional YouTube (or any) link shown to customers as a "Watch tutorial"
    # button under the PayFast top-up message, in case they don't know how to
    # complete the payment. Left blank = button is hidden.
    payfast_tutorial_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # Force-join gate: after /start, user must join channel+group and confirm
    # (real getChatMember check). Stock/product/sale DMs still go out regardless.
    force_join_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    force_join_channel: Mapped[str | None] = mapped_column(String(120), nullable=True)  # @channel or -100…
    force_join_channel_url: Mapped[str | None] = mapped_column(String(500), nullable=True)  # Join button URL
    force_join_group: Mapped[str | None] = mapped_column(String(500), nullable=True)
    force_join_group_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Sidebar "notification badge" last-seen timestamps. Revenue / Sold Accounts
    # badges count activity in a rolling window (not a real pending queue like
    # Orders), so simply opening that page should clear the badge — these
    # columns remember when the admin last opened each page and the badge
    # count is computed as "activity since this timestamp" instead of a fixed
    # 24h window once it's set.
    sidebar_seen_revenue_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sidebar_seen_sold_accounts_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sidebar_seen_orders_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sidebar_seen_users_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Announcement(Base, TimestampMixin):
    __tablename__ = "announcements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    image_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    is_sent: Mapped[bool] = mapped_column(Boolean, default=False)


class ApiKey(Base, TimestampMixin):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    api_key: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    secret_key: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    rate_limit: Mapped[int] = mapped_column(Integer, default=lambda: int(os.getenv("API_RATE_LIMIT_DEFAULT", "100")))
    last_used: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped[User] = relationship(back_populates="api_keys")


class ReferralCode(Base, TimestampMixin):
    __tablename__ = "referral_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    code: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    commission_pct: Mapped[float] = mapped_column(Float, default=lambda: float(os.getenv("REFERRAL_COMMISSION_DEFAULT", "15")))
    valid_from: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    valid_to: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    total_earned: Mapped[float] = mapped_column(Float, default=0.0)


class ReferralEarning(Base, TimestampMixin):
    __tablename__ = "referral_earnings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    referrer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    referred_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    order_id: Mapped[int | None] = mapped_column(ForeignKey("orders.id"), nullable=True)
    # "per_purchase" (commission on an order) or "per_link" (one-off join bonus).
    earning_type: Mapped[str] = mapped_column(String(20), default="per_purchase")
    amount_earned: Mapped[float] = mapped_column(Float, default=0.0)
    # "credited", "pending", or "voided_self_referral" (fraud check flagged the pair).
    status: Mapped[str] = mapped_column(String(40), default="pending")
    credited_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Webhook(Base, TimestampMixin):
    __tablename__ = "webhooks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    webhook_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class PaymentVerification(Base, TimestampMixin):
    __tablename__ = "payment_verifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    transaction_id: Mapped[int] = mapped_column(ForeignKey("transactions.id"), nullable=False)
    tx_hash: Mapped[str] = mapped_column(String(200), nullable=False)
    blockchain: Mapped[str] = mapped_column(String(20), nullable=False)
    contract_address: Mapped[str | None] = mapped_column(String(200), nullable=True)
    from_address: Mapped[str | None] = mapped_column(String(200), nullable=True)
    to_address: Mapped[str | None] = mapped_column(String(200), nullable=True)
    amount_verified: Mapped[float] = mapped_column(Float, default=0.0)
    verification_status: Mapped[str] = mapped_column(String(40), default="pending")
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    api_response: Mapped[str | None] = mapped_column(Text, nullable=True)

    transaction: Mapped[Transaction] = relationship(back_populates="verification")


class PaymentMethod(Base, TimestampMixin):
    """Admin-managed payment methods shown to customers (wallet top-up & order checkout)."""

    __tablename__ = "payment_methods"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)  # e.g. "Binance Pay"
    code: Mapped[str] = mapped_column(String(40), unique=True, nullable=False)  # e.g. "BINANCE"
    method_type: Mapped[str] = mapped_column(String(20), default="manual")  # "auto" or "manual"
    network: Mapped[str | None] = mapped_column(String(40), nullable=True)  # e.g. "BEP20", "TRC20"
    address: Mapped[str | None] = mapped_column(String(500), nullable=True)  # wallet/account/number
    icon: Mapped[str] = mapped_column(String(60), default="💳")  # normal emoji, ya premium/custom emoji "ID|fallback"
    image_path: Mapped[str | None] = mapped_column(String(500), nullable=True)  # optional real logo/icon image
    instructions: Mapped[str | None] = mapped_column(Text, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Language(Base, TimestampMixin):
    """Admin-managed list of languages users can pick from (/language menu)."""

    __tablename__ = "languages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(10), unique=True, nullable=False)  # e.g. "en", "vi", "fa"
    name: Mapped[str] = mapped_column(String(80), nullable=False)  # e.g. "English", "Tiếng Việt"
    flag: Mapped[str] = mapped_column(String(10), default="🌐")
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_active_languages(db) -> list["Language"]:
    return (
        db.query(Language)
        .filter(Language.is_active.is_(True))
        .order_by(Language.sort_order.asc())
        .all()
    )


def get_active_payment_methods(db) -> list["PaymentMethod"]:
    return (
        db.query(PaymentMethod)
        .filter(PaymentMethod.is_active.is_(True))
        .order_by(PaymentMethod.sort_order.asc())
        .all()
    )


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    run_light_migrations()
    seed_defaults()


def run_light_migrations() -> None:
    """Add newly introduced columns to already-existing tables (no-op if already present)."""
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    table_names = inspector.get_table_names()

    if "services" in table_names:
        existing_columns = {col["name"] for col in inspector.get_columns("services")}
        if "is_deleted" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE services ADD COLUMN is_deleted BOOLEAN DEFAULT 0"))
        if "fulfillment_type" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE services ADD COLUMN fulfillment_type VARCHAR(20) DEFAULT 'auto'"))
        if "image_path" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE services ADD COLUMN image_path VARCHAR(500)"))
        if "sort_order" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE services ADD COLUMN sort_order INTEGER DEFAULT 0"))
        if "warranty" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE services ADD COLUMN warranty VARCHAR(200)"))
        if "require_email" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE services ADD COLUMN require_email BOOLEAN DEFAULT 0"))
        if "markup_fixed_usdt" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE services ADD COLUMN markup_fixed_usdt FLOAT DEFAULT 0"))
        if "manual_sell_price" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE services ADD COLUMN manual_sell_price BOOLEAN DEFAULT 0"))

    if "stocks" in table_names:
        existing_columns = {col["name"] for col in inspector.get_columns("stocks")}
        if "login_details" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE stocks ADD COLUMN login_details TEXT"))

    if "categories" in table_names:
        existing_columns = {col["name"] for col in inspector.get_columns("categories")}
        if "description" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE categories ADD COLUMN description TEXT"))
        if "image_path" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE categories ADD COLUMN image_path VARCHAR(500)"))
        # Widen name for embedded <tg-emoji> tags (Postgres). SQLite keeps flexible TEXT.
        try:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE categories ALTER COLUMN name TYPE VARCHAR(500)"))
        except Exception:
            pass

    if "orders" in table_names:
        existing_columns = {col["name"] for col in inspector.get_columns("orders")}
        if "delivered_info" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE orders ADD COLUMN delivered_info TEXT"))
        if "payment_method" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE orders ADD COLUMN payment_method VARCHAR(40)"))
        if "expire_notify" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE orders ADD COLUMN expire_notify BOOLEAN DEFAULT 1"))
        if "refund_method" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE orders ADD COLUMN refund_method VARCHAR(20)"))
        if "refund_amount" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE orders ADD COLUMN refund_amount FLOAT"))
        if "refunded_at" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE orders ADD COLUMN refunded_at DATETIME"))
        if "customer_email" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE orders ADD COLUMN customer_email VARCHAR(200)"))

    if "transactions" in table_names:
        existing_columns = {col["name"] for col in inspector.get_columns("transactions")}
        if "expire_notify" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE transactions ADD COLUMN expire_notify BOOLEAN DEFAULT 1"))

    if "issue_reports" in table_names:
        existing_columns = {col["name"] for col in inspector.get_columns("issue_reports")}
        if "admin_note" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE issue_reports ADD COLUMN admin_note TEXT"))
        if "status" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE issue_reports ADD COLUMN status VARCHAR(20) DEFAULT 'pending'"))
        if "resolved_at" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE issue_reports ADD COLUMN resolved_at DATETIME"))

    if "users" in table_names:
        existing_columns = {col["name"] for col in inspector.get_columns("users")}
        if "language" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE users ADD COLUMN language VARCHAR(10) DEFAULT 'en'"))
        if "force_join_ok" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE users ADD COLUMN force_join_ok BOOLEAN DEFAULT 0"))

    if "bot_configs" in table_names:
        existing_columns = {col["name"] for col in inspector.get_columns("bot_configs")}
        with engine.begin() as connection:
            if "support_username" not in existing_columns:
                connection.execute(text("ALTER TABLE bot_configs ADD COLUMN support_username VARCHAR(120)"))
            if "support_url" not in existing_columns:
                connection.execute(text("ALTER TABLE bot_configs ADD COLUMN support_url VARCHAR(500)"))
            if "support_email" not in existing_columns:
                connection.execute(text("ALTER TABLE bot_configs ADD COLUMN support_email VARCHAR(200)"))
            if "support_note" not in existing_columns:
                connection.execute(text("ALTER TABLE bot_configs ADD COLUMN support_note TEXT"))
            if "support_whatsapp" not in existing_columns:
                connection.execute(text("ALTER TABLE bot_configs ADD COLUMN support_whatsapp VARCHAR(40)"))
            if "tg_channel_url" not in existing_columns:
                connection.execute(text("ALTER TABLE bot_configs ADD COLUMN tg_channel_url VARCHAR(500)"))
            if "whatsapp_channel_url" not in existing_columns:
                connection.execute(text("ALTER TABLE bot_configs ADD COLUMN whatsapp_channel_url VARCHAR(500)"))
            if "orders_notify_chat_id" not in existing_columns:
                connection.execute(text("ALTER TABLE bot_configs ADD COLUMN orders_notify_chat_id VARCHAR(500)"))
            else:
                # Older installs used VARCHAR(80) — widen so t.me links are not truncated.
                try:
                    connection.execute(text("ALTER TABLE bot_configs ALTER COLUMN orders_notify_chat_id TYPE VARCHAR(500)"))
                except Exception:  # noqa: BLE001
                    pass
            if "channel_notify_chat_id" not in existing_columns:
                connection.execute(text("ALTER TABLE bot_configs ADD COLUMN channel_notify_chat_id VARCHAR(500)"))
            else:
                try:
                    connection.execute(text("ALTER TABLE bot_configs ALTER COLUMN channel_notify_chat_id TYPE VARCHAR(500)"))
                except Exception:  # noqa: BLE001
                    pass
            if "referral_enabled" not in existing_columns:
                connection.execute(text("ALTER TABLE bot_configs ADD COLUMN referral_enabled BOOLEAN DEFAULT 1"))
            if "referral_program_type" not in existing_columns:
                connection.execute(text("ALTER TABLE bot_configs ADD COLUMN referral_program_type VARCHAR(20) DEFAULT 'per_purchase'"))
            if "referral_commission_type" not in existing_columns:
                connection.execute(text("ALTER TABLE bot_configs ADD COLUMN referral_commission_type VARCHAR(10) DEFAULT 'percent'"))
            if "referral_commission_value" not in existing_columns:
                connection.execute(text("ALTER TABLE bot_configs ADD COLUMN referral_commission_value FLOAT DEFAULT 15.0"))
            if "usd_to_pkr_rate" not in existing_columns:
                connection.execute(text("ALTER TABLE bot_configs ADD COLUMN usd_to_pkr_rate FLOAT DEFAULT 280.0"))
            if "payfast_merchant_id" not in existing_columns:
                connection.execute(text("ALTER TABLE bot_configs ADD COLUMN payfast_merchant_id VARCHAR(120)"))
            if "payfast_secured_key" not in existing_columns:
                connection.execute(text("ALTER TABLE bot_configs ADD COLUMN payfast_secured_key VARCHAR(200)"))
            if "payfast_store_id" not in existing_columns:
                connection.execute(text("ALTER TABLE bot_configs ADD COLUMN payfast_store_id VARCHAR(80)"))
            if "payfast_base_url" not in existing_columns:
                connection.execute(text("ALTER TABLE bot_configs ADD COLUMN payfast_base_url VARCHAR(200) DEFAULT 'https://ipg2.apps.net.pk'"))
            if "payfast_tutorial_url" not in existing_columns:
                connection.execute(text("ALTER TABLE bot_configs ADD COLUMN payfast_tutorial_url VARCHAR(300)"))
            if "force_join_enabled" not in existing_columns:
                connection.execute(text("ALTER TABLE bot_configs ADD COLUMN force_join_enabled BOOLEAN DEFAULT 0"))
            if "force_join_channel" not in existing_columns:
                connection.execute(text("ALTER TABLE bot_configs ADD COLUMN force_join_channel VARCHAR(120)"))
            if "force_join_channel_url" not in existing_columns:
                connection.execute(text("ALTER TABLE bot_configs ADD COLUMN force_join_channel_url VARCHAR(500)"))
            if "force_join_group" not in existing_columns:
                connection.execute(text("ALTER TABLE bot_configs ADD COLUMN force_join_group VARCHAR(500)"))
            else:
                try:
                    connection.execute(text("ALTER TABLE bot_configs ALTER COLUMN force_join_group TYPE VARCHAR(500)"))
                except Exception:  # noqa: BLE001
                    pass
            if "force_join_group_url" not in existing_columns:
                connection.execute(text("ALTER TABLE bot_configs ADD COLUMN force_join_group_url VARCHAR(500)"))
            if "sidebar_seen_revenue_at" not in existing_columns:
                connection.execute(text("ALTER TABLE bot_configs ADD COLUMN sidebar_seen_revenue_at DATETIME"))
            if "sidebar_seen_sold_accounts_at" not in existing_columns:
                connection.execute(text("ALTER TABLE bot_configs ADD COLUMN sidebar_seen_sold_accounts_at DATETIME"))
            if "sidebar_seen_orders_at" not in existing_columns:
                connection.execute(text("ALTER TABLE bot_configs ADD COLUMN sidebar_seen_orders_at DATETIME"))
            if "sidebar_seen_users_at" not in existing_columns:
                connection.execute(text("ALTER TABLE bot_configs ADD COLUMN sidebar_seen_users_at DATETIME"))

    if "users" in table_names:
        existing_columns = {col["name"] for col in inspector.get_columns("users")}
        if "referral_join_credited" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE users ADD COLUMN referral_join_credited BOOLEAN DEFAULT 0"))

    if "referral_earnings" in table_names:
        existing_columns = {col["name"] for col in inspector.get_columns("referral_earnings")}
        if "earning_type" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE referral_earnings ADD COLUMN earning_type VARCHAR(20) DEFAULT 'per_purchase'"))

    if "categories" in table_names:
        existing_columns = {col["name"] for col in inspector.get_columns("categories")}
        with engine.begin() as connection:
            if "image_path" not in existing_columns:
                connection.execute(text("ALTER TABLE categories ADD COLUMN image_path VARCHAR(500)"))
            if "is_active" not in existing_columns:
                connection.execute(text("ALTER TABLE categories ADD COLUMN is_active BOOLEAN DEFAULT 1"))

    if "providers" in table_names:
        existing_columns = {col["name"] for col in inspector.get_columns("providers")}
        if "balance_url" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE providers ADD COLUMN balance_url VARCHAR(500)"))
        if "api_balance" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE providers ADD COLUMN api_balance FLOAT"))
        if "api_username" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE providers ADD COLUMN api_username VARCHAR(120)"))
        if "balance_synced_at" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE providers ADD COLUMN balance_synced_at DATETIME"))
        if "low_balance_alert_active" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE providers ADD COLUMN low_balance_alert_active BOOLEAN DEFAULT 0"))

    if "payment_methods" in table_names:
        existing_columns = {col["name"] for col in inspector.get_columns("payment_methods")}
        if "image_path" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE payment_methods ADD COLUMN image_path VARCHAR(500)"))

    if "payment_verifications" in table_names:
        existing_columns = {col["name"] for col in inspector.get_columns("payment_verifications")}
        if "reason" not in existing_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE payment_verifications ADD COLUMN reason TEXT"))


def seed_defaults() -> None:
    db = SessionLocal()
    try:
        env_admin_id = (os.getenv("ADMIN_ID") or os.getenv("ADMIN_TG_ID") or "").strip() or None
        if db.query(BotConfig).count() == 0:
            db.add(
                BotConfig(
                    bot_token=os.getenv("BOT_TOKEN"),
                    admin_tg_id=env_admin_id,
                    usdt_address=os.getenv("USDT_ADDRESS"),
                    usdt_network=os.getenv("USDT_NETWORK", "BEP20"),
                    auto_verify_enabled=os.getenv("AUTO_VERIFY_ENABLED", "true").lower() == "true",
                    bscscan_api_key=os.getenv("BSCSCAN_API_KEY"),
                    tronscan_api_key=os.getenv("TRONSCAN_API_KEY"),
                )
            )
        elif env_admin_id:
            # Keep DB admin id in sync with the ENV variable (source of truth).
            config = db.query(BotConfig).first()
            if config and config.admin_tg_id != env_admin_id:
                config.admin_tg_id = env_admin_id
        # Rebrand default welcome if still Aurex / SMM Reseller (any similar wording).
        from utils.helpers import DEFAULT_WELCOME, is_legacy_welcome

        config = db.query(BotConfig).first()
        if config and is_legacy_welcome(config.welcome_msg):
            config.welcome_msg = DEFAULT_WELCOME
        if db.query(Category).count() == 0:
            db.add_all(
                [
                    Category(name="CapCut", emoji="CC", sort_order=1),
                    Category(name="ChatGPT", emoji="AI", sort_order=2),
                    Category(name="Netflix", emoji="TV", sort_order=3),
                    Category(name="Social Media", emoji="SM", sort_order=4),
                ]
            )

        # Migrate the existing 4 hardcoded payment methods into the new PaymentMethod table.
        # This runs only once (guarded by count() == 0), so nothing breaks for existing bots.
        if db.query(PaymentMethod).count() == 0:
            usdt_address = os.getenv("USDT_ADDRESS", "")
            db.add_all(
                [
                    PaymentMethod(
                        name="Binance Pay",
                        code="BINANCE",
                        method_type="auto",
                        network=None,
                        address=None,
                        icon="🟡",
                        sort_order=1,
                        is_active=True,
                    ),
                    PaymentMethod(
                        name="Bybit Pay",
                        code="BYBIT",
                        method_type="auto",
                        network=None,
                        address=None,
                        icon="🟠",
                        sort_order=2,
                        is_active=True,
                    ),
                    PaymentMethod(
                        name="USDT BEP20",
                        code="BEP20",
                        method_type="auto",
                        network="BEP20",
                        address=usdt_address,
                        icon="💵",
                        sort_order=3,
                        is_active=True,
                    ),
                    PaymentMethod(
                        name="USDT TRC20",
                        code="TRC20",
                        method_type="auto",
                        network="TRC20",
                        address=usdt_address,
                        icon="💸",
                        sort_order=4,
                        is_active=True,
                    ),
                ]
            )

        # Seed the default language list shown in the /language menu.
        # Admin can add/remove/activate more from the admin panel later.
        if db.query(Language).count() == 0:
            db.add_all(
                [
                    Language(code="en", name="English", flag="🇬🇧", sort_order=1, is_active=True),
                    Language(code="vi", name="Tiếng Việt", flag="🇻🇳", sort_order=2, is_active=True),
                    Language(code="id", name="Bahasa Indonesia", flag="🇮🇩", sort_order=3, is_active=True),
                    Language(code="fa", name="فارسی", flag="🇮🇷", sort_order=4, is_active=True),
                    Language(code="ru", name="Русский", flag="🇷🇺", sort_order=5, is_active=True),
                    Language(code="hi", name="हिन्दी", flag="🇮🇳", sort_order=6, is_active=True),
                    Language(code="ko", name="한국어", flag="🇰🇷", sort_order=7, is_active=True),
                    Language(code="zh", name="中文", flag="🇨🇳", sort_order=8, is_active=True),
                ]
            )

        # Ensure menu command rows exist (Admin → Commands). Missing keys only —
        # never overwrite names/icons the admin already customized.
        from utils.menu_commands import ensure_menu_commands

        ensure_menu_commands(db)

        db.commit()
    finally:
        db.close()
