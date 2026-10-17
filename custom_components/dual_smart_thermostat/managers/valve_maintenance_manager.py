"""Periodic valve maintenance: exercise a switch so the valve doesn't seize.

Runs even when heating is off (the point is to cycle the valve during summer).
Each run holds an idle valve open, then closes it. Normal control is suspended for the duration via the supplied callbacks.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
import logging
from random import random

from homeassistant.const import (
    ATTR_ENTITY_ID,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import DOMAIN as HA_DOMAIN, HomeAssistant, callback
from homeassistant.helpers.event import async_track_point_in_time
import homeassistant.util.dt as dt_util

from ..control.valve_maintenance import (
    initial_maintenance_time,
    max_jitter_hours,
    next_maintenance_time,
)

_LOGGER = logging.getLogger(__name__)


class ValveMaintenanceManager:
    """Schedule and run periodic anti-stick cycles on a switch entity."""

    def __init__(
        self,
        hass: HomeAssistant,
        entity_id: str,
        interval_days: float,
        suspend_control: Callable[[bool], None],
        open_minutes: int = 6,
    ) -> None:
        self.hass = hass
        self.entity_id = entity_id
        self._interval_days = interval_days
        self._suspend_control = suspend_control
        self._open_minutes = open_minutes
        self._task: asyncio.Task | None = None
        self._cancelled = False
        self._cancel_timer: Callable[[], None] | None = None
        self._running = False

    def schedule_initial(self) -> None:
        """Schedule the first maintenance run after startup."""
        when = initial_maintenance_time(dt_util.utcnow(), self._interval_days)
        self._schedule_at(when)

    def _schedule_next(self) -> None:
        jitter = random() * max_jitter_hours(self._interval_days)
        when = next_maintenance_time(dt_util.utcnow(), self._interval_days, jitter)
        self._schedule_at(when)

    def _schedule_at(self, when) -> None:
        self._cancel_pending_timer()
        _LOGGER.debug("Scheduling valve maintenance for %s at %s", self.entity_id, when)
        self._cancel_timer = async_track_point_in_time(self.hass, self._async_run, when)

    def _cancel_pending_timer(self) -> None:
        if self._cancel_timer is not None:
            self._cancel_timer()
            self._cancel_timer = None

    async def cancel(self) -> None:
        """Cancel scheduling and finish closing any valve opened by this manager."""
        self._cancelled = True
        self._cancel_pending_timer()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _async_run(self, *_) -> None:
        try:
            await self.async_run_cycle()
        finally:
            if not self._cancelled:
                self._schedule_next()

    async def async_run_cycle(self) -> None:
        """Run one tracked exercise, ignoring overlapping service requests."""
        if self._cancelled or self._task is not None:
            return
        state = self.hass.states.get(self.entity_id)
        if state is None or state.state in (STATE_ON, STATE_UNAVAILABLE, STATE_UNKNOWN):
            _LOGGER.debug(
                "Skipping valve maintenance; %s active or unavailable", self.entity_id
            )
            return
        self._task = asyncio.create_task(self._exercise())
        try:
            await self._task
        except asyncio.CancelledError:
            if not self._cancelled:
                raise
        finally:
            self._task = None

    async def _exercise(self) -> None:
        self._running = True
        self._suspend_control(True)
        cancel_hold = None
        try:
            await self._switch(SERVICE_TURN_ON)
            done = self.hass.loop.create_future()

            @callback
            def finish(_):
                if not done.done():
                    done.set_result(None)

            cancel_hold = async_track_point_in_time(
                self.hass,
                finish,
                dt_util.utcnow() + timedelta(minutes=self._open_minutes),
            )
            await done
        finally:
            if cancel_hold is not None:
                cancel_hold()
            try:
                await self._switch(SERVICE_TURN_OFF)
            finally:
                self._running = False
                self._suspend_control(False)
                _LOGGER.info("Valve maintenance finished for %s", self.entity_id)

    @property
    def running(self) -> bool:
        return self._running

    async def _switch(self, service: str) -> None:
        try:
            await self.hass.services.async_call(
                HA_DOMAIN,
                service,
                {ATTR_ENTITY_ID: self.entity_id},
                blocking=True,
            )
        except Exception as e:  # noqa: BLE001
            _LOGGER.error(
                "Valve maintenance %s failed for %s: %s", service, self.entity_id, e
            )

    @property
    def interval(self) -> timedelta:
        return timedelta(days=self._interval_days)
