# Changelog

All notable changes to Burnerific. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [0.2.0] - Unreleased

### Upgrade notes
- **Boiler and hot water setpoints moved.** The "Boiler setpoint" and "Hot water setpoint" number entities are gone. Use the new **Boiler** thermostat (`climate`) and **Hot water** (`water_heater`) entities instead. Automations that set the old numbers need updating.
- **"Heating season ends" is now "Next restock date".** Your setting is carried over automatically (config entry version 2). It means when you plan to buy pellets again, for example when new-season prices start.
- **Oxygen sensors are disabled on boilers without a lambda sensor**, because their readings are meaningless there. You can enable them again under the entity's settings.
- Requires Home Assistant 2026.x for the integration's own icon. Everything else works on 2025.6 and later.

### Added

**Learning**
- **Import your fill log** (`burnerific.import_fills`): paste one line per silo fill, like `04-10-2025 6`, in sacks or kg. The model learns from past winters straight away.
- **Wind and sun in the model.** Cold windy days cost more, and sunshine through the windows saves pellets. New diagnostic sensors show how much for your house.
- **Hot water split off** when the controller reports it separately, with "Hot water today" and "Hot water share" sensors.
- The controller's monthly totals and fill-log intervals are paired with the measured weather of every day in them.
- **Counter calibration**: compares the pellets you filled with what the controller counted, and can correct for the difference (option "Correct the controller's counter").

**Stock**
- **Deliveries become lots** with supplier and price. Pellets move first-in first-out through storage and silo, so Burnerific knows which delivery is burning ("Burning lot").
- **Silo level from a sensor**: a percentage sensor, or a distance sensor above the pellets with "empty" and "full" readings.
- **Order by** date: the last day to order, given your delivery time.

**Maintenance**
- **Ash drawer** fill level, from kg burned and the pellets' ash content.
- **Heat exchanger cleaning** and **burner service** by kg burned, and **chimney sweep** by date, each with a due date predicted from the burn forecast.
- "Due" binary sensors, "done" buttons, and a `burnerific.maintenance_done` service.

**Money and efficiency**
- **Cost** today, this month, this season, and expected for the next 30 days.
- **Pellet price per kWh**, for the Energy dashboard.
- **Heat price compared with a heat pump**, using your electricity price sensor and a COP.
- **Supplier quality**: how many pellets each lot needs compared with what the weather called for.
- **Weather-normalised yearly use** per heating season, to see whether insulation or a lower setpoint paid off.

**Home Assistant**
- **Boiler thermostat** (`climate`): setpoint, on/off, heating or idle. **Hot water** (`water_heater`) on boilers with a tank sensor.
- **Alarms become Repairs issues with a hint.** An ignition alarm with an empty silo says "probably out of pellets". A `burnerific_alarm` event is fired for automations.
- **Controller event log in the logbook**, for example "changed from burning to stopped by timer".
- **Pellet calendar** with silo empty, order by, run-out, restock and maintenance dates.
- **Lovelace card** (`custom:burnerific-card`): silo gauge, stock, run-out date and range, order by, a fan chart of remaining pellets, and maintenance chips.
- **Blueprints**, installed to `blueprints/automation/burnerific/`: fill-silo reminder, order pellets, and boiler alarm.
- **Re-authentication** when the boiler refuses the PIN. The PIN is now also checked during setup.
- **Discovery fallback**: when broadcast finds nothing, Burnerific asks every address on the local subnets directly.
- **StokerCloud fallback** (optional): reads the boiler through the cloud while the local network can't reach it, with "Data source" and "LAN connection" sensors.
- `host:port` addresses for boilers behind NAT or port forwarding.
- Ignition counter sensor.
- Translations: **Swedish, Norwegian, German and Polish** (drafts, corrections welcome), alongside English and Danish.
- Options are grouped into sections: boiler, weather, pellets and silo, maintenance, and money.

### Fixed
- Removing a boiler now deletes its saved stock and history.
- "Burned today" counts from midnight using the controller's figure, instead of from when Home Assistant started.
- The silo is no longer counted twice when a fill is written to the controller.
- The silo and first forecast now use the controller's silo reading straight away, instead of after the next poll.
- The card's JavaScript loads even when the frontend starts after Burnerific.

## [0.1.0] - 2026-10-19

First version.

### Added
- Our own async implementation of the NBE local UDP protocol: discovery, reads, and PIN-protected writes using the controller's RSA scheme, with no third-party requirements.
- Boiler sensors (state, temperatures, power, fans, consumption, fuel energy), binary sensors (alarm, flame, ignition, pumps), and start, stop and reset alarm buttons.
- Pellet stock: storage and silo, deliveries with average price, silo fills synced to the controller.
- Bayesian degree-day consumption model with model averaging over the heating limit. It learns from live data and the controller's own daily and monthly history.
- Seasonal climate model from six years of Open-Meteo history.
- Monte Carlo forecast of silo-empty and run-out dates (P10/P50/P90), order amount and risk of running out.
- Config flow with LAN discovery, options, reconfigure, services, diagnostics, English and Danish.
- Icon, logo and icons for every entity and service.
- Devcontainer with Home Assistant, a simulated NBE boiler for development, and tests that run inside Home Assistant.

[0.2.0]: https://github.com/dissan/burnerific/compare/23269d7...develop
[0.1.0]: https://github.com/dissan/burnerific/commits/83b7c5a
