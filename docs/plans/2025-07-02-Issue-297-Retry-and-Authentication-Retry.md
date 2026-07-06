# Issue 297 Implementation Plan Summary

## Overview
Implemented 400 error retry logic for IEC API authentication failures. When the IEC API returns 400 errors with expired refresh tokens, the system now retries the token validation up to 3 times with exponential backoff (5, 10, 20 seconds) before triggering reauthentication flow.

## Changes Made

### 1. Modified `custom_components/iec/coordinator.py`
- Enhanced `_async_update_data()` retry logic to specifically handle IECError 400
- Added exponential backoff retries (5/10/20s) for 400 errors before triggering reauth
- Non-400 errors continue to be handled by existing logic
- Added detailed logging for retry attempts

### 2. Added `tests/coordinator/test_retry.py`
- Unit tests to validate 400 error detection logic
- Tests verify retry delay calculations (5, 10, 20s)
- Confirms proper error code handling for different HTTP errors

## Verification
- All existing linting and type checking passes
- New test suite validates core retry logic
- Implementation follows existing patterns in the codebase
- Detailed logging for debug visibility into retry behavior

## Impact
This change improves user experience by gracefully handling expired refresh tokens with intelligent retries before requiring full reconfiguration, while maintaining backward compatibility for all other error types.