from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from inventory_app.models import Order, Product

Checkpoint = Callable[[str, str], None]


def purchase_inventory(
    session: Session,
    product_id: int,
    actor_id: str,
    checkpoint: Checkpoint,
) -> bool:
    """Intentionally vulnerable read-check-write operation."""
    product = session.scalar(select(Product).where(Product.id == product_id))
    if product is None or product.stock < 1:
        return False
    checkpoint(actor_id, "after_read")
    product.stock -= 1
    session.add(Order(product_id=product_id, actor_id=actor_id))
    session.flush()
    checkpoint(actor_id, "after_write")
    return True
