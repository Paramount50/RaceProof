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
