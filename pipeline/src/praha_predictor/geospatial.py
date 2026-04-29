from __future__ import annotations

from math import atan2, cos, radians, sin, sqrt


def haversine_km(
    lat1: float, lng1: float, lat2: float, lng2: float
) -> float:
    radius_km = 6371.0
    d_lat = radians(lat2 - lat1)
    d_lng = radians(lng2 - lng1)
    a = (
        sin(d_lat / 2) ** 2
        + cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lng / 2) ** 2
    )
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    return radius_km * c

