"""Home Assistant actuator and minute-clock glue for predictive heating."""

from datetime import timedelta
import json
import logging
import math
from pathlib import Path
import time

from homeassistant.components.climate import HVACMode
from homeassistant.const import UnitOfTemperature
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
)
from homeassistant.helpers.storage import Store
import homeassistant.util.dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from ..const import DOMAIN, SET_PREDICTIVE_SENSOR_SIGNAL
from ..control.predictive import MinuteInputs, PredictiveController
from ..control.tpi import TpiParams
from .pwm_heater_device import PwmHeaterDevice

_LOGGER = logging.getLogger(__name__)


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
        self._sensor_key = store_key
        self._ctl = PredictiveController(settings)
        self._origin = dt_util.utcnow()
        self._clock_origin = time.monotonic()
        self._last_tick = self._origin
        self._interval_valid = False
        self._planned_setpoint = None
        self._cycle_started_min = 0.0
        self._planned_on_minutes = 0.0
        self._outdoor = None
        self._last_opening = None
        self._last_ac = None
        self._maintenance = False
        self._cooler_selected = False
        self._sibling_cooler = None
        self._temperature_fault = lambda: False
        self._seen_temperature = False
        self._last_publish_error = None
        self._store = Store(hass, 1, f"{DOMAIN}.predictive.{store_key}")
        self._save_scheduled = False
        self._removed = False
        self._shutdown_task = None
        self.async_on_remove(self._remove)

    @property
    def predictive_mode(self):
        return self._ctl.mode

    @PwmHeaterDevice.hvac_mode.setter
    def hvac_mode(self, hvac_mode):
        """Cancel independent heating timers as soon as demand is deselected."""
        self._hvac_mode = hvac_mode
        if hvac_mode != HVACMode.HEAT and hasattr(self, "_pwm"):
            self._pwm.cancel()

    def set_sibling_cooler(self, device):
        """Use the configured cooler for AC validity; never interlock its output."""
        self._sibling_cooler = device

    def set_sensor_key(self, sensor_key):
        """Keep entity identity separate from the path-safe storage key."""
        self._sensor_key = sensor_key

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
            now_min=(time.monotonic() - self._clock_origin) / 60,
            temp=self._celsius(temp),
            outdoor=self._celsius(self._outdoor),
            valve_on=self.is_active,
            setpoint=self._celsius(
                getattr(self.environment, self.target_env_attr, None)
            ),
            demand=self.hvac_mode == HVACMode.HEAT
            and not self.environment.humidity_sensor_stalled
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

    def _celsius(self, value):
        """The learned coefficients and fault thresholds use Celsius."""
        if value is None:
            return None
        return TemperatureConverter.convert(
            value, self.hass.config.units.temperature_unit, UnitOfTemperature.CELSIUS
        )

    def _can_control_temperature(self):
        self._inputs()
        return self._seen_temperature

    def _compute_duty(self):
        inp = self._inputs()
        self._planned_setpoint = inp.setpoint
        self._cycle_started_min = inp.now_min
        mode = self._ctl.mode
        self._planned_on_minutes = self._ctl.plan(inp)
        if mode != self._ctl.mode:
            # PWM boundaries also call plan without going through the minute
            # callback. Persist a gate change there immediately as well.
            self.hass.async_create_task(self.async_save())
        return 100 * self._planned_on_minutes / self._settings.cycle_minutes

    async def async_on_startup(self, async_write_ha_state_cb=None):
        if self._removed:
            return
        try:
            data = await self._store.async_load()
        except Exception as exc:
            _LOGGER.warning("Cannot load predictive state; starting fresh: %s", exc)
            data = None
        if self._removed:
            return
        self._origin = self._last_tick = dt_util.utcnow()
        self._clock_origin = time.monotonic()
        if data is not None:
            try:
                downtime = self._downtime(data.get("saved_at"))
                self._ctl = PredictiveController.from_dict(
                    self._settings, data, downtime
                )
            except (AttributeError, TypeError, ValueError, OverflowError) as exc:
                _LOGGER.warning(
                    "Invalid saved predictive state; starting fresh: %s", exc
                )
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
        if self._removed:
            return
        self._interval_valid = abs((now - self._last_tick).total_seconds() - 60) <= 3
        self._last_tick = now
        mode = self._ctl.mode
        restart = self._ctl.minute(self._inputs())
        await self.async_control_hvac(force=restart)
        if mode != self._ctl.mode:
            await self.async_save()
        elif not self._save_scheduled:
            self._save_scheduled = True
            self._store.async_delay_save(self._delayed_state, 300)
        self._publish()

    def _downtime(self, saved_at):
        saved = dt_util.parse_datetime(saved_at)
        if saved is None or saved.tzinfo is None:
            raise ValueError("saved timestamp must be aware")
        return max(0, (dt_util.utcnow() - saved).total_seconds() / 60)

    @callback
    def _delayed_state(self):
        self._save_scheduled = False
        return self._ctl.to_dict()

    async def async_save(self):
        self._save_scheduled = False
        await self._store.async_save(self._ctl.to_dict())

    @callback
    def _remove(self):
        if not self._removed:
            self._removed = True
            self._pwm.cancel()
            self._shutdown_task = self.hass.async_create_task(
                self._async_finish_remove()
            )

    async def _async_finish_remove(self):
        """Close the valve before saving, even after PWM timers were cancelled."""
        await self.async_turn_off()
        await self.async_save()

    async def async_shutdown(self):
        """Finish closing and saving before an entry can reload the Store."""
        self.call_on_remove_callbacks()
        if self._shutdown_task:
            await self._shutdown_task

    async def async_import_state(self, path, room):
        if not self.hass.config.is_allowed_path(path):
            raise HomeAssistantError("Predictive import path is not allowed")
        try:
            data = await self.hass.async_add_executor_job(self._read_import, path)
            if not isinstance(data, dict) or data.get("version") != 5:
                raise ValueError("a v5 state file is required")
            state = data["rooms"][room]
            restored = PredictiveController.from_appdaemon_room(
                self._settings, state, self._downtime(state["model_saved_at"])
            )
        except (OSError, KeyError, TypeError, ValueError, OverflowError) as exc:
            raise HomeAssistantError(f"Cannot import predictive state: {exc}") from exc
        self._ctl = restored
        self._origin = self._last_tick = dt_util.utcnow()
        self._clock_origin = time.monotonic()
        self._interval_valid = False
        await self.async_save()
        self._pwm.cancel()
        await self.async_control_hvac(force=True)
        self._publish()

    @staticmethod
    def _read_import(path):
        return json.loads(Path(path).read_text(encoding="utf-8"))

    async def async_reset_learning(self):
        self._ctl = PredictiveController(self._settings)
        self._ctl.mode = "fallback"
        self._origin = self._last_tick = dt_util.utcnow()
        self._clock_origin = time.monotonic()
        self._interval_valid = False
        self._pwm.cancel()
        await self.async_save()
        await self.async_control_hvac(force=True)
        self._publish()

    def _publish(self):
        """Publication cannot interrupt or change a valve decision."""
        now = dt_util.utcnow()
        try:
            inp = self._inputs()
            if self._pwm.running:
                on_minutes = self._planned_on_minutes
                elapsed = inp.now_min - self._cycle_started_min
            else:
                # Overrides have no running PWM cycle. Never simulate the age
                # of a stopped cycle, which could be days of unnecessary work.
                elapsed = inp.now_min % self._settings.cycle_minutes
                on_minutes = (
                    self._settings.cycle_minutes
                    if (self.environment.is_floor_cold and inp.demand)
                    else 0
                )
            forecasts = self._ctl.forecasts(
                inp,
                elapsed,
                on_minutes,
            )
            model, tracker = self._ctl.model.active(), self._ctl.tracker
            until = self._ctl.demoted_until_min
            values = {
                **{f"forecast_{key}": value for key, value in forecasts.items()},
                "mae_60": tracker.mae60,
                "mae_120": tracker.mae120,
                "persistence_mae_120": tracker.persistence_mae120,
                "mode": self._ctl.mode,
                "demoted_until": (
                    (now + timedelta(minutes=until - inp.now_min)).isoformat()
                    if until is not None
                    else None
                ),
                "alpha_rel_sd": model.alpha_rel_sd(),
                "slab": model.slab,
                "tau": model.tau,
                "planned_on_minutes": on_minutes,
                "validations_120_independent": tracker.n120_independent,
                "learning_blocks": model.blocks,
            }
            values = {
                k: (None if isinstance(v, float) and not math.isfinite(v) else v)
                for k, v in values.items()
            }
            async_dispatcher_send(
                self.hass, SET_PREDICTIVE_SENSOR_SIGNAL.format(self._sensor_key), values
            )
            if self._async_write_ha_state_cb:
                self._async_write_ha_state_cb()
        except Exception:  # Diagnostics must never affect actuator control.
            if (
                self._last_publish_error is None
                or now - self._last_publish_error >= timedelta(seconds=600)
            ):
                self._last_publish_error = now
                _LOGGER.exception(
                    "Predictive sensor publication failed for %s", self.entity_id
                )

    async def async_control_hvac(self, time=None, force=False):
        if self._removed:
            return
        self._inputs()
        if self._maintenance:
            return
        if self._cooler_selected:
            await self._pwm.async_stop()
            return
        was_running = self._pwm.running
        mode = self._ctl.mode
        await super().async_control_hvac(time, force)
        if was_running and self._pwm.running and force:
            inp = self._inputs()
            if inp.setpoint != self._planned_setpoint or self._ctl._restart:
                await self._pwm.async_restart()
        if mode != self._ctl.mode:
            await self.async_save()
