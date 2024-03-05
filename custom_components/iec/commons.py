"""IEC common functions."""
from datetime import datetime

from iec_api.models.remote_reading import RemoteReading


def find_reading_by_date(daily_reading: RemoteReading, desired_date: datetime) -> bool:
    """Search for a daily reading matching a specific date.

    Args:
        daily_reading (RemoteReading): An object representing a daily reading.
            It is assumed to have a `date` attribute of type `datetime`.
        desired_date (datetime): The date to search for.

    Returns:
        bool: True if a daily reading with the matching date is found, False otherwise.

    Raises:
        AttributeError: If the `daily_reading` object is missing a required attribute (e.g., `date`).
        TypeError: If the `daily_reading.date` attribute is not of type `datetime`.

    """
    return (daily_reading.date.year == desired_date.year and
            daily_reading.date.month == desired_date.month and
            daily_reading.date.day == desired_date.day)  # Checks if the dates match
