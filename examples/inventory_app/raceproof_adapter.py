from __future__ import annotations

from inventory_app.models import Base, Order, Product
from inventory_app.operations import purchase_inventory
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from raceproof.contracts import ActorSpec, InvariantResult
from raceproof.database import actor_session
from raceproof.events import EventRecorder
from raceproof.target import TargetAdapter


class InventoryAdapter(TargetAdapter):
    def reset(self, engine) -> None:
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        with sessionmaker(bind=engine, expire_on_commit=False).begin() as session:
            session.add(Product(id=1, sku="DEMO-1", stock=1))

    def run_actor(self, engine, actor: ActorSpec, checkpoint, recorder: EventRecorder) -> str:
        checkpoint(actor.id, "before_operation")
        with actor_session(engine, actor.id) as session:
            accepted = purchase_inventory(
                session,
                int(actor.input["product_id"]),
                actor.id,
                checkpoint,
            )
        return "accepted" if accepted else "rejected"

    def evaluate_invariant(self, engine) -> InvariantResult:
        with sessionmaker(bind=engine, expire_on_commit=False)() as session:
            product = session.get(Product, 1)
            orders = session.scalar(select(func.count()).select_from(Order)) or 0
        stock = product.stock if product else -1
        passed = stock >= 0 and stock + orders == 1
        return InvariantResult(
            name=self.config.invariant.name,
            passed=passed,
            assertion="final_stock >= 0 and final_stock + committed_orders == initial_stock",
            evidence={"initial_stock": 1, "final_stock": stock, "committed_orders": orders},
            explanation="Inventory is conserved."
            if passed
            else "Committed orders exceed available stock.",
        )

    def schema_ddl(self) -> str:
        from sqlalchemy.dialects import postgresql
        from sqlalchemy.schema import CreateTable

        return "\n\n".join(
            str(CreateTable(table).compile(dialect=postgresql.dialect()))
            for table in Base.metadata.sorted_tables
        )


def create_adapter(repository_root, config) -> TargetAdapter:
    return InventoryAdapter(repository_root, config)
