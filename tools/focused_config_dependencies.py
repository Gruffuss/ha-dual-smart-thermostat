#!/usr/bin/env python3
"""
Focused Configuration Parameter Dependencies for Dual Smart Thermostat

This script identifies and documents only the critical conditional dependencies
where configuration parameters only make sense when other parameters are set.
"""

from dataclasses import dataclass, field
from enum import Enum
import json
from pathlib import Path
import re

# Additional guide metadata maintained alongside the dependency graph.
PRESENCE_PARAMETERS = {
    "presence": {
        "required_parameter": "away",
        "description": (
            "Presence sensing switches to the away preset when nobody is "
            "present, so an away preset must be configured"
        ),
        "example": "away: {temperature: 16} → presence: [binary_sensor.occupancy]",
    },
    "presence_scope": {
        "required_parameter": "presence",
        "description": "Presence scope only applies when presence sensors are defined",
        "example": "presence: [binary_sensor.occupancy] → presence_scope: [heat]",
    },
}

HEATING_PARAMETERS = {
    "pwm_cycle_duration": {
        "required_parameter": "heater_control_mode",
        "required_value": ["tpi", "ai", "predictive"],
        "description": (
            "PWM cycle duration only applies to proportional heater control "
            "(tpi, ai or predictive)"
        ),
        "example": "heater_control_mode: tpi → pwm_cycle_duration: 1800",
    },
    "tpi_coef_int": {
        "required_parameter": "heater_control_mode",
        "required_value": ["tpi"],
        "description": (
            "TPI internal coefficient only applies when heater_control_mode is" " tpi"
        ),
        "example": "heater_control_mode: tpi → tpi_coef_int: 0.6",
    },
    "tpi_coef_ext": {
        "required_parameter": "heater_control_mode",
        "required_value": ["tpi"],
        "description": (
            "TPI external coefficient only applies when heater_control_mode is"
            " tpi (and needs outside_sensor to have effect)"
        ),
        "example": "heater_control_mode: tpi → tpi_coef_ext: 0.01",
    },
    "ai_initial_heating_power": {
        "required_parameter": "heater_control_mode",
        "required_value": ["ai"],
        "description": (
            "AI initial heating power only applies when heater_control_mode is" " ai"
        ),
        "example": "heater_control_mode: ai → ai_initial_heating_power: 0.01",
    },
    "heater_control_mode": {
        "required_parameter": "outside_sensor",
        "when_value": "predictive",
        "description": (
            "Predictive heater control requires an outdoor temperature sensor"
        ),
        "example": "heater_control_mode: predictive -> outside_sensor: sensor.outdoor",
    },
    "predictive_gate": {
        "required_parameter": "heater_control_mode",
        "required_value": ["predictive"],
        "description": "Predictive accuracy gate: auto, off or on (default auto)",
        "example": "heater_control_mode: predictive -> predictive_gate: auto",
    },
    "predictive_mae_on": {
        "required_parameter": "heater_control_mode",
        "required_value": ["predictive"],
        "description": (
            "Predictive promotion error threshold in degrees Celsius (default " "0.3)"
        ),
        "example": "heater_control_mode: predictive -> predictive_mae_on: 0.3",
    },
    "predictive_mae_off": {
        "required_parameter": "heater_control_mode",
        "required_value": ["predictive"],
        "description": (
            "Predictive demotion error threshold in degrees Celsius (default " "0.45)"
        ),
        "example": "heater_control_mode: predictive -> predictive_mae_off: 0.45",
    },
    "predictive_min_validations": {
        "required_parameter": "heater_control_mode",
        "required_value": ["predictive"],
        "description": (
            "Independent 120-minute validations needed before promotion " "(default 48)"
        ),
        "example": "heater_control_mode: predictive -> predictive_min_validations: 48",
    },
    "predictive_kp": {
        "required_parameter": "heater_control_mode",
        "required_value": ["predictive"],
        "description": "Predictive fallback PI proportional gain (default 65.0)",
        "example": "heater_control_mode: predictive -> predictive_kp: 65.0",
    },
    "predictive_ki": {
        "required_parameter": "heater_control_mode",
        "required_value": ["predictive"],
        "description": "Predictive fallback PI integral gain (default 0.05)",
        "example": "heater_control_mode: predictive -> predictive_ki: 0.05",
    },
    "predictive_initial_k_loss": {
        "required_parameter": "heater_control_mode",
        "required_value": ["predictive"],
        "description": (
            "Predictive fallback initial heat-loss feed-forward coefficient "
            "(default 2.0)"
        ),
        "example": "heater_control_mode: predictive -> predictive_initial_k_loss: 2.0",
    },
    "valve_maintenance_interval": {
        "required_parameter": "valve_maintenance",
        "required_value": [True],
        "description": (
            "Valve maintenance interval only applies when valve_maintenance is"
            " enabled"
        ),
        "example": "valve_maintenance: true → valve_maintenance_interval: 7",
    },
}

