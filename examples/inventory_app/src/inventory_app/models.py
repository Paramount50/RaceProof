from __future__ import annotations

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Product(Base):
    __tablename__ = "inventory_products"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sku: Mapped[str] = mapped_column(String(80), unique=True)
    stock: Mapped[int] = mapped_column(Integer, nullable=False)


class Order(Base):
    __tablename__ = "inventory_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("inventory_products.id"), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(32), nullable=False)
