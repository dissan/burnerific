from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from homeassistant.components.calendar import DOMAIN as CALENDAR_DOMAIN
from homeassistant.components.climate import HVACAction, HVACMode
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.burnerific.const import DOMAIN, EVENT_ALARM, EVENT_CONTROLLER
from tests.test_features import WINTER_LOG

from .conftest import PIN

PLATFORMS = {
    "binary_sensor": {"alarm", "burning", "igniting", "boiler_pump", "order_pellets", "refill_silo", "lan_connection",
                      "ash_due_now", "clean_due_now", "sweep_due_now", "service_due_now"},
    "number": {"fill_amount", "storage_set", "silo_set"},
    "button": {"fill_silo", "start", "stop", "reset_alarm", "ash_done", "clean_done", "sweep_done", "service_done"},
    "climate": {"boiler"},
    "water_heater": {"hot_water"},
    "calendar": {"calendar"},
}


def eid(hass: HomeAssistant, key: str) -> str | None:
    platform = next((p for p, keys in PLATFORMS.items() if key in keys), "sensor")
    return er.async_get(hass).async_get_entity_id(platform, DOMAIN, f"12345678_{key}")


def state_of(hass: HomeAssistant, key: str):
    entity_id = eid(hass, key)
    assert entity_id, f"no entity for {key}"
    return hass.states.get(entity_id)


async def _refresh(hass: HomeAssistant, entry, slow: bool = True) -> None:
    rt = entry.runtime_data
    if slow:
        rt.boiler.reader.invalidate()
    await rt.boiler.async_refresh()
    await rt.pellets.async_refresh()
    await hass.async_block_till_done()


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
    assert float(state_of(hass, "ignitions").state) == 2281
    assert state_of(hass, "data_source").state == "local"
    assert state_of(hass, "lan_connection").state == "on"
    # No hot water tank and no lambda sensor on this boiler.
    assert eid(hass, "dhw_temp") is None and eid(hass, "hot_water") is None
    reg = er.async_get(hass)
    assert reg.async_get(eid(hass, "oxygen")).disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert hass.states.get(eid(hass, "oxygen")) is None

    assert float(state_of(hass, "storage").state) == 480
    assert float(state_of(hass, "storage_bags").state) == 30
    assert float(state_of(hass, "silo").state) == 104  # mirrored from the controller
    assert float(state_of(hass, "stock").state) == 584
    runout = state_of(hass, "runout_date")
    assert runout.attributes["runout_p10"] <= runout.state <= runout.attributes["runout_p90"]
    assert runout.attributes["bands"][0]["p50"] == pytest.approx(584, abs=15)
    assert state_of(hass, "order_by").state <= runout.attributes["runout_p10"]
    assert state_of(hass, "restock_date").state not in ("unknown", "unavailable")
    assert state_of(hass, "heating_limit").state not in ("unknown", "unavailable")
    assert state_of(hass, "wind_effect").state not in ("unknown", "unavailable")
    assert state_of(hass, "start") is not None  # PIN given, so the command buttons exist

    boiler = state_of(hass, "boiler")
    assert boiler.state == HVACMode.HEAT
    assert boiler.attributes["hvac_action"] == HVACAction.HEATING
    assert boiler.attributes["temperature"] == 65
    assert boiler.attributes["current_temperature"] == 61.5


async def test_maintenance(hass: HomeAssistant, loaded) -> None:
    ash = state_of(hass, "ash_level")
    assert float(ash.state) == 0  # counted from when Burnerific started
    assert ash.attributes["kg_left"] == pytest.approx(4.0 / 0.007, abs=1)
    assert state_of(hass, "sweep_due").state == "unknown"  # needs a first "done"
    await hass.services.async_call("button", "press", {"entity_id": eid(hass, "sweep_done")}, blocking=True)
    await hass.async_block_till_done()
    assert state_of(hass, "sweep_due").state not in ("unknown", "unavailable")
    assert state_of(hass, "sweep_due_now").state == "off"
    await hass.services.async_call(DOMAIN, "maintenance_done", {"task": "service", "date": "2025-01-01"}, blocking=True)
    await hass.async_block_till_done()
    assert state_of(hass, "service_due_now").state == "on"