TEMPLATE_DEPENDENCIES = {
    "description": (
        "Template-based preset temperatures depend on referenced entities " "existing"
    ),
    "applies_to": [
        "away_temp",
        "away_temp_high",
        "eco_temp",
        "eco_temp_high",
        "comfort_temp",
        "comfort_temp_high",
        "home_temp",
        "home_temp_high",
        "sleep_temp",
        "sleep_temp_high",
        "activity_temp",
        "activity_temp_high",
        "boost_temp",
        "boost_temp_high",
        "anti_freeze_temp",
        "anti_freeze_temp_high",
    ],
    "dependency_type": "entity_reference",
    "examples": {
        "input_number_reference": {
            "template": "{{ states('input_number.away_temp') | float }}",
            "requires": "input_number.away_temp must exist",
            "description": "Template references input_number helper",
        },
        "sensor_reference": {
            "template": "{{ states('sensor.outdoor_temp') | float + 5 }}",
            "requires": "sensor.outdoor_temp must exist",
            "description": "Template references sensor with calculation",
        },
        "conditional_logic": {
            "template": "{{ 16 if is_state('sensor.season', 'winter') else 26 }}",
            "requires": "sensor.season must exist",
            "description": "Template uses conditional logic based on entity state",
        },
        "multiple_entities": {
            "template": (
                "{{ states('input_number.base_temp') | float + "
                "states('input_number.offset') | float }}"
            ),
            "requires": (
                "Both input_number.base_temp and input_number.offset must exist"
            ),
            "description": "Template references multiple entities",
        },
    },
    "validation": {
        "config_flow": "Templates validated for syntax before saving",
        "runtime": "Referenced entities checked during template evaluation",
        "fallback": "Uses last good value if template evaluation fails",
    },
    "notes": [
        "All entities referenced in template must exist",
        "Always use | float filter to convert states to numbers",
        "Use | float(default) to provide fallback if entity unavailable",
        "Templates automatically re-evaluate when referenced entities change",
        "Both static values and templates can be used for preset temperatures",
        "Template syntax errors are caught during config flow validation",
    ],
}

TEMPLATE_PRESET_EXAMPLE = {
    "description": "Preset temperatures using templates for dynamic adjustment",
    "required": ["Referenced entities (input_number, sensor, etc.) must exist"],
    "optional": [
        "Any preset temperature parameter can use templates",
        "Templates can reference multiple entities",
        "Conditional logic can be used",
    ],
    "example": {
        "away_temp": "{{ states('input_number.away_target') | float }}",
        "eco_temp": "{{ 16 if is_state('sensor.season', 'winter') else 26 }}",
        "home_temp": "{{ states('sensor.outdoor_temp') | float + 5 }}",
        "comfort_temp": (
            "{{ states('input_number.base_temp') | float + "
            "states('input_number.offset') | float }}"
        ),
    },
    "notes": [
        "Preset parameters can use static values OR templates",
        (
            "Both single temp mode (away_temp) and range mode (away_temp, "
            "away_temp_high) support templates"
        ),
        "Template validation occurs in config flow before saving",
        "Runtime evaluation includes fallback to last good value if template fails",
        "See template_dependencies section for detailed requirements",
    ],
}


