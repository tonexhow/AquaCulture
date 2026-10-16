"""Scanner package for AquaCulture fish recommendation and size detection."""

from .fish_recommendation import (
    cancel_recommendation,
    get_recommendation_status,
    recommend_from_values,
    scan_water_recommendation,
)

from .size_detection import (
    cancel_prediction,
    get_prediction_status,
    start_prediction,
)

__all__ = [
    "cancel_recommendation",
    "get_recommendation_status",
    "recommend_from_values",
    "scan_water_recommendation",
    "cancel_prediction",
    "get_prediction_status",
    "start_prediction",
]
