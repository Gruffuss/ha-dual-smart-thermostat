from datetime import timedelta
import logging

from homeassistant.components.climate import DOMAIN as CLIMATE_DOMAIN
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType

from ..const import (
    CONF_AUX_HEATER,
    CONF_AUX_HEATING_DUAL_MODE,
    CONF_AUX_HEATING_TIMEOUT,
    CONF_COOLER,
    CONF_DRYER,
    CONF_FAN,
    CONF_FAN_ON_WITH_AC,
    CONF_HEAT_PUMP_COOLING,
    CONF_HEATER,
    CONF_HEATER_CONTROL_MODE,
    CONF_INITIAL_HVAC_MODE,
    CONF_MIN_DUR,
    CONF_PWM_CYCLE_DURATION,
    CONF_TPI_COEF_EXT,
    CONF_TPI_COEF_INT,
    DEFAULT_PWM_CYCLE_DURATION,
    HeaterControlMode,
)
from ..control.tpi import DEFAULT_COEF_EXT, DEFAULT_COEF_INT, TpiParams
from ..hvac_device.controllable_hvac_device import ControlableHVACDevice
from ..hvac_device.cooler_device import CoolerDevice
from ..hvac_device.cooler_fan_device import CoolerFanDevice
from ..hvac_device.dryer_device import DryerDevice
from ..hvac_device.fan_device import FanDevice
from ..hvac_device.heat_pump_device import HeatPumpDevice
from ..hvac_device.heater_aux_heater_device import HeaterAUXHeaterDevice
from ..hvac_device.heater_cooler_device import HeaterCoolerDevice
from ..hvac_device.heater_device import HeaterDevice
from ..hvac_device.multi_hvac_device import MultiHvacDevice
from ..hvac_device.pwm_heater_device import PwmHeaterDevice
from ..hvac_device.wrapped_climate_device import WrappedClimateDevice
from ..managers.environment_manager import EnvironmentManager
from ..managers.feature_manager import FeatureManager
from ..managers.hvac_power_manager import HvacPowerManager
from ..managers.opening_manager import OpeningManager

_LOGGER = logging.getLogger(__name__)


