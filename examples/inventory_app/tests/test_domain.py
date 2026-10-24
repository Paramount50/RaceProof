from inventory_app.models import Product


def test_product_model_has_stock() -> None:
    assert "stock" in Product.__table__.columns