class DependencyType(Enum):
    """Focused dependency types for configuration parameters."""

    REQUIRES = "requires"  # A requires B to function
    ENABLES = "enables"  # A enables B functionality
    CONDITIONAL = "conditional"  # A is only used if B is set
    MUTUAL_EXCLUSIVE = "mutual_exclusive"  # Only one of A or B can be used


@dataclass
class ConfigParameter:
    """Configuration parameter with focused metadata."""

    name: str
    description: str
    condition: str | None = None  # When this parameter is relevant
    enabled_by: str | None = None  # What parameter enables this
    requires: list[str] = field(default_factory=list)  # Required parameters
    conflicts_with: list[str] = field(default_factory=list)  # Conflicting parameters


@dataclass
class ConfigDependency:
    """Represents a configuration dependency relationship."""

    source: str
    target: str
    type: DependencyType
    description: str
    example: str | None = None


class FocusedConfigDependencies:
    """Critical configuration parameter dependencies only."""

    def __init__(self):
        self.parameters: dict[str, ConfigParameter] = {}
        self.dependencies: list[ConfigDependency] = []
        self._initialize_critical_dependencies()

    def _initialize_critical_dependencies(self):
        """Initialize only the critical conditional dependencies."""

        # === SECONDARY HEATING DEPENDENCIES ===
        self.dependencies.extend(
            [
                ConfigDependency(
                    source="secondary_heater",
                    target="secondary_heater_timeout",
                    type=DependencyType.ENABLES,
                    description="Secondary heater timeout only works when secondary heater is defined",
                    example="secondary_heater: switch.aux_heater → secondary_heater_timeout: '00:05:00'",
                ),
                ConfigDependency(
                    source="secondary_heater",
                    target="secondary_heater_dual_mode",
                    type=DependencyType.ENABLES,
                    description="Dual mode operation only works when secondary heater is defined",
                    example="secondary_heater: switch.aux_heater → secondary_heater_dual_mode: true",
                ),
            ]
        )

        # === FLOOR HEATING DEPENDENCIES ===
        self.dependencies.extend(
            [
                ConfigDependency(
                    source="floor_sensor",
                    target="max_floor_temp",
                    type=DependencyType.ENABLES,
                    description="Floor temperature limits only work when floor sensor is defined",
                    example="floor_sensor: sensor.floor_temp → max_floor_temp: 28",
                ),
                ConfigDependency(
                    source="floor_sensor",
                    target="min_floor_temp",
                    type=DependencyType.ENABLES,
                    description="Minimum floor temperature only works when floor sensor is defined",
                    example="floor_sensor: sensor.floor_temp → min_floor_temp: 5",
                ),
            ]
        )

        # === COOLING MODE DEPENDENCIES ===
        self.dependencies.extend(
            [
                ConfigDependency(
                    source="cooler",
                    target="ac_mode",
                    type=DependencyType.MUTUAL_EXCLUSIVE,
                    description="AC mode is ignored when separate cooler entity is defined",
                    example="If cooler: switch.ac_unit is set, ac_mode setting is ignored",
                ),
                ConfigDependency(
                    source="heat_cool_mode",
                    target="target_temp_low",
                    type=DependencyType.ENABLES,
                    description="Low temperature setting only works in heat/cool mode",
                    example="heat_cool_mode: true → target_temp_low: 18",
                ),
                ConfigDependency(
                    source="heat_cool_mode",
                    target="target_temp_high",
                    type=DependencyType.ENABLES,
                    description="High temperature setting only works in heat/cool mode",
                    example="heat_cool_mode: true → target_temp_high: 24",
                ),
            ]
        )

        # === FAN CONTROL DEPENDENCIES ===
        self.dependencies.extend(
            [
                ConfigDependency(
                    source="fan",
                    target="fan_mode",
                    type=DependencyType.ENABLES,
                    description="Fan mode only works when fan entity is defined",
                    example="fan: switch.ceiling_fan → fan_mode: true",
                ),
                ConfigDependency(
                    source="fan",
                    target="fan_on_with_ac",
                    type=DependencyType.ENABLES,
                    description="Fan with AC only works when fan entity is defined",
                    example="fan: switch.ceiling_fan → fan_on_with_ac: true",
                ),
                ConfigDependency(
                    source="fan",
                    target="fan_hot_tolerance",
                    type=DependencyType.ENABLES,
                    description="Fan temperature tolerance only works when fan entity is defined",
                    example="fan: switch.ceiling_fan → fan_hot_tolerance: 1.0",
                ),
                ConfigDependency(
                    source="fan",
                    target="fan_hot_tolerance_toggle",
                    type=DependencyType.ENABLES,
                    description="Fan tolerance toggle only works when fan entity is defined",
                    example="fan: switch.ceiling_fan → fan_hot_tolerance_toggle: input_boolean.fan_auto",
                ),
                ConfigDependency(
                    source="outside_sensor",
                    target="fan_air_outside",
                    type=DependencyType.ENABLES,
                    description="Fan air outside control only works when outside sensor is defined",
                    example="outside_sensor: sensor.outdoor_temp → fan_air_outside: true",
                ),
            ]
        )

        # === HUMIDITY CONTROL DEPENDENCIES ===
        self.dependencies.extend(
            [
                ConfigDependency(
                    source="humidity_sensor",
                    target="target_humidity",
                    type=DependencyType.ENABLES,
                    description="Target humidity only works when humidity sensor is defined",
                    example="humidity_sensor: sensor.room_humidity → target_humidity: 50",
                ),
                ConfigDependency(
                    source="humidity_sensor",
                    target="min_humidity",
                    type=DependencyType.ENABLES,
                    description="Minimum humidity only works when humidity sensor is defined",
                    example="humidity_sensor: sensor.room_humidity → min_humidity: 30",
                ),
                ConfigDependency(
                    source="humidity_sensor",
                    target="max_humidity",
                    type=DependencyType.ENABLES,
                    description="Maximum humidity only works when humidity sensor is defined",
                    example="humidity_sensor: sensor.room_humidity → max_humidity: 70",
                ),
                ConfigDependency(
                    source="dryer",
                    target="dry_tolerance",
                    type=DependencyType.ENABLES,
                    description="Dry tolerance only works when dryer entity is defined",
                    example="dryer: switch.dehumidifier → dry_tolerance: 5",
                ),
                ConfigDependency(
                    source="dryer",
                    target="moist_tolerance",
                    type=DependencyType.ENABLES,
                    description="Moist tolerance only works when dryer entity is defined",
                    example="dryer: switch.dehumidifier → moist_tolerance: 5",
                ),
            ]
        )

        # === POWER MANAGEMENT DEPENDENCIES ===
        self.dependencies.extend(
            [
                ConfigDependency(
                    source="hvac_power_levels",
                    target="hvac_power_min",
                    type=DependencyType.ENABLES,
                    description="Minimum power level only works when power levels are defined",
                    example="hvac_power_levels: 5 → hvac_power_min: 1",
                ),
                ConfigDependency(
                    source="hvac_power_levels",
                    target="hvac_power_max",
                    type=DependencyType.ENABLES,
                    description="Maximum power level only works when power levels are defined",
                    example="hvac_power_levels: 5 → hvac_power_max: 100",
                ),
                ConfigDependency(
                    source="hvac_power_levels",
                    target="hvac_power_tolerance",
                    type=DependencyType.ENABLES,
                    description="Power tolerance only works when power levels are defined",
                    example="hvac_power_levels: 5 → hvac_power_tolerance: 0.5",
                ),
            ]
        )

        # === ENTITY CONFLICTS ===
        self.dependencies.extend(
            [
                ConfigDependency(
                    source="heater",
                    target="target_sensor",
                    type=DependencyType.MUTUAL_EXCLUSIVE,
                    description="Heater and temperature sensor must be different entities",
                    example="heater: switch.heater ≠ target_sensor: sensor.temp (must be different)",
                ),
                ConfigDependency(
                    source="heater",
                    target="cooler",
                    type=DependencyType.MUTUAL_EXCLUSIVE,
                    description="Heater and cooler must be different entities when both are defined",
                    example="heater: switch.heater ≠ cooler: switch.ac (must be different)",
                ),
            ]
        )

    def get_conditional_parameters(self) -> dict[str, list[str]]:
        """Get parameters that are conditional on others."""
        conditional_map = {}

        for dep in self.dependencies:
            if dep.type in [DependencyType.ENABLES, DependencyType.CONDITIONAL]:
                if dep.source not in conditional_map:
                    conditional_map[dep.source] = []
                conditional_map[dep.source].append(dep.target)

        return conditional_map

    def get_parameter_condition(self, param_name: str) -> str | None:
        """Get the condition under which a parameter is relevant."""
        for dep in self.dependencies:
            if dep.target == param_name and dep.type in [
                DependencyType.ENABLES,
                DependencyType.CONDITIONAL,
            ]:
                return f"Only relevant when '{dep.source}' is configured"
        return None

    def generate_conditional_guide(self) -> dict:
        """Generate a guide for conditional parameters."""
        guide = {
            "conditional_parameters": {},
            "dependency_groups": {},
            "template_dependencies": TEMPLATE_DEPENDENCIES,
            "configuration_examples": {},
        }

        # Group by dependency type
        for dep in self.dependencies:
            if dep.type.value not in guide["dependency_groups"]:
                guide["dependency_groups"][dep.type.value] = []

            guide["dependency_groups"][dep.type.value].append(
                {
                    "source": dep.source,
                    "target": dep.target,
                    "description": dep.description,
                    "example": dep.example,
                }
            )

        # Create conditional parameters map
        for dep in self.dependencies:
            if dep.type in [DependencyType.ENABLES, DependencyType.CONDITIONAL]:
                guide["conditional_parameters"][dep.target] = {
                    "required_parameter": dep.source,
                    "description": dep.description,
                    "example": dep.example,
                }

        # Keep the published parameter order while including presence and PWM modes.
        parameters = guide["conditional_parameters"]
        guide["conditional_parameters"] = {
            "secondary_heater_timeout": parameters.pop("secondary_heater_timeout"),
            **PRESENCE_PARAMETERS,
            **parameters,
            **HEATING_PARAMETERS,
        }

        # Configuration examples
        guide["configuration_examples"] = {
            "floor_heating": {
                "description": "Floor heating with temperature protection",
                "required": ["floor_sensor"],
                "optional": ["max_floor_temp", "min_floor_temp"],
                "example": {
                    "floor_sensor": "sensor.floor_temperature",
                    "max_floor_temp": 28,
                    "min_floor_temp": 5,
                },
            },
            "two_stage_heating": {
                "description": "Two-stage heating with auxiliary heater",
                "required": ["secondary_heater"],
                "optional": ["secondary_heater_timeout", "secondary_heater_dual_mode"],
                "example": {
                    "secondary_heater": "switch.aux_heater",
                    "secondary_heater_timeout": "00:05:00",
                    "secondary_heater_dual_mode": True,
                },
            },
            "fan_control": {
                "description": "Fan control with advanced features",
                "required": ["fan"],
                "optional": [
                    "fan_mode",
                    "fan_on_with_ac",
                    "fan_hot_tolerance",
                    "fan_hot_tolerance_toggle",
                ],
                "example": {
                    "fan": "switch.ceiling_fan",
                    "fan_mode": True,
                    "fan_on_with_ac": True,
                    "fan_hot_tolerance": 1.0,
                },
            },
            "humidity_control": {
                "description": "Humidity control with dry mode",
                "required": ["humidity_sensor", "dryer"],
                "optional": [
                    "target_humidity",
                    "min_humidity",
                    "max_humidity",
                    "dry_tolerance",
                    "moist_tolerance",
                ],
                "example": {
                    "humidity_sensor": "sensor.room_humidity",
                    "dryer": "switch.dehumidifier",
                    "target_humidity": 50,
                    "dry_tolerance": 5,
                    "moist_tolerance": 3,
                },
            },
            "heat_cool_mode": {
                "description": "Heat/Cool mode with temperature ranges",
                "required": ["heat_cool_mode"],
                "optional": ["target_temp_low", "target_temp_high"],
                "example": {
                    "heat_cool_mode": True,
                    "target_temp_low": 18,
                    "target_temp_high": 24,
                },
            },
            "power_management": {
                "description": "HVAC power level management",
                "required": ["hvac_power_levels"],
                "optional": [
                    "hvac_power_min",
                    "hvac_power_max",
                    "hvac_power_tolerance",
                ],
                "example": {
                    "hvac_power_levels": 5,
                    "hvac_power_min": 20,
                    "hvac_power_max": 100,
                    "hvac_power_tolerance": 0.5,
                },
            },
        }

        guide["configuration_examples"][
            "template_based_presets"
        ] = TEMPLATE_PRESET_EXAMPLE
        return guide


