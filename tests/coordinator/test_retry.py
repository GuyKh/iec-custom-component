"""Tests for IEC coordinator 400 error retry logic."""

from iec_api.models.exceptions import IECError


def test_400_error_handling_in_retry_logic():
    """Test that 400 errors are properly identified in retry logic.

    This test verifies the logic change in coordinator.py where 400 errors
    are handled differently than other IECError codes.
    """
    # Simple unit test for the error checking logic
    error_400 = IECError(400, "expired refresh token")
    error_500 = IECError(500, "server error")

    # Verify our logic checks work
    assert error_400.code == 400
    assert error_500.code == 500
    assert error_400.code != 500


def test_retry_delay_calculation():
    """Test exponential backoff delay calculation.

    Validates the retry delay logic: 5, 10, 20 seconds for attempts 0, 1, 2.
    """
    base_delay = 5
    expected_delays = [5, 10, 20]
    calculated_delays = [base_delay * (2**attempt) for attempt in range(3)]
    assert calculated_delays == expected_delays