@pytest.mark.parametrize("profile", ["v13"], indirect=True)
async def test_v13_with_lambda_and_hot_water(hass: HomeAssistant, loaded, fake_boiler) -> None:
    boiler, _ = fake_boiler
    assert float(state_of(hass, "oxygen").state) == 8.4  # lambda fitted: enabled
    tank = state_of(hass, "hot_water")
    assert tank.attributes["current_temperature"] == 48.5
    assert tank.attributes["temperature"] == 50
    await hass.services.async_call(
        "water_heater", "set_temperature", {"entity_id": tank.entity_id, "temperature": 55}, blocking=True
    )
    assert boiler.writes[-1] == "hot_water.temp=55"
    assert float(state_of(hass, "hot_water_today").state) == 1.5
    assert float(state_of(hass, "hot_water_share").state) == pytest.approx(15, abs=1)


async def test_climate_controls(hass: HomeAssistant, loaded, fake_boiler) -> None:
    boiler, _ = fake_boiler
    climate = eid(hass, "boiler")
    await hass.services.async_call("climate", "set_temperature", {"entity_id": climate, "temperature": 70}, blocking=True)
    assert boiler.writes[-1] == "boiler.temp=70"
    await hass.services.async_call("climate", "set_hvac_mode", {"entity_id": climate, "hvac_mode": "off"}, blocking=True)
    assert boiler.writes[-1] == "misc.stop=1"


async def test_delivery_fill_and_lots(hass: HomeAssistant, loaded, fake_boiler) -> None:
    boiler, _ = fake_boiler
    await hass.services.async_call(
        DOMAIN, "add_delivery", {"bags": 10, "total_price": 480, "supplier": "Pillefabrikken"}, blocking=True
    )
    assert float(state_of(hass, "storage").state) == 640
    assert float(state_of(hass, "price_per_kg").state) == pytest.approx(3.0)

    await hass.services.async_call(DOMAIN, "fill_silo", {"kg": 32}, blocking=True)
    await hass.async_block_till_done()
    assert boiler.writes == ["hopper.content=136"]  # 104 + 32, written to the controller
    assert float(state_of(hass, "storage").state) == 608
    assert float(state_of(hass, "silo").state) == 136
    assert float(state_of(hass, "cost_today").state) >= 0
    assert state_of(hass, "fuel_price_kwh").state == "0.625"

    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "fill_silo", {"kg": 5000}, blocking=True)

    await hass.services.async_call(DOMAIN, "set_storage", {"kg": 100}, blocking=True)
    assert float(state_of(hass, "storage").state) == 100


async def test_fill_button(hass: HomeAssistant, loaded, fake_boiler) -> None:
    boiler, _ = fake_boiler
    fill_amount = eid(hass, "fill_amount")
    await hass.services.async_call("number", "set_value", {"entity_id": fill_amount, "value": 48}, blocking=True)
    await hass.services.async_call("button", "press", {"entity_id": eid(hass, "fill_silo")}, blocking=True)
    await hass.async_block_till_done()
    assert boiler.writes[-1] == "hopper.content=152"
    assert float(state_of(hass, "storage").state) == 432


async def test_import_fills(hass: HomeAssistant, loaded) -> None:
    result = await hass.services.async_call(
        DOMAIN, "import_fills", {"log": WINTER_LOG, "unit": "bags"}, blocking=True, return_response=True
    )
    assert result["fills"] == 44 and result["total_kg"] == pytest.approx(317 * 16)
    response = await hass.services.async_call(DOMAIN, "get_forecast", {}, blocking=True, return_response=True)
    assert response["model"]["periods_observed"] >= 5
    assert any(s["season"] == "2025/26" for s in response["seasons"])
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "import_fills", {"log": "yesterday 5"}, blocking=True)


async def test_forecast_service(hass: HomeAssistant, loaded, fake_boiler) -> None:
    boiler, _ = fake_boiler
    await hass.services.async_call(DOMAIN, "set_setting", {"key": "hot_water.temp", "value": "55"}, blocking=True)
    assert boiler.writes[-1] == "hot_water.temp=55"
    response = await hass.services.async_call(DOMAIN, "get_forecast", {}, blocking=True, return_response=True)
    assert response["stock"]["total_kg"] == 584
    assert response["climate"]["source"] == "history"
    assert response["model"]["days_observed"] >= 1  # backfilled from the controller's day array
    assert len(response["forecast"]["curve"]) == 30
    assert set(response["maintenance"]) == {"ash", "clean", "sweep", "service"}
    assert response["order_by"] and response["restock_date"]


async def test_calendar(hass: HomeAssistant, loaded, hass_client) -> None:
    client = await hass_client()
    cal = eid(hass, "calendar")
    resp = await client.get(f"/api/calendars/{cal}?start=2026-01-01T00:00:00Z&end=2030-01-01T00:00:00Z")
    assert resp.status == 200
    titles = {e["summary"] for e in await resp.json()}
    assert {"Pellets run out (expected)", "Order pellets by", "Restock date"} <= titles
    assert hass.states.get(cal).state in ("on", "off")
    assert CALENDAR_DOMAIN in hass.config.components


