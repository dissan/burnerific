"""Constants for Burnerific."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "burnerific"
MANUFACTURER: Final = "NBE"

CONF_SERIAL: Final = "serial"
CONF_PIN: Final = "pin"
CONF_SCAN_INTERVAL: Final = "scan_interval"
CONF_WEATHER_ENTITY: Final = "weather_entity"
CONF_TEMPERATURE_ENTITY: Final = "temperature_entity"
CONF_USE_OPEN_METEO: Final = "use_open_meteo"
CONF_BAG_KG: Final = "bag_kg"
CONF_SILO_CAPACITY: Final = "silo_capacity_kg"
CONF_ANNUAL_KG: Final = "annual_kg"
CONF_SILO_TRACKING: Final = "silo_tracking"
CONF_ORDER_LEAD_DAYS: Final = "order_lead_days"
CONF_SEASON_END: Final = "season_end"
CONF_KWH_PER_KG: Final = "kwh_per_kg"
CONF_INITIAL_STORAGE: Final = "initial_storage_kg"

SILO_BOILER: Final = "boiler"
SILO_INTEGRATION: Final = "integration"

DEFAULT_SCAN_INTERVAL: Final = 30
DEFAULT_BAG_KG: Final = 16.0
DEFAULT_ANNUAL_KG: Final = 4000.0
DEFAULT_ORDER_LEAD_DAYS: Final = 21
DEFAULT_KWH_PER_KG: Final = 4.8  # net calorific value of EN plus A1 pellets, ~4.6-5.0

FORECAST_INTERVAL_MIN: Final = 30
CLIMATE_MAX_AGE_DAYS: Final = 30
WEATHER_MAX_AGE_MIN: Final = 180

STORAGE_VERSION: Final = 1

SERVICE_ADD_DELIVERY: Final = "add_delivery"
SERVICE_FILL_SILO: Final = "fill_silo"
SERVICE_SET_STORAGE: Final = "set_storage"
SERVICE_SET_SILO: Final = "set_silo"
SERVICE_SET_SETTING: Final = "set_setting"
SERVICE_GET_FORECAST: Final = "get_forecast"

ATTR_CONFIG_ENTRY: Final = "config_entry_id"
ATTR_KG: Final = "kg"
ATTR_BAGS: Final = "bags"
ATTR_PRICE_PER_KG: Final = "price_per_kg"
ATTR_TOTAL_PRICE: Final = "total_price"
ATTR_NOTE: Final = "note"
ATTR_FROM_STORAGE: Final = "from_storage"
ATTR_KEY: Final = "key"
ATTR_VALUE: Final = "value"
