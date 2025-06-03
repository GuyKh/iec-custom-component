"""Constants for the IEC integration."""

from datetime import datetime

from iec_api.models.invoice import Invoice
from iec_api.models.meter_reading import MeterReading
from iec_api.models.remote_reading import RemoteReading

DOMAIN = "iec"

ILS = "₪"
ILS_PER_KWH = "ILS/kWh"

EMPTY_DATETIME = datetime.fromordinal(1)
EMPTY_REMOTE_READING = RemoteReading(0, datetime(2024, 1, 1), 0)
EMPTY_INVOICE = Invoice(
    consumption=0,
    amount_origin=0,
    days_period="0",
    to_date=None,
    last_date=None,
    amount_paid=0,
    amount_to_pay=0,
    invoice_id=0,
    contract_number=0,
    document_id="",
    from_date=None,
    full_date=None,
    has_direct_debit=False,
    reading_code=0,
    invoice_type=0,
    invoice_payment_status=0,
    order_number=0,
    meter_readings=[
        MeterReading(
            reading=0,
            reading_code="",
            reading_date=EMPTY_DATETIME,
            usage="",
            serial_number="",
        ),
    ],
)
CONF_USER_ID = "user_id"
CONF_TOTP_SECRET = "totp_secret"
CONF_BP_NUMBER = "bp_number"
CONF_SELECTED_CONTRACTS = "selected_contracts"
CONF_AVAILABLE_CONTRACTS = "contracts"
CONF_MAIN_CONTRACT_ID = "main_contract_id"
JWT_DICT_NAME = "jwt"
STATICS_DICT_NAME = "statics"
ATTRIBUTES_DICT_NAME = "entity_attributes"
ESTIMATED_BILL_DICT_NAME = "estimated_bill"
METER_ID_ATTR_NAME = "device_number"
CONTRACT_ID_ATTR_NAME = "contract_id"
IS_SMART_METER_ATTR_NAME = "is_smart_meter"
TOTAL_EST_BILL_ATTR_NAME = "total_estimated_bill"
EST_BILL_DAYS_ATTR_NAME = "total_bill_days"
EST_BILL_CONSUMPTION_PRICE_ATTR_NAME = "consumption_price"
EST_BILL_DELIVERY_PRICE_ATTR_NAME = "delivery_price"
EST_BILL_DISTRIBUTION_PRICE_ATTR_NAME = "distribution_price"
EST_BILL_TOTAL_KVA_PRICE_ATTR_NAME = "total_kva_price"
EST_BILL_KWH_CONSUMPTION_ATTR_NAME = "estimated_kwh_consumption_in_bill"
INVOICE_DICT_NAME = "invoice"
CONTRACT_DICT_NAME = "contract"
DAILY_READINGS_DICT_NAME = "daily_readings"
FUTURE_CONSUMPTIONS_DICT_NAME = "future_consumption"
STATIC_KWH_TARIFF = "kwh_tariff"
STATIC_KVA_TARIFF = "kva_tariff"
STATIC_BP_NUMBER = "bp_number"
ELECTRIC_INVOICE_DOC_ID = "1"
ACCESS_TOKEN_ISSUED_AT = "iat"
ACCESS_TOKEN_EXPIRATION_TIME = "exp"
