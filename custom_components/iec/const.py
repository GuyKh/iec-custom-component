"""Constants for the IEC integration."""
from datetime import datetime

from iec_api.models.remote_reading import RemoteReading

DOMAIN = "iec"

ILS = "₪"
ILS_PER_KWH = "ILS/kWh"

EMPTY_REMOTE_READING = RemoteReading(0, datetime(2024, 1, 1), 0)
CONF_USER_ID = "user_id"
CONF_TOTP_SECRET = "totp_secret"
STATICS_DICT_NAME = "statics"
INVOICE_DICT_NAME = "invoice"
DAILY_READINGS_DICT_NAME = "daily_readings"
FUTURE_CONSUMPTIONS_DICT_NAME = "future_consumption"
TODAY_READING_DICT_NAME = "today_reading"
STATIC_KWH_TARIFF = "kwh_tariff"
STATIC_CONTRACT = "contract_number"
STATIC_BP_NUMBER = "bp_number"