async def test_card_and_blueprints_are_installed(hass: HomeAssistant, loaded, hass_client) -> None:
    client = await hass_client()
    resp = await client.get("/burnerific/burnerific-card.js")
    assert resp.status == 200 and "customElements.define" in await resp.text()
    folder = Path(hass.config.path("blueprints", "automation", DOMAIN))
    assert {p.name for p in folder.glob("*.yaml")} == {"alarm.yaml", "fill_silo_reminder.yaml", "order_pellets.yaml"}


async def test_controller_events_and_alarm(hass: HomeAssistant, loaded, fake_boiler) -> None:
    boiler, _ = fake_boiler
    events = async_capture_events(hass, EVENT_CONTROLLER)
    alarms = async_capture_events(hass, EVENT_ALARM)
    boiler.events.insert(0, ("261023", "200000", 1, 0, 5, 13))  # a new log entry: burning -> ignition alarm
    boiler.state = 13
    boiler.settings["hopper"]["content"] = "3"
    await _refresh(hass, loaded)
    assert [(e.data["before_state"], e.data["after_state"]) for e in events] == [("power", "alarm_ignition")]
    assert alarms and alarms[0].data["hint"] == "out_of_pellets"
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"alarm_{loaded.entry_id}")
    assert issue is not None and issue.translation_key == "alarm_out_of_pellets"
    assert state_of(hass, "boiler").attributes["hvac_action"] == HVACAction.OFF

    boiler.state = 5
    await _refresh(hass, loaded, slow=False)
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"alarm_{loaded.entry_id}") is None


async def test_wrong_pin_starts_reauth(hass: HomeAssistant, loaded, fake_boiler) -> None:
    boiler, _ = fake_boiler
    boiler.pin = "1111111111"  # changed on the panel
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            "climate", "set_temperature", {"entity_id": eid(hass, "boiler"), "temperature": 70}, blocking=True
        )
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)
    flow = next(f for f in flows if f["context"]["source"] == "reauth")
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {"pin": "1111111111"})
    assert result["reason"] == "reauth_successful"
    assert loaded.data["pin"] == "1111111111"


async def test_stokercloud_fallback(hass: HomeAssistant, entry, open_meteo, fake_boiler) -> None:
    from custom_components.burnerific.nbe.model import BoilerData

    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(entry, options=entry.options | {"stokercloud_user": "12345678"})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    boiler, _ = fake_boiler
    boiler.drop = 10**6  # the LAN goes silent
    cloud = entry.runtime_data.boiler.cloud
    entry.runtime_data.boiler.client.timeout = 0.2
    entry.runtime_data.boiler.client.retries = 0
    fake = BoilerData(raw={}, at=entry.runtime_data.boiler.data.at, state=5, boiler_temp=60.0, power_pct=40, source="cloud")
    with patch.object(type(cloud), "read", return_value=fake):
        await entry.runtime_data.boiler.async_refresh()
        await hass.async_block_till_done()
    assert state_of(hass, "data_source").state == "cloud"
    assert state_of(hass, "lan_connection").state == "off"
    assert float(state_of(hass, "boiler_temp").state) == 60.0


async def test_unload_and_remove(hass: HomeAssistant, loaded, hass_storage) -> None:
    assert await hass.config_entries.async_unload(loaded.entry_id)
    await hass.async_block_till_done()
    assert loaded.state is ConfigEntryState.NOT_LOADED
    assert f"{DOMAIN}.{loaded.entry_id}" in hass_storage
    await hass.config_entries.async_remove(loaded.entry_id)
    await hass.async_block_till_done()
    assert f"{DOMAIN}.{loaded.entry_id}" not in hass_storage


async def test_migrate_v1(hass: HomeAssistant, fake_boiler, open_meteo) -> None:
    _, host = fake_boiler
    old = MockConfigEntry(
        domain=DOMAIN, unique_id="12345678", version=1,
        data={"host": host, "serial": "12345678", "pin": PIN}, options={"season_end": "04-01"},
    )
    old.add_to_hass(hass)
    assert await hass.config_entries.async_setup(old.entry_id)
    await hass.async_block_till_done()
    assert old.version == 2
    assert old.options == {"restock_date": "04-01"}


async def test_boiler_offline_retries(hass: HomeAssistant, open_meteo, socket_enabled) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="1", version=2, data={"host": "127.0.0.1:9", "serial": "1"})
    entry.add_to_hass(hass)
    await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