def main():
    """Generate focused configuration dependency analysis."""
    config_deps = FocusedConfigDependencies()

    # Generate the focused guide
    guide = config_deps.generate_conditional_guide()

    # Save to JSON
    output_path = (
        Path(__file__).resolve().parents[1]
        / "tools"
        / "focused_config_dependencies.json"
    )
    with open(output_path, "w", encoding="utf-8", newline="\n") as f:
        # Keep short required-value lists inline, matching the published guide.
        output = re.sub(
            r'"required_value": (\[[^\]]*\])',
            lambda match: '"required_value": ' + json.dumps(json.loads(match[1])),
            json.dumps(guide, indent=2),
        )
        f.write(output + "\n")

    # Print analysis
    print("🎯 Focused Configuration Parameter Dependencies")
    print("=" * 55)
    print(f"Total conditional dependencies: {len(config_deps.dependencies)}")
    print()

    print("📋 Dependency Types:")
    dep_types = {}
    for dep in config_deps.dependencies:
        dep_types[dep.type.value] = dep_types.get(dep.type.value, 0) + 1

    for dep_type, count in sorted(dep_types.items()):
        print(f"  {dep_type}: {count} dependencies")
    print()

    print("🔗 Key Conditional Relationships:")
    print()

    # Group by enabling parameter
    enabling_params = {}
    for dep in config_deps.dependencies:
        if dep.type == DependencyType.ENABLES:
            if dep.source not in enabling_params:
                enabling_params[dep.source] = []
            enabling_params[dep.source].append(dep.target)

    for source, targets in enabling_params.items():
        print(f"  📌 {source}")
        for target in targets:
            print(f"     └─ enables → {target}")
        print()

    print("⚠️  Critical Conflicts:")
    for dep in config_deps.dependencies:
        if dep.type == DependencyType.MUTUAL_EXCLUSIVE:
            print(f"  ❌ {dep.source} ↔ {dep.target}: {dep.description}")
    print()

    print("📝 Configuration Examples Generated:")
    for example_name in guide["configuration_examples"]:
        example = guide["configuration_examples"][example_name]
        print(f"  • {example_name}: {example['description']}")
    print()

    print("Files generated:")
    print(
        "  📄 focused_config_dependencies.json - Conditional dependencies and examples"
    )


if __name__ == "__main__":
    main()
