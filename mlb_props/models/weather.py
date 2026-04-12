# models/weather.py — Weather dataclass. Pure data, no logic.

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Weather:
    """Snapshot of weather conditions at a stadium at game time."""

    stadium: str
    temp_f: float
    wind_speed_mph: float
    wind_direction_deg: float
    condition_code: int
    fetched_at: datetime = field(default_factory=datetime.utcnow)
    is_dome: bool = False
    condition_text: str = "Unknown"   # e.g. "Sunny", "Partly Cloudy", "Rain"
    precipitation_mm: float = 0.0    # mm of precipitation
    cloud_cover_pct: int = 0         # 0-100%
