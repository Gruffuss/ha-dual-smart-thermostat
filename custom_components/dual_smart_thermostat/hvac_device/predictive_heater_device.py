"""Home Assistant actuator and minute-clock glue for predictive heating."""

from datetime import timedelta

from homeassistant.components.climate import HVACMode
from homeassistant.core import callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
import homeassistant.util.dt as dt_util

from ..control.predictive import MinuteInputs, PredictiveController
from ..control.tpi import TpiParams
from .pwm_heater_device import PwmHeaterDevice


class PredictiveHeaterDevice(PwmHeaterDevice):
    """Drive the real heater valve from the learned slab model or PI fallback."""

    def __init__(
        self,
        hass,
        entity_id,
        min_cycle_duration,
        initial_hvac_mode,
        environment,
        openings,
        features,
        hvac_power,
        cycle_duration,
        settings,
        store_key,
    ):
        super().__init__(
            hass,
            entity_id,
            min_cycle_duration,
            initial_hvac_mode,
            environment,
            openings,
            features,
            hvac_power,
            cycle_duration,
            TpiParams(),
        )
        self._settings = settings
        self._store_key = store_key
        self._ctl = PredictiveController(settings)
        self._origin = dt_util.utcnow()
        self._last_tick = self._origin
        self._interval_valid = False
        self._planned_setpoint = None
        self._cycle_started = self._origin
        self._planned_on_minutes = 0.0
        self._outdoor = None
        self._last_opening = None
        self._last_ac = None
        self._maintenance = False
        self._cooler_selected = False
        self._sibling_cooler = None
        self._temperature_fault = lambda: False
        self._seen_temperature = False

    def set_sibling_cooler(self, device):
        """Use the configured cooler for AC validity; never interlock its output."""
        self._sibling_cooler = device

    def set_temperature_fault_checker(self, checker):
        self._temperature_fault = checker

    def set_maintenance(self, active):
        """Exercise owns the valve, so cancel PWM without switching it."""
        self._maintenance = active
        if active:
            self._pwm.cancel()
            self._ctl.tracker.invalidate()

    def set_cooler_selected(self, selected):
        self._cooler_selected = selected

    def _ac_active(self, state):
        return state is not None and state.state in (
            "heat",
            "cool",
            "heat_cool",
            "dry",
            "fan_only",
            "auto",
            "on",
        )

    @callback
    def _validity_changed(self, event):
        now = dt_util.utcnow()
        if event.data["entity_id"] in self.openings.opening_entities:
            states = (event.data.get("old_state"), event.data.get("new_state"))
            if any(s is not None and s.state in ("on", "open") for s in states):
                self._last_opening = now
                self._ctl.tracker.invalidate()
        elif any(
            self._ac_active(event.data.get(k)) for k in ("old_state", "new_state")
        ):
            self._last_ac = now
            self._ctl.tracker.invalidate()

    def _inputs(self):
        now = dt_util.utcnow()
        if self.environment.cur_outside_temp is not None:
            self._outdoor = self.environment.cur_outside_temp
        temp = self.environment.cur_temp
        if self._temperature_fault():
            temp = None
        self._seen_temperature |= temp is not None
        if any(self.openings._is_opening_open_state(o) for o in self.openings.openings):
            self._last_opening = now
        if self._sibling_cooler and self._ac_active(self._sibling_cooler._entity_state):
            self._last_ac = now
        opening_valid = (
            self._last_opening is None
            or now - self._last_opening >= timedelta(minutes=15)
        )
        ac_valid = self._last_ac is None or now - self._last_ac >= timedelta(minutes=30)
        return MinuteInputs(
            now_min=(now - self._origin).total_seconds() / 60,
            temp=temp,
            outdoor=self._outdoor,
            valve_on=self.is_on,
            setpoint=getattr(self.environment, self.target_env_attr, None),
            demand=self.hvac_mode == HVACMode.HEAT
            and not self._maintenance
            and not self._cooler_selected
            and not self.openings.any_opening_open(self.hvac_mode)
            and not self.environment.is_floor_hot,
            valid=temp is not None
            and self._outdoor is not None
            and self._interval_valid
            and opening_valid
            and ac_valid
            and not self._maintenance,
        )

    def _can_control_temperature(self):
        self._inputs()
        return self._seen_temperature

    def _compute_duty(self):
        inp = self._inputs()
        self._planned_setpoint = inp.setpoint
        self._cycle_started = dt_util.utcnow()
        self._planned_on_minutes = self._ctl.plan(inp)
        return 100 * self._planned_on_minutes / self._settings.cycle_minutes

    async def async_on_startup(self, async_write_ha_state_cb=None):
        await super().async_on_startup(async_write_ha_state_cb)
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_minute, timedelta(minutes=1)
            )
        )
        entities = list(self.openings.opening_entities)
        if self._sibling_cooler:
            entities.append(self._sibling_cooler.entity_id)
        if entities:
            self.async_on_remove(
                async_track_state_change_event(
                    self.hass, entities, self._validity_changed
                )
            )

    async def _async_minute(self, now):
        self._interval_valid = abs((now - self._last_tick).total_seconds() - 60) <= 3
        self._last_tick = now
        restart = self._ctl.minute(self._inputs())
        await self.async_control_hvac(force=restart)

    async def async_control_hvac(self, time=None, force=False):
        self._inputs()
        if self._maintenance:
            return
        if self._cooler_selected:
            await self._pwm.async_stop()
            return
        was_running = self._pwm.running
        await super().async_control_hvac(time, force)
        if was_running and self._pwm.running and force:
            inp = self._inputs()
            if inp.setpoint != self._planned_setpoint or self._ctl._restart:
                await self._pwm.async_restart()
