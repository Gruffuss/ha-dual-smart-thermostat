"""Unit tests for valve-maintenance scheduling helpers."""

from datetime import datetime, timedelta

from custom_components.dual_smart_thermostat.control.valve_maintenance import (
    initial_maintenance_time,
    max_jitter_hours,
    next_maintenance_time,
)

NOW = datetime(2024, 6, 1, 0, 0, 0)


def test_next_maintenance_adds_interval_and_jitter():
    assert next_maintenance_time(NOW, 7) == NOW + timedelta(days=7)
    assert next_maintenance_time(NOW, 7, jitter_hours=5) == NOW + timedelta(
        days=7, hours=5
    )


def test_next_maintenance_clamps_negative_jitter():
    assert next_maintenance_time(NOW, 7, jitter_hours=-3) == NOW + timedelta(days=7)


def test_initial_maintenance_is_within_interval():
    when = initial_maintenance_time(NOW, 7, fraction=0.5)
    assert NOW < when <= NOW + timedelta(days=7)
    # halfway through a 7 day interval
    assert when == NOW + timedelta(days=3.5)


def test_initial_maintenance_minimum_one_hour():
    # Tiny interval still waits at least an hour after startup.
    when = initial_maintenance_time(NOW, 0.01, fraction=0.5)
    assert when == NOW + timedelta(hours=1)


def test_max_jitter_hours():
    # 7% of a 7-day interval in hours
    assert max_jitter_hours(7) == 7 * 24 * 0.07
