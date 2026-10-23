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
CONF_RESTOCK_DATE: Final = "restock_date"
CONF_SEASON_END: Final = "season_end"  # v1 name of restock_date
CONF_KWH_PER_KG: Final = "kwh_per_kg"
CONF_INITIAL_STORAGE: Final = "initial_storage_kg"
CONF_STOKERCLOUD_USER: Final = "stokercloud_user"
CONF_SILO_SENSOR: Final = "silo_sensor"
CONF_SILO_SENSOR_EMPTY: Final = "silo_sensor_empty"  # sensor reading (cm or mm) when the silo is empty
CONF_SILO_SENSOR_FULL: Final = "silo_sensor_full"  # ... and when it is full
CONF_APPLY_CALIBRATION: Final = "apply_calibration"
CONF_ASH_CAPACITY: Final = "ash_capacity_kg"
CONF_ASH_PCT: Final = "ash_pct"
CONF_CLEAN_INTERVAL_KG: Final = "clean_interval_kg"
CONF_SERVICE_INTERVAL_KG: Final = "service_interval_kg"
CONF_SERVICE_INTERVAL_DAYS: Final = "service_interval_days"
CONF_SWEEP_INTERVAL_DAYS: Final = "sweep_interval_days"
CONF_BOILER_EFFICIENCY: Final = "boiler_efficiency"
CONF_ELECTRICITY_PRICE: Final = "electricity_price"  # entity or fixed number
CONF_HEAT_PUMP_COP: Final = "heat_pump_cop"
CONF_SPLIT_HOT_WATER: Final = "split_hot_water"

SILO_BOILER: Final = "boiler"
SILO_INTEGRATION: Final = "integration"
SILO_SENSOR: Final = "sensor"

DEFAULT_SCAN_INTERVAL: Final = 30
DEFAULT_BAG_KG: Final = 16.0
DEFAULT_ANNUAL_KG: Final = 4000.0
DEFAULT_ORDER_LEAD_DAYS: Final = 21
DEFAULT_KWH_PER_KG: Final = 4.8  # net calorific value of EN plus A1 pellets, ~4.6-5.0
DEFAULT_ASH_CAPACITY: Final = 4.0  # kg of ash the drawer holds
DEFAULT_ASH_PCT: Final = 0.7  # EN plus A1 allows up to 0.7 %
DEFAULT_CLEAN_INTERVAL_KG: Final = 1000.0
DEFAULT_SERVICE_INTERVAL_KG: Final = 8000.0
DEFAULT_SERVICE_INTERVAL_DAYS: Final = 365
DEFAULT_SWEEP_INTERVAL_DAYS: Final = 365
DEFAULT_BOILER_EFFICIENCY: Final = 0.88
DEFAULT_HEAT_PUMP_COP: Final = 3.5

TASK_ASH: Final = "ash"
TASK_CLEAN: Final = "clean"
TASK_SWEEP: Final = "sweep"
TASK_SERVICE: Final = "service"
TASKS: Final = (TASK_ASH, TASK_CLEAN, TASK_SWEEP, TASK_SERVICE)

FORECAST_INTERVAL_MIN: Final = 30
CLIMATE_MAX_AGE_DAYS: Final = 30
WEATHER_MAX_AGE_MIN: Final = 180

STORAGE_VERSION: Final = 1

EVENT_CONTROLLER: Final = f"{DOMAIN}_controller_event"
EVENT_ALARM: Final = f"{DOMAIN}_alarm"

SERVICE_ADD_DELIVERY: Final = "add_delivery"
SERVICE_FILL_SILO: Final = "fill_silo"
SERVICE_SET_STORAGE: Final = "set_storage"
SERVICE_SET_SILO: Final = "set_silo"
SERVICE_SET_SETTING: Final = "set_setting"
SERVICE_GET_FORECAST: Final = "get_forecast"
SERVICE_IMPORT_FILLS: Final = "import_fills"
SERVICE_MAINTENANCE_DONE: Final = "maintenance_done"

ATTR_CONFIG_ENTRY: Final = "config_entry_id"
ATTR_KG: Final = "kg"
ATTR_BAGS: Final = "bags"
ATTR_PRICE_PER_KG: Final = "price_per_kg"
ATTR_TOTAL_PRICE: Final = "total_price"
ATTR_SUPPLIER: Final = "supplier"
ATTR_NOTE: Final = "note"
ATTR_FROM_STORAGE: Final = "from_storage"
ATTR_KEY: Final = "key"
ATTR_VALUE: Final = "value"
ATTR_LOG: Final = "log"
ATTR_UNIT: Final = "unit"
ATTR_TASK: Final = "task"
ATTR_DATE: Final = "date"
