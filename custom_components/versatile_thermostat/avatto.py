"""Room-temperature policy for Avatto TRVs controlled through their setpoint."""

import math

START_GAP = 0.3
MAX_BOOST = 0.5


def room_demand(current: float | None, target: float | None, was_heating: bool) -> bool | None:
    """Apply hysteresis, or defer to normal regulation if either reading is missing."""
    if current is None or target is None or not math.isfinite(current) or not math.isfinite(target):
        return None
    gap = round(target - current, 3)
    return gap > 0 if was_heating else gap >= START_GAP


def heating_setpoint(regulated: float, target: float, step: float) -> float:
    """Bound the boost without letting quantization raise it above the limit."""
    ceiling = math.floor((target + MAX_BOOST + 1e-9) / step) * step
    return round(min(regulated, ceiling), 6)
