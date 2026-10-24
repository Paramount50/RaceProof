from booking_app.models import Booking


def test_booking_points_to_resource() -> None:
    assert "resource_id" in Booking.__table__.columns
