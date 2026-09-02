# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

_Nothing yet._

## [v0.15.1] - 2026-09-02

### Fixed

- **Per-mode setpoint persistence** - a target temperature captured in one HVAC mode could resurface in the other, because the saved target temperature was not mode-scoped and outranked the mode's own configured bound (upstream #641, #643)
  - Hit when turning a preset off after switching modes (heat -> preset -> cool -> preset off), and whenever anything re-applied the already-active preset (scheduler tick, UI resend, re-selecting the same preset)
  - Both call sites now resolve through a single `_bound_for_mode` helper; `saved_target_temp` remains the fallback for modes with no bound to use
- Fan speed control is re-detected when the fan entity appears after startup, and a fan mode selected while it was missing is replayed (upstream #636, #638)
- `fan_on_with_heater` now takes effect on systems that also have a cooler configured (upstream #637, #639)
- Reconfigure is no longer silently shadowed by a stale `entry.options` snapshot (upstream #631, #634)
- The reconfigure wizard now seeds from the entry's effective configuration (`{**data, **options}`) rather than `data` alone, so options-flow settings are not dropped when reconfigure clears `entry.options`
- A wrapped climate cooler that came online late regained fan/swing passthrough; `WrappedClimateDevice` gained a `redetect_capabilities` shim after it started being called through the fan-device duck type

### Changed

- pip-audit is now report-only; the security gate moved to `bandit` over `custom_components`, replacing an `--ignore-vuln` list that needed editing on every Home Assistant bump (upstream #640)

## [v0.15.0] - 2026-07-27

### Added

- **Preset Auto-Save** - Adjusting the target temperature, humidity or fan speed from the thermostat while a preset is selected now updates that preset instead of reverting the next time the preset is re-applied (re-selecting it, presence restoring it, a template re-render, or a restart)
  - Enabled by default via the new `preset_auto_save` option; set it to `false` to keep the classic revert-to-configured behavior
  - Persists the updated value back to the config entry (without reloading the integration) so the preset configuration shown in the options flow stays in sync
  - Template-based preset values are never overwritten, keeping the template as the source of truth
  - Exposed on the preset selection step of the config, reconfigure and options flows, and in the YAML platform schema

## v0.12.0 - v0.14.3

This changelog was not maintained across these versions. Release notes for
them are published on the
[GitHub Releases page](https://github.com/Gruffuss/ha-dual-smart-thermostat/releases).

## v0.11.3 - 2026-03-05

### Added

- **Native Fan Speed Control** - Control fan speeds (low, medium, high, auto) directly from the thermostat interface, similar to built-in thermostats (#517)
  - Automatic detection of fan entity capabilities (preset_mode and percentage support)
  - Fan speed control works in FAN_ONLY mode, fan_on_with_ac mode, and fan tolerance mode
  - State persistence across Home Assistant restarts
  - Support for both preset_mode (named speeds) and percentage-based control
  - Automatic percentage-to-preset mapping for optimal compatibility
  - Full backward compatibility with switch-based fans (no fan speed control)

### Changed

- Fan entities now expose speed control capabilities when supported by the underlying fan entity
- FeatureManager enhanced to detect and track fan speed capabilities

### Documentation

- Added comprehensive fan speed control architecture documentation to CLAUDE.md
- Updated README.md with fan speed control usage examples and configuration guidance
- Added detailed fan speed control design and implementation documentation

## [v0.11.2] - 2025-01-XX

### Fixed

- Fixed heater/cooler turns off prematurely ignoring tolerance when active (#518) (#521)
- Corrected logger name handling for multiple thermostat instances (#511) (#513)
- Corrected inverted tolerance logic and added comprehensive behavioral tests (#506) (#507)

## [v0.11.0] - 2024-12-XX

See [RELEASE_NOTES_v0.11.0.md](RELEASE_NOTES_v0.11.0.md) for complete release notes.

### Major Features

- Complete UI Configuration - Set up your thermostat through Home Assistant's UI with guided wizard
- Template-Based Preset Temperatures - Dynamic presets using Home Assistant templates
- Input Boolean Support for Equipment - Use input_boolean entities for all equipment controls
- Docker-Based Development Environment - Professional development workflow for contributors

[Unreleased]: https://github.com/Gruffuss/ha-dual-smart-thermostat/compare/v0.15.1...HEAD
[v0.15.1]: https://github.com/Gruffuss/ha-dual-smart-thermostat/compare/v0.15.0...v0.15.1
[v0.15.0]: https://github.com/Gruffuss/ha-dual-smart-thermostat/compare/v0.14.3...v0.15.0
[v0.11.2]: https://github.com/swingerman/ha-dual-smart-thermostat/compare/v0.11.0...v0.11.2
[v0.11.0]: https://github.com/swingerman/ha-dual-smart-thermostat/releases/tag/v0.11.0
