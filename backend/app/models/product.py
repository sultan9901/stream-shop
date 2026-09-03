"""Catalogue: categories, products, media, downloadable files."""
from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin, UUIDPk
from app.models.user import User


class Category(UUIDPk, TimestampMixin, Base):
    __tablename__ = "categories"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(140), unique=True, index=True, nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    icon: Mapped[str | None] = mapped_column(String(64))
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    products: Mapped[list["Product"]] = relationship(back_populates="category")


class Product(UUIDPk, TimestampMixin, Base):
    __tablename__ = "products"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(220), unique=True, index=True, nullable=False)
    tagline: Mapped[str | None] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    version: Mapped[str | None] = mapped_column(String(40))
    platform: Mapped[str | None] = mapped_column(String(120))

    # price is ALWAYS in coins; never in currency
    coin_price: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    category_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("categories.id", ondelete="SET NULL"), index=True
    )
    seller_id: Mapped[str | None] = mapped_column(
        String(32), ForeignKey("users.id", ondelete="SET NULL"), index=True
    )

    thumbnail_url: Mapped[str | None] = mapped_column(Text)
    banner_url: Mapped[str | None] = mapped_column(Text)
    demo_video_url: Mapped[str | None] = mapped_column(Text)

    # delivery payload: either a stored file, or an external link
    external_download_url: Mapped[str | None] = mapped_column(Text)
    delivery_note: Mapped[str | None] = mapped_column(Text)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_featured: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    allow_repurchase: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    stock: Mapped[int | None] = mapped_column(Integer)  # NULL = unlimited

    sold_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    view_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    category: Mapped[Category | None] = relationship(back_populates="products")
    seller: Mapped[User | None] = relationship()
    media: Mapped[list["ProductMedia"]] = relationship(
        back_populates="product", cascade="all, delete-orphan", order_by="ProductMedia.display_order"
    )
    files: Mapped[list["ProductFile"]] = relationship(
        back_populates="product", cascade="all, delete-orphan"
    )

    __table_args__ = (Index("ix_products_active_featured", "is_active", "is_featured"),)

    @property
    def primary_file(self) -> "ProductFile | None":
        for f in self.files:
            if f.is_primary:
                return f
        return self.files[0] if self.files else None

    @property
    def in_stock(self) -> bool:
        return self.stock is None or self.stock > 0


class ProductMedia(UUIDPk, TimestampMixin, Base):
    __tablename__ = "product_media"

    product_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(24), nullable=False)  # image | video | banner
    url: Mapped[str] = mapped_column(Text, nullable=False)
    caption: Mapped[str | None] = mapped_column(String(200))
    display_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    product: Mapped[Product] = relationship(back_populates="media")


class ProductFile(UUIDPk, TimestampMixin, Base):
    """The actual deliverable. Stored outside the web root and only ever served
    through a signed, expiring, per-order download token."""

    __tablename__ = "product_files"

    product_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("products.id", ondelete="CASCADE"), nullable=False, index=True
    )
    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_path: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str | None] = mapped_column(String(120))
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    checksum_sha256: Mapped[str | None] = mapped_column(String(64))
    is_primary: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    product: Mapped[Product] = relationship(back_populates="files")