class HVACDeviceFactory:

    def __init__(
        self, hass: HomeAssistant, config: ConfigType, features: FeatureManager
    ) -> None:

        self.hass = hass
        self._features = features

        self._heater_entity_id = config[CONF_HEATER]
        self._cooler_entity_id = None
        if cooler_entity_id := config.get(CONF_COOLER):
            if cooler_entity_id == self._heater_entity_id:
                _LOGGER.warning(
                    "'cooler' entity cannot be equal to 'heater' entity. "
                    "'cooler' entity will be ignored"
                )
                self._cooler_entity_id = None
            else:
                self._cooler_entity_id = cooler_entity_id

        self._fan_entity_id = config.get(CONF_FAN)
        self._fan_on_with_cooler = config.get(CONF_FAN_ON_WITH_AC)

        self._dryer_entity_id = config.get(CONF_DRYER)
        self._heat_pump_cooling_entity_id = config.get(CONF_HEAT_PUMP_COOLING)

        self._aux_heater_entity_id = config.get(CONF_AUX_HEATER)
        self._aux_heater_dual_mode = config.get(CONF_AUX_HEATING_DUAL_MODE)
        self._aux_heater_timeout = config.get(CONF_AUX_HEATING_TIMEOUT)

        self._min_cycle_duration: timedelta = config.get(CONF_MIN_DUR)

        self._initial_hvac_mode = config.get(CONF_INITIAL_HVAC_MODE)

        # Proportional heating control (PWM/TPI) over the heater switch.
        self._heater_control_mode = config.get(
            CONF_HEATER_CONTROL_MODE, HeaterControlMode.BANG_BANG
        )
        self._pwm_cycle_duration = timedelta(
            seconds=config.get(CONF_PWM_CYCLE_DURATION, DEFAULT_PWM_CYCLE_DURATION)
        )
        self._tpi_params = TpiParams(
            coef_int=config.get(CONF_TPI_COEF_INT, DEFAULT_COEF_INT),
            coef_ext=config.get(CONF_TPI_COEF_EXT, DEFAULT_COEF_EXT),
        )

    def create_device(
        self,
        environment: EnvironmentManager,
        openings: OpeningManager,
        hvac_power: HvacPowerManager,
    ) -> ControlableHVACDevice:

        dryer_device = None
        fan_device = None
        cooler_device = None
        heater_device = None
        aux_heater_device = None

        if self._features.is_configured_for_dryer_mode:
            dryer_device = DryerDevice(
                self.hass,
                self._dryer_entity_id,
                self._min_cycle_duration,
                self._initial_hvac_mode,
                environment,
                openings,
                self._features,
                hvac_power,
            )

        if self._features.is_configured_for_fan_only_mode:
            fan_device = FanDevice(
                self.hass,
                self._heater_entity_id,
                self._min_cycle_duration,
                self._initial_hvac_mode,
                environment,
                openings,
                self._features,
                hvac_power,
            )

        if self._features.is_configured_for_fan_mode:
            fan_device = FanDevice(
                self.hass,
                self._fan_entity_id,
                self._min_cycle_duration,
                self._initial_hvac_mode,
                environment,
                openings,
                self._features,
                hvac_power,
            )

        if self._features.is_configured_for_aux_heating_mode:
            aux_heater_device = HeaterDevice(
                self.hass,
                self._aux_heater_entity_id,
                self._min_cycle_duration,
                self._initial_hvac_mode,
                environment,
                openings,
                self._features,
                hvac_power,
            )

        if self._features.is_configured_for_dual_mode:
            cooler_entity_id = self._cooler_entity_id
        else:
            cooler_entity_id = self._heater_entity_id

        if (
            self._features.is_configured_for_cooler_mode
            or self._cooler_entity_id is not None
        ):
            cooler_device = self._create_cooler_device(
                environment, openings, hvac_power, cooler_entity_id, fan_device
            )

        if (
            fan_device
            and environment.fan_hot_tolerance is not None
            and cooler_device is None
        ):
            _LOGGER.warning(
                "'fan_hot_tolerance' is configured but no cooler device exists. "
                "The fan_hot_tolerance feature only works with a cooler entity "
                "or ac_mode enabled. The fan will not be used for cooling"
            )

        if self._features.is_configured_for_heat_pump_mode:
            heater_device = HeatPumpDevice(
                self.hass,
                self._heater_entity_id,
                self._min_cycle_duration,
                self._initial_hvac_mode,
                environment,
                openings,
                self._features,
                hvac_power,
            )

        if (
            self._heater_entity_id
            and not self._features.is_configured_for_cooler_mode
            and not self._features.is_configured_for_fan_only_mode
            and not self._features.is_configured_for_heat_pump_mode
        ):
            """Create a heater device if no other specific device is configured"""
            heater_device = self._create_heater_device(
                environment, openings, hvac_power
            )

        if aux_heater_device and heater_device:
            _LOGGER.info("Creating heater aux heater device")
            heater_device = HeaterAUXHeaterDevice(
                self.hass,
                [heater_device, aux_heater_device],
                self._initial_hvac_mode,
                environment,
                openings,
                self._features,
            )

        _LOGGER.debug(
            "heater_device: %s, cooler_device: %s", heater_device, cooler_device
        )

        # Set fan device on feature manager for speed control access
        # This must be done before returning any device
        if fan_device:
            self._features.set_fan_device(fan_device)

        # A wrapped climate cooler (e.g. a split AC) exposes its own fan speed
        # and swing; surface those through the feature manager so the thermostat
        # can pass them through. Register it unconditionally: capability is read
        # dynamically from the device, so it reflects the wrapped entity even if
        # the entity was unavailable when the device was built (the feature
        # manager then reports no fan/swing until the entity comes online). Only
        # use it as the fan device when there is no dedicated fan entity.
        if isinstance(cooler_device, WrappedClimateDevice):
            if fan_device is None:
                self._features.set_fan_device(cooler_device)
            self._features.set_swing_device(cooler_device)

        if heater_device is not None and cooler_device is not None:
            _LOGGER.info("Creating heater cooler device")
            heater_cooler_device = HeaterCoolerDevice(
                self.hass,
                [heater_device, cooler_device],
                self._initial_hvac_mode,
                environment,
                openings,
                self._features,
            )

            if dryer_device:
                return MultiHvacDevice(
                    self.hass,
                    [heater_cooler_device, dryer_device],
                    self._initial_hvac_mode,
                    environment,
                    openings,
                    self._features,
                )
            else:
                return heater_cooler_device

        if heater_device:
            sub_devices = [heater_device]
            if fan_device:
                sub_devices.append(fan_device)
            if dryer_device:
                sub_devices.append(dryer_device)
            if len(sub_devices) > 1:
                return MultiHvacDevice(
                    self.hass,
                    sub_devices,
                    self._initial_hvac_mode,
                    environment,
                    openings,
                    self._features,
                )
            return heater_device

        if cooler_device:
            sub_devices = [cooler_device]
            if dryer_device:
                sub_devices.append(dryer_device)
            if len(sub_devices) > 1:
                return MultiHvacDevice(
                    self.hass,
                    sub_devices,
                    self._initial_hvac_mode,
                    environment,
                    openings,
                    self._features,
                )
            return cooler_device

        if fan_device:
            return fan_device

    def _create_cooler_device(
        self,
        environment: EnvironmentManager,
        openings: OpeningManager,
        hvac_power: HvacPowerManager,
        cooler_entitiy_id: str,
        fan_device: FanDevice | None,
    ) -> CoolerDevice:

        # When the cooler is a real `climate` entity (e.g. a split AC) delegate
        # control to it instead of toggling a switch. The wrapped entity already
        # exposes its own fan/swing, so it is not combined with a separate fan
        # device.
        if self._is_climate_entity(cooler_entitiy_id):
            _LOGGER.info(
                "Creating wrapped climate cooler device for %s", cooler_entitiy_id
            )
            return WrappedClimateDevice(
                self.hass,
                cooler_entitiy_id,
                self._min_cycle_duration,
                self._initial_hvac_mode,
                environment,
                openings,
                self._features,
                hvac_power,
            )

        cooler_device = CoolerDevice(
            self.hass,
            cooler_entitiy_id,
            self._min_cycle_duration,
            self._initial_hvac_mode,
            environment,
            openings,
            self._features,
            hvac_power,
        )

        if fan_device:
            cooler_device = CoolerFanDevice(
                self.hass,
                [cooler_device, fan_device],
                self._initial_hvac_mode,
                environment,
                openings,
                self._features,
            )

        return cooler_device

    @staticmethod
    def _is_climate_entity(entity_id: str | None) -> bool:
        """Return True if the entity_id belongs to the climate domain."""
        return bool(entity_id) and entity_id.split(".")[0] == CLIMATE_DOMAIN

    def _create_heater_device(
        self,
        environment: EnvironmentManager,
        openings: OpeningManager,
        hvac_power: HvacPowerManager,
    ) -> HeaterDevice:
        """Create a bang-bang or PWM/TPI heater device per the control mode."""
        if self._heater_control_mode == HeaterControlMode.TPI:
            _LOGGER.info(
                "Creating TPI (PWM) heater device for %s", self._heater_entity_id
            )
            return PwmHeaterDevice(
                self.hass,
                self._heater_entity_id,
                self._min_cycle_duration,
                self._initial_hvac_mode,
                environment,
                openings,
                self._features,
                hvac_power,
                self._pwm_cycle_duration,
                self._tpi_params,
            )

        return HeaterDevice(
            self.hass,
            self._heater_entity_id,
            self._min_cycle_duration,
            self._initial_hvac_mode,
            environment,
            openings,
            self._features,
            hvac_power,
        )
