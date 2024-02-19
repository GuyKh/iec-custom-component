"""Constants for iec."""
from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)

NAME = "IEC"
DOMAIN = "iec"
VERSION = "0.1.0"
ATTRIBUTION = "Data provided by http://jsonplaceholder.typicode.com/"

CONF_USER_ID = "user_id"
CONF_OTP = "one_time_password"

ATTR_BP_NUMBER = "bp_number"
ATTR_METER_NUMBER = "meter_number"
ATTR_METER_TYPE = "meter_type"
ATTR_METER_CODE = "meter_code"
ATTR_METER_IS_ACTIVE = "is_active_meter"
ATTR_CONTRACT_ID = "contract_id"
ATTR_METER_READINGS = "meter_readings"
