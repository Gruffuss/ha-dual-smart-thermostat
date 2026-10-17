"""Home Assistant actuator and minute-clock glue for predictive heating."""

from datetime import timedelta

from homeassistant.components.climate import HVACMode
from homeassistant.helpers.event import async_track_time_interval
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

    def _inputs(self):
        now = dt_util.utcnow()
        if self.environment.cur_outside_temp is not None:
            self._outdoor = self.environment.cur_outside_temp
        temp = self.environment.cur_temp
        return MinuteInputs(
            now_min=(now - self._origin).total_seconds() / 60,
            temp=temp,
            outdoor=self._outdoor,
            valve_on=self.is_on,
            setpoint=getattr(self.environment, self.target_env_attr, None),
            demand=self.hvac_mode == HVACMode.HEAT,
            valid=temp is not None
            and self._outdoor is not None
            and self._interval_valid,
        )

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

    async def _async_minute(self, now):
        self._interval_valid = abs((now - self._last_tick).total_seconds() - 60) <= 3
        self._last_tick = now
        restart = self._ctl.minute(self._inputs())
        await self.async_control_hvac(force=restart)

    async def async_control_hvac(self, time=None, force=False):
        was_running = self._pwm.running
        await super().async_control_hvac(time, force)
        if was_running and self._pwm.running and force:
            inp = self._inputs()
            if inp.setpoint != self._planned_setpoint or self._ctl._restart:
                await self._pwm.async_restart()
