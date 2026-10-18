from __future__ import annotations

import pytest

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from custom_components.burnerific.const import DOMAIN


def state_of(hass: HomeAssistant, key: str):
    ent = er.async_get(hass)
    entity_id = ent.async_get_entity_id(_platform(key), DOMAIN, f"12345678_{key}")
    assert entity_id, f"no entity for {key}"
    return hass.states.get(entity_id)


def _platform(key: str) -> str:
    if key in {"alarm", "burning", "igniting", "boiler_pump", "order_pellets", "refill_silo"}:
        return "binary_sensor"
    if key in {"fill_amount", "storage_set", "silo_set", "boiler_setpoint"}:
        return "number"
    if key in {"fill_silo", "start", "stop", "reset_alarm"}:
        return "button"
    return "sensor"


@pytest.fixture
async def loaded(hass: HomeAssistant, entry, open_meteo):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def test_entities(hass: HomeAssistant, loaded) -> None:
    assert state_of(hass, "state").state == "power"
    assert float(state_of(hass, "boiler_temp").state) == 61.5
    assert float(state_of(hass, "power_kw").state) == 6.8
    assert state_of(hass, "burning").state == "on"
    assert state_of(hass, "alarm").state == "off"
    assert float(state_of(hass, "consumption_total").state) == 12345.6
    assert float(state_of(hass, "fuel_energy").state) == pytest.approx(12345.6 * 4.8, abs=0.1)
    # The fake has no hot water sensor (999.9), so no entity for it.
    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, "12345678_dhw_temp") is None

    assert float(state_of(hass, "storage").state) == 480
    assert float(state_of(hass, "storage_bags").state) == 30
    assert float(state_of(hass, "silo").state) == 104  # mirrored from the controller
    assert float(state_of(hass, "stock").state) == 584
    runout = state_of(hass, "runout_date")
    assert runout.state not in ("unknown", "unavailable")
    assert runout.attributes["runout_p10"] <= runout.state <= runout.attributes["runout_p90"]
    assert state_of(hass, "heating_limit").state not in ("unknown", "unavailable")
    assert state_of(hass, "boiler_setpoint").state == "65.0"
    assert state_of(hass, "start") is not None  # PIN given, so the command buttons exist


async def test_delivery_and_fill(hass: HomeAssistant, loaded, fake_boiler) -> None:
    boiler, _ = fake_boiler
    await hass.services.async_call(
        DOMAIN, "add_delivery", {"bags": 10, "total_price": 480}, blocking=True
    )
    assert float(state_of(hass, "storage").state) == 640
    assert float(state_of(hass, "price_per_kg").state) == pytest.approx(3.0)

    await hass.services.async_call(DOMAIN, "fill_silo", {"kg": 32}, blocking=True)
    await hass.async_block_till_done()
    assert boiler.writes == ["hopper.content=136"]  # 104 + 32, written to the controller
    assert float(state_of(hass, "storage").state) == 608
    assert float(state_of(hass, "silo").state) == 136

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "fill_silo", {"kg": 5000}, blocking=True)

    await hass.services.async_call(DOMAIN, "set_storage", {"kg": 100}, blocking=True)
    assert float(state_of(hass, "storage").state) == 100


async def test_fill_button_and_numbers(hass: HomeAssistant, loaded, fake_boiler) -> None:
    boiler, _ = fake_boiler
    fill_amount = state_of(hass, "fill_amount").entity_id
    await hass.services.async_call("number", "set_value", {"entity_id": fill_amount, "value": 48}, blocking=True)
    await hass.services.async_call("button", "press", {"entity_id": state_of(hass, "fill_silo").entity_id}, blocking=True)
    await hass.async_block_till_done()
    assert boiler.writes[-1] == "hopper.content=152"
    assert float(state_of(hass, "storage").state) == 432

    setpoint = state_of(hass, "boiler_setpoint").entity_id
    await hass.services.async_call("number", "set_value", {"entity_id": setpoint, "value": 70}, blocking=True)
    assert boiler.writes[-1] == "boiler.temp=70"


async def test_set_setting_and_forecast_service(hass: HomeAssistant, loaded, fake_boiler) -> None:
    boiler, _ = fake_boiler
    await hass.services.async_call(DOMAIN, "set_setting", {"key": "hot_water.temp", "value": "55"}, blocking=True)
    assert boiler.writes[-1] == "hot_water.temp=55"

    response = await hass.services.async_call(DOMAIN, "get_forecast", {}, blocking=True, return_response=True)
    assert response["stock"]["total_kg"] == 584
    assert response["climate"]["source"] == "history"
    assert response["model"]["days_observed"] >= 1  # backfilled from the controller's day array
    assert len(response["forecast"]["curve"]) == 30
    assert response["forecast"]["runout"]["p50"]


async def test_unload(hass: HomeAssistant, loaded) -> None:
    assert await hass.config_entries.async_unload(loaded.entry_id)
    await hass.async_block_till_done()
    assert loaded.state is ConfigEntryState.NOT_LOADED


async def test_boiler_offline_retries(hass: HomeAssistant, open_meteo, socket_enabled) -> None:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

    entry = MockConfigEntry(domain=DOMAIN, unique_id="1", data={"host": "127.0.0.1:9", "serial": "1"})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
