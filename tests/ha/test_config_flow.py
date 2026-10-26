from __future__ import annotations

from unittest.mock import patch

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.burnerific.const import DOMAIN
from custom_components.burnerific.nbe import DiscoveryInfo, NbeTimeout

from .conftest import PIN

SCAN = "custom_components.burnerific.config_flow.scan"
SETUP = "custom_components.burnerific.async_setup_entry"


async def test_discovered_boiler(hass: HomeAssistant, fake_boiler) -> None:
    _, host = fake_boiler
    found = [DiscoveryInfo("12345678", "127.0.0.1", "v13", "1105", host)]
    with patch(SCAN, return_value=found):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pick"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": host, "pin": PIN})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pellets"

    with patch(SETUP, return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"initial_storage_kg": 960, "silo_capacity_kg": 250, "bag_kg": 16, "annual_kg": 4500}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "NBE v13 12345678"
    assert result["data"]["host"] == host
    assert result["data"]["pin"] == PIN
    assert result["data"]["initial_storage_kg"] == 960
    assert result["options"] == {"silo_capacity_kg": 250, "bag_kg": 16, "annual_kg": 4500}
    assert result["result"].unique_id == "12345678"


async def test_manual_entry_and_errors(hass: HomeAssistant, fake_boiler) -> None:
    _, host = fake_boiler
    with patch(SCAN, return_value=[]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["step_id"] == "manual"

    with patch("custom_components.burnerific.config_flow.NbeClient.discover", side_effect=NbeTimeout("no answer")):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": "10.0.0.250"})
    assert result["errors"] == {"base": "cannot_connect"}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": host, "serial": "", "pin": ""})
    assert result["step_id"] == "pellets"


async def test_already_configured(hass: HomeAssistant, fake_boiler, entry) -> None:
    entry.add_to_hass(hass)
    _, host = fake_boiler
    with patch(SCAN, return_value=[]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": host})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_flow(hass: HomeAssistant, entry, open_meteo) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "scan_interval": 60,
            "use_open_meteo": False,
            "bag_kg": 15,
            "silo_tracking": "integration",
            "annual_kg": 3000,
            "order_lead_days": 14,
            "season_end": "05-15",
            "kwh_per_kg": 4.9,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["silo_tracking"] == "integration"
    assert entry.runtime_data.pellets.inventory.bag_kg == 15  # reloaded with the new options


async def test_reconfigure_keeps_pin(hass: HomeAssistant, entry, fake_boiler, open_meteo) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    _, host = fake_boiler
    result = await entry.start_reconfigure_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"host": host, "pin": ""})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data["pin"] == PIN
