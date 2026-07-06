"""Statistics insertion for IEC energy data into Home Assistant recorder."""

import itertools
import logging
from datetime import datetime, timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.util.unit_conversion import EnergyConverter
from iec_api.models.remote_reading import ReadingResolution

from .commons import TIMEZONE, localize_datetime
from .const import DOMAIN, ILS

try:
    from homeassistant.components.recorder.models import StatisticMeanType
except ImportError:
    from enum import StrEnum

    class StatisticMeanType(StrEnum):  # type: ignore[no-redef]
        """Statistic mean type."""

        NONE = "none"
        ARITHMETIC = "arithmetic"
        CIRCULAR = "circular"

_LOGGER = logging.getLogger(__name__)


async def insert_statistics(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    fetcher,
    contract_id: int,
    is_smart_meter: bool,
) -> None:
    """Insert energy consumption and cost statistics for a contract."""
    if not is_smart_meter:
        _LOGGER.info(
            "IEC Contract %s doesn't contain Smart Meters, not adding statistics",
            contract_id,
        )
        return

    _LOGGER.debug(
        "[IEC Statistics] Updating statistics for IEC Contract %s",
        contract_id,
    )
    devices = await fetcher._get_devices_by_contract_id(contract_id)
    kwh_price = await fetcher._get_kwh_tariff()
    localized_today = datetime.now(TIMEZONE)

    if not devices:
        _LOGGER.error(
            "[IEC Statistics] Failed fetching devices for IEC Contract %s",
            contract_id,
        )
        return

    for device in devices:
        id_prefix = f"iec_meter_{device.device_number}"
        consumption_statistic_id = f"{DOMAIN}:{id_prefix}_energy_consumption"
        cost_statistic_id = f"{DOMAIN}:{id_prefix}_energy_est_cost"
        production_statistic_id = f"{DOMAIN}:{id_prefix}_energy_production"

        last_stat = await get_instance(hass).async_add_executor_job(
            get_last_statistics, hass, 1, consumption_statistic_id, True, set()
        )

        if not last_stat:
            _LOGGER.debug(
                "[IEC Statistics] No statistics found, fetching today's MONTHLY readings "
                "to extract field `meterStartDate`"
            )

            month_ago_time = localized_today - timedelta(weeks=4)
            readings = await fetcher._get_readings(
                contract_id,
                device.device_number,
                device.device_code,
                localized_today,
                ReadingResolution.MONTHLY,
                device.meter_kind,
            )

            if (
                readings
                and readings.meter_list
                and readings.meter_list[0].meter_start_date
            ):
                month_ago_time = max(
                    month_ago_time,
                    localize_datetime(
                        datetime.combine(
                            readings.meter_list[0].meter_start_date,
                            datetime.min.time(),
                        )
                    ),
                )
            else:
                _LOGGER.debug(
                    "[IEC Statistics] Failed to extract field `meterStartDate`, "
                    "falling back to a month ago"
                )

            _LOGGER.debug("[IEC Statistics] Updating statistic for the first time")
            _LOGGER.debug(
                "[IEC Statistics] Fetching consumption from %s",
                month_ago_time.strftime("%Y-%m-%d %H:%M:%S"),
            )
            last_stat_time = 0.0
            readings = await fetcher._get_readings(
                contract_id,
                device.device_number,
                device.device_code,
                month_ago_time,
                ReadingResolution.DAILY,
                device.meter_kind,
            )

        else:
            last_stat_time = last_stat[consumption_statistic_id][0]["start"]
            from_date = datetime.fromtimestamp(last_stat_time, tz=TIMEZONE)
            _LOGGER.debug(
                "[IEC Statistics] Last statistics are from %s",
                from_date.strftime("%Y-%m-%d %H:%M:%S"),
            )

            if from_date.hour == 23:
                from_date = from_date + timedelta(hours=2)

            if localized_today.date() == from_date.date():
                _LOGGER.debug(
                    "[IEC Statistics] The date to fetch is today or later, "
                    "replacing it with Today at 01:00:00"
                )
                from_date = localized_today.replace(
                    hour=1, minute=0, second=0, microsecond=0
                )

            min_from_date = (localized_today - timedelta(days=30)).replace(
                hour=1, minute=0, second=0, microsecond=0
            )
            if from_date < min_from_date:
                _LOGGER.debug(
                    "[IEC Statistics] Last statistics are too old, "
                    "limiting fetch window to %s",
                    min_from_date.strftime("%Y-%m-%d %H:%M:%S"),
                )
                from_date = min_from_date

            _LOGGER.debug(
                "[IEC Statistics] Fetching consumption from %s",
                from_date.strftime("%Y-%m-%d %H:%M:%S"),
            )
            readings = await fetcher._get_readings(
                contract_id,
                device.device_number,
                device.device_code,
                from_date,
                ReadingResolution.DAILY,
                device.meter_kind,
            )
            if from_date.date() == localized_today.date() and readings:
                fetcher._today_readings[
                    str(contract_id) + "-" + device.device_number
                ] = readings

        if (
            not readings
            or not readings.meter_list
            or not len(readings.meter_list) > 0
            or not readings.meter_list[0].period_consumptions
            or not len(readings.meter_list[0].period_consumptions) > 0
        ):
            _LOGGER.debug("[IEC Statistics] No recent usage data. Skipping update")
            continue

        last_stat_hour = (
            datetime.fromtimestamp(last_stat_time, tz=TIMEZONE)
            if last_stat_time
            else readings.meter_list[0].period_consumptions[0].interval
        )
        last_stat_req_hour = (
            last_stat_hour
            if last_stat_hour.hour > 0
            else (last_stat_hour - timedelta(hours=1))
        )

        _LOGGER.debug(
            "[IEC Statistics] Fetching LongTerm Statistics since %s",
            last_stat_req_hour,
        )
        stats = await get_instance(hass).async_add_executor_job(
            statistics_during_period,
            hass,
            last_stat_req_hour,
            None,
            {
                cost_statistic_id,
                consumption_statistic_id,
                production_statistic_id,
            },
            "hour",
            None,
            {"sum"},
        )

        if not stats.get(consumption_statistic_id):
            _LOGGER.debug("[IEC Statistics] No recent usage data")
            consumption_sum = 0.0
        else:
            consumption_sum = stats[consumption_statistic_id][0]["sum"] or 0.0

        if not stats.get(cost_statistic_id):
            _LOGGER.debug("[IEC Statistics] No recent cost data")
            cost_sum = consumption_sum * kwh_price
        else:
            cost_sum = stats[cost_statistic_id][0]["sum"] or 0.0

        if not stats.get(production_statistic_id):
            _LOGGER.debug("[IEC Statistics] No recent production data")
            production_sum = 0.0
        else:
            production_sum = stats[production_statistic_id][0]["sum"] or 0.0

        _LOGGER.debug(
            "[IEC Statistics] Last Consumption Sum for C[%s] D[%s]: %s",
            contract_id,
            device.device_number,
            consumption_sum,
        )
        _LOGGER.debug(
            "[IEC Statistics] Last Estimated Cost Sum for C[%s] D[%s]: %s",
            contract_id,
            device.device_number,
            cost_sum,
        )

        new_readings = list(
            filter(
                lambda reading: (
                    reading.interval
                    >= datetime.fromtimestamp(last_stat_time, tz=TIMEZONE)
                ),
                readings.meter_list[0].period_consumptions,
            )
        )

        grouped_new_readings_by_hour = itertools.groupby(
            new_readings,
            key=lambda reading: reading.interval.replace(
                minute=0, second=0, microsecond=0
            ),
        )
        readings_by_hour: dict[datetime, float] = {}
        backstream_by_hour: dict[datetime, float] = {}

        if last_stat_req_hour and last_stat_req_hour.tzinfo is None:
            last_stat_req_hour = last_stat_req_hour.replace(tzinfo=TIMEZONE)

        for key, group in grouped_new_readings_by_hour:
            group_list = list(group)
            one_month_ago = localized_today - timedelta(days=30)
            if key.date() >= one_month_ago.date() and len(group_list) < 4:
                _LOGGER.debug(
                    "[IEC Statistics] LongTerm Statistics - Skipping %s "
                    "since it's partial for the hour "
                    "(data is less than 1 month old and has only %s readings)",
                    key,
                    len(group_list),
                )
                continue
            if key <= last_stat_req_hour:
                _LOGGER.debug(
                    "[IEC Statistics] LongTerm Statistics - Skipping %s "
                    "data since it's already reported",
                    key,
                )
                continue
            readings_by_hour[key] = sum(reading.consumption for reading in group_list)
            backstream_by_hour[key] = sum(
                reading.back_stream or 0 for reading in group_list
            )

        if not readings_by_hour and last_stat_time:
            attempted_local = from_date.astimezone(TIMEZONE)
            if attempted_local.date() < localized_today.date():
                month_anchor = localize_datetime(
                    datetime.combine(
                        attempted_local.date().replace(day=1),
                        datetime.min.time(),
                    )
                )
                monthly = await fetcher._get_readings(
                    contract_id,
                    device.device_number,
                    device.device_code,
                    month_anchor,
                    ReadingResolution.MONTHLY,
                    device.meter_kind,
                )
                daily_pc = next(
                    (
                        pc
                        for pc in (
                            monthly.meter_list[0].period_consumptions
                            if monthly and monthly.meter_list
                            else []
                        )
                        if pc.interval.astimezone(TIMEZONE).date()
                        == attempted_local.date()
                    ),
                    None,
                )
                if daily_pc is not None:
                    daily_kwh = daily_pc.consumption or 0.0
                    daily_back = daily_pc.back_stream or 0.0
                    base_dt = localize_datetime(
                        datetime.combine(attempted_local.date(), datetime.min.time())
                    )
                    for h in range(24):
                        hour_key = base_dt + timedelta(hours=h)
                        if hour_key <= last_stat_req_hour:
                            continue
                        readings_by_hour[hour_key] = daily_kwh / 24
                        backstream_by_hour[hour_key] = daily_back / 24
                    _LOGGER.debug(
                        "[IEC Statistics] DAILY for %s was "
                        "incomplete; synthesized 24 hourly entries from MONTHLY "
                        "aggregate (%s kWh)",
                        attempted_local.date(),
                        daily_kwh,
                    )
                else:
                    _LOGGER.debug(
                        "[IEC Statistics] No MONTHLY aggregate available for %s; "
                        "cannot advance past it",
                        attempted_local.date(),
                    )

        consumption_metadata: StatisticMetaData = {
            "has_mean": False,
            "has_sum": True,
            "mean_type": StatisticMeanType.NONE,
            "unit_class": EnergyConverter.UNIT_CLASS,  # type: ignore[typeddict-unknown-key]
            "name": f"IEC Meter {device.device_number} Consumption",
            "source": DOMAIN,
            "statistic_id": consumption_statistic_id,
            "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
        }

        cost_metadata: StatisticMetaData = {
            "has_mean": False,
            "has_sum": True,
            "mean_type": StatisticMeanType.NONE,
            "unit_class": None,  # type: ignore[typeddict-unknown-key]
            "name": f"IEC Meter {device.device_number} Estimated Cost",
            "source": DOMAIN,
            "statistic_id": cost_statistic_id,
            "unit_of_measurement": ILS,
        }

        production_metadata: StatisticMetaData = {
            "has_mean": False,
            "has_sum": True,
            "mean_type": StatisticMeanType.NONE,
            "unit_class": EnergyConverter.UNIT_CLASS,  # type: ignore[typeddict-unknown-key]
            "name": f"IEC Meter {device.device_number} Production",
            "source": DOMAIN,
            "statistic_id": production_statistic_id,
            "unit_of_measurement": UnitOfEnergy.KILO_WATT_HOUR,
        }

        consumption_statistics = []
        cost_statistics = []
        production_statistics = []
        for key, value in sorted(readings_by_hour.items()):
            consumption_sum += value
            cost_sum += value * kwh_price
            production_value = backstream_by_hour.get(key, 0.0)
            production_sum += production_value

            consumption_statistics.append(
                StatisticData(start=key, sum=consumption_sum, state=value)
            )

            cost_statistics.append(
                StatisticData(start=key, sum=cost_sum, state=value * kwh_price)
            )
            production_statistics.append(
                StatisticData(
                    start=key,
                    sum=production_sum,
                    state=production_value,
                )
            )

        if readings_by_hour:
            _LOGGER.debug(
                "[IEC Statistics] Last hour fetched for C[%s] D[%s]: %s",
                contract_id,
                device.device_number,
                max(readings_by_hour, key=lambda k: k),
            )
            _LOGGER.debug(
                "[IEC Statistics] New Consumption Sum for C[%s] D[%s]: %s",
                contract_id,
                device.device_number,
                consumption_sum,
            )
            _LOGGER.debug(
                "[IEC Statistics] New Estimated Cost Sum for C[%s] D[%s]: %s",
                contract_id,
                device.device_number,
                cost_sum,
            )
            _LOGGER.debug(
                "[IEC Statistics] New Production Sum for C[%s] D[%s]: %s",
                contract_id,
                device.device_number,
                production_sum,
            )

        async_add_external_statistics(
            hass, consumption_metadata, consumption_statistics
        )
        async_add_external_statistics(hass, cost_metadata, cost_statistics)
        async_add_external_statistics(hass, production_metadata, production_statistics)
