from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from booking_app.models import Booking, Resource

Checkpoint = Callable[[str, str], None]


def reserve_resource(
    session: Session,
    resource_id: int,
    actor_id: str,
    checkpoint: Checkpoint,
) -> bool:
    """Intentionally vulnerable availability-check followed by insert."""
    resource = session.scalar(select(Resource).where(Resource.id == resource_id))
    if resource is None:
        return False
    existing = (
        session.scalar(
            select(func.count()).select_from(Booking).where(Booking.resource_id == resource_id)
        )
        or 0
    )
    if existing:
        return False
    checkpoint(actor_id, "after_read")
    session.add(Booking(resource_id=resource_id, actor_id=actor_id))
    session.flush()
    checkpoint(actor_id, "after_write")
    return True
