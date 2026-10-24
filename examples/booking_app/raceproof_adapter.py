from __future__ import annotations

from booking_app.models import Base, Booking, Resource
from booking_app.operations import reserve_resource
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from raceproof.contracts import ActorSpec, InvariantResult
from raceproof.database import actor_session
from raceproof.events import EventRecorder
from raceproof.target import TargetAdapter


class BookingAdapter(TargetAdapter):
    def reset(self, engine) -> None:
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        with sessionmaker(bind=engine, expire_on_commit=False).begin() as session:
            session.add(Resource(id=1, name="Conference room"))

    def run_actor(self, engine, actor: ActorSpec, checkpoint, recorder: EventRecorder) -> str:
        checkpoint(actor.id, "before_operation")
        with actor_session(engine, actor.id) as session:
            accepted = reserve_resource(
                session,
                int(actor.input["resource_id"]),
                actor.id,
                checkpoint,
            )
        return "accepted" if accepted else "rejected"

    def evaluate_invariant(self, engine) -> InvariantResult:
        with sessionmaker(bind=engine, expire_on_commit=False)() as session:
            total = (
                session.scalar(
                    select(func.count()).select_from(Booking).where(Booking.resource_id == 1)
                )
                or 0
            )
        passed = total <= 1
        return InvariantResult(
            name=self.config.invariant.name,
            passed=passed,
            assertion="active_bookings_per_resource <= 1",
            evidence={"resource_id": 1, "active_bookings": total, "maximum_allowed": 1},
            explanation="The resource is exclusive."
            if passed
            else "The resource was booked twice.",
        )

    def schema_ddl(self) -> str:
        from sqlalchemy.dialects import postgresql
        from sqlalchemy.schema import CreateTable

        return "\n\n".join(
            str(CreateTable(table).compile(dialect=postgresql.dialect()))
            for table in Base.metadata.sorted_tables
        )


def create_adapter(repository_root, config) -> TargetAdapter:
    return BookingAdapter(repository_root, config)
