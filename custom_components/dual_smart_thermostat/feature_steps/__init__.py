"""Feature-specific configuration steps for dual smart thermostat."""

from .fan import FanSteps
from .floor import FloorSteps
from .humidity import HumiditySteps
from .openings import OpeningsSteps
from .presence import PresenceSteps
from .presets import PresetsSteps

__all__ = [
    "OpeningsSteps",
    "PresenceSteps",
    "FanSteps",
    "HumiditySteps",
    "PresetsSteps",
    "FloorSteps",
]
