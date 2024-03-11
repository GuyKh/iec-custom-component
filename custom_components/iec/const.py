"""Constants for the IEC integration."""
from datetime import datetime

from iec_api.models.remote_reading import RemoteReading

DOMAIN = "iec"

ILS = "₪"
ILS_PER_KWH = "ILS/kWh"

EMPTY_REMOTE_READING = RemoteReading(0, datetime(2024, 1, 1), 0)
CONF_USER_ID = "user_id"
CONF_TOTP_SECRET = "totp_secret"
CONF_BP_NUMBER = "bp_number"
CONF_SELECTED_CONTRACTS = "selected_contracts"
CONF_AVAILABLE_CONTRACTS = "contracts"
CONF_MAIN_CONTRACT_ID = "main_contract_id"
STATICS_DICT_NAME = "statics"
INVOICE_DICT_NAME = "invoice"
CONTRACT_DICT_NAME = "contract"
DAILY_READINGS_DICT_NAME = "daily_readings"
FUTURE_CONSUMPTIONS_DICT_NAME = "future_consumption"
STATIC_KWH_TARIFF = "kwh_tariff"
STATIC_BP_NUMBER = "bp_number"
