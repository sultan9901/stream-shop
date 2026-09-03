"""Request schemas for Master / Seller panel endpoints."""
from __future__ import annotations

from pydantic import BaseModel, Field


# ---------------- accounts ----------------
class MasterCreateIn(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=256)
    device_lock: bool = True
    note: str | None = Field(default=None, max_length=1000)


class SellerCreateIn(BaseModel):
    username: str = Field(min_length=3, max_length=64)
    password: str = Field(min_length=6, max_length=256)
    contact_email: str | None = Field(default=None, max_length=255)
    device_lock: bool = True
    can_verify_payments: bool = True
    note: str | None = Field(default=None, max_length=1000)


class AccountUpdateIn(BaseModel):
    is_active: bool | None = None
    device_lock: bool | None = None
    note: str | None = Field(default=None, max_length=1000)
    contact_email: str | None = Field(default=None, max_length=255)
    can_verify_payments: bool | None = None


class ResetPasswordIn(BaseModel):
    new_password: str = Field(min_length=6, max_length=256)
    force_change: bool = True


# ---------------- catalogue ----------------
class ProductIn(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    coin_price: int = Field(ge=0, le=10_000_000)
    tagline: str | None = Field(default=None, max_length=255)
    description: str | None = Field(default=None, max_length=20000)
    version: str | None = Field(default=None, max_length=40)
    platform: str | None = Field(default=None, max_length=120)
    category_id: str | None = Field(default=None, max_length=64)
    seller_id: str | None = Field(default=None, max_length=64)
    external_download_url: str | None = Field(default=None, max_length=2000)
    delivery_note: str | None = Field(default=None, max_length=4000)
    demo_video_url: str | None = Field(default=None, max_length=2000)
    is_active: bool = True
    is_featured: bool = False
    allow_repurchase: bool = False
    stock: int | None = Field(default=None, ge=0, le=1_000_000)
    display_order: int = 0


class CategoryIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    icon: str | None = Field(default=None, max_length=64)
    display_order: int = 0
    is_active: bool = True


# ---------------- coins & payments ----------------
class CoinPackageIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    coins: int = Field(ge=1, le=100_000_000)
    bonus_coins: int = Field(default=0, ge=0, le=100_000_000)
    price_bdt: float = Field(ge=0, le=10_000_000)
    badge: str | None = Field(default=None, max_length=40)
    is_active: bool = True
    display_order: int = 0


class PaymentMethodIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    account_number: str = Field(min_length=3, max_length=80)
    account_name: str | None = Field(default=None, max_length=120)
    account_type: str | None = Field(default=None, max_length=40)
    instructions: str | None = Field(default=None, max_length=4000)
    is_active: bool = True
    display_order: int = 0


# ---------------- wallet control ----------------
class WalletAdjustIn(BaseModel):
    coins: int = Field(ge=1, le=100_000_000)
    direction: str = Field(pattern="^(add|remove|bonus)$")
    reason: str = Field(min_length=3, max_length=1000)


class SettingsIn(BaseModel):
    values: dict[str, str]
