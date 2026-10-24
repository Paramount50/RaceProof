from pathlib import Path

import pytest

from raceproof.inspection import inspect_python_file


@pytest.mark.parametrize(
    "relative",
    [
        "examples/inventory_app/src/inventory_app/operations.py",
        "examples/booking_app/src/booking_app/operations.py",
    ],
)
def test_inspector_flags_generic_read_check_write_shapes(relative: str) -> None:
    root = Path(__file__).resolve().parents[1]
    findings = inspect_python_file(root / relative, root)
    assert len(findings) == 1
    assert "row read" in findings[0].signals
    assert "check then act" in findings[0].signals
    assert "dependent insert" in findings[0].signals
    assert "hypothesis only" in findings[0].explanation


def test_inspector_recognizes_session_get_and_read_modify_write(tmp_path: Path) -> None:
    source = tmp_path / "operation.py"
    source.write_text(
        """def reserve(session, item_id):
    item = session.get(Item, item_id)
    if item.stock > 0:
        item.stock -= 1
        session.commit()
""",
        encoding="utf-8",
    )

    findings = inspect_python_file(source, tmp_path)

    assert len(findings) == 1
    assert findings[0].function == "reserve"
    assert "row read" in findings[0].signals
    assert "write derived from observed value" in findings[0].signals
    assert "no obvious row lock" in findings[0].signals


def test_inspector_recognizes_count_then_insert(tmp_path: Path) -> None:
    source = tmp_path / "operation.py"
    source.write_text(
        """def book(session):
    existing = session.query(Booking).count()
    if existing < 1:
        session.add(Booking())
        session.flush()
""",
        encoding="utf-8",
    )

    findings = inspect_python_file(source, tmp_path)

    assert "count then insert" in findings[0].signals
    assert "aggregate count read" in findings[0].signals
