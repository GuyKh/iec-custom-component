# Implementation Plan: `iec-custom-component` Coordinator — Bug Fixes, Refactor & Parallelization

**Repo:** `GuyKh/iec-custom-component`
**Primary file:** `custom_components/iec/coordinator.py` (~2054 lines)
**Secondary files:** `custom_components/iec/__init__.py`, `commons.py`, `const.py`, `sensor.py`, `tests/`

## Context for the agent

This is a Home Assistant (HA) custom integration polling the Israel Electric Company (IEC) API hourly via a `DataUpdateCoordinator`. The update cycle is currently a long chain of sequential `await`s across contracts, devices/meters, tariffs, and readings. The goal is to (1) fix known correctness bugs, (2) refactor the oversized coordinator into testable units, (3) parallelize independent API calls with bounded concurrency, and (4) align with current HA integration best practices.

**Hard constraints:**

- Read and follow the repo's `AGENTS.md` (branch naming, use `./scripts/setup` / `./scripts/lint` / `./scripts/typecheck` instead of ad-hoc commands, breaking-change warning policy). Where this plan and AGENTS.md conflict on codebase description, this plan wins — AGENTS.md is partially stale and Phase 7 updates it.
- Preserve all existing sensor entity IDs, statistic IDs (`iec:iec_meter_{n}_energy_consumption` etc.), config entry data schema, and the shape of `coordinator.data` consumed by `sensor.py` / `binary_sensor.py`. This is a refactor, not a behavior redesign.
- The IEC API is fragile and possibly rate-limited. All new concurrency MUST be bounded (semaphore) and failure-isolated (`return_exceptions=True` or per-task try/except).
- Keep compatibility with the existing `StatisticMeanType` import fallback for HA < 2025.10.
- Python 3.12+ / HA-style async: no blocking I/O in the event loop; recorder calls stay on `async_add_executor_job`.
- Run `ruff check` (config in `.ruff.toml`) and the existing test suite after every phase. Do not proceed to the next phase with failures.

Work in the phase order below. Each phase should be a separate commit (or PR) with the stated acceptance criteria met.

---

## Phase 1 — Correctness bug fixes (do these first, before any refactor)

### 1.1 `estimated_bill_dict` leaks across contracts and devices

In `_update_data`, `estimated_bill_dict = None` is initialized once **before** the contract loop and only assigned inside the smart-meter device loop. Consequences: a non-smart-meter contract following a smart one inherits the previous contract's estimated bill; multi-meter contracts keep only the last device's estimate.

**Fix:** Reset `estimated_bill_dict = None` at the top of each contract iteration. Key the estimate per device where the data structure allows (if sensors currently expect a single dict per contract, keep contract-level but document that it reflects the last processed meter — prefer per-device keying if `sensor.py` can be adapted without changing entity unique IDs).

### 1.2 Stale caches never invalidated

End-of-cycle cleanup in `_update_data` clears `_today_readings`, `_devices_by_contract_id`, `_kwh_tariff`, `_readings` — but NOT `_last_meter_reading` (so estimated bills use the meter reading from HA startup forever) and NOT `_kva_tariff` (inconsistent with `_kwh_tariff`).

**Fix:** Introduce explicit cache policy. Per-cycle caches (cleared each update): `_readings`, `_today_readings`, `_last_meter_reading`, `_devices_by_contract_id`. Long-lived caches with TTL (e.g., 24h, stored as `(value, fetched_at)`): `_kwh_tariff`, `_kva_tariff`, `_delivery_tariff_by_phase`, `_distribution_tariff_by_phase`, `_power_size_by_connection_size`, `_connection_size_by_account_id`. Implement a small helper (dataclass or dict of `(value, timestamp)`) rather than ad-hoc clears.

### 1.3 Falsy-value cache misses

Several caches use `if not value:` where legitimate values are falsy: `_get_devices_by_contract_id` caches `[]` then refetches every call; a zero tariff refetches every time.

**Fix:** Use `if key not in cache:` sentinel-based checks so negative/empty results are cached for the cycle too. Apply to: `_get_devices_by_contract_id`, `_get_devices_by_device_id`, `_get_last_meter_reading`, `_get_delivery_tariff`, `_get_distribution_tariff`, `_get_power_size`, `_get_connection_size`.

### 1.4 Stop swallowing `asyncio.CancelledError`

`CancelledError` is caught and suppressed in `_resolve_bp_number_for_contract`, `_load_selected_contracts`, `_get_kwh_tariff`, `_get_kva_tariff`, `_fetch_tariffs_from_calculators`, invoice fetching, and `_async_update_data` (which returns `{}`).

**Fix:** Remove every `except asyncio.CancelledError` handler and let cancellation propagate (HA relies on it for shutdown/reload). Where the original intent was resilience to flaky networking, catch `TimeoutError` and `aiohttp.ClientError` explicitly instead. **This applies to `config_flow.py` as well** — it contains 7 more `except asyncio.CancelledError` handlers (in `_validate_login` and related steps) that remap cancellation to `cannot_connect`; apply the same fix there.

### 1.5 `_insert_statistics` race with shared state

`_insert_statistics` is launched with `hass.async_create_task(...)` per contract and runs concurrently with the rest of `_update_data`, while both read/write `_today_readings`, `_readings`, `_devices_by_contract_id` — which `_update_data` wipes at cycle end. Ordering is nondeterministic.

**Fix:**
- Launch with `self.config_entry.async_create_background_task(self.hass, coro, name=f"iec_stats_{contract_id}")` so tasks are tracked and cancelled on unload.
- Collect the created tasks; either `await asyncio.gather(*stat_tasks)` before the cleanup block, or (preferred) pass the data `_insert_statistics` needs as arguments so it doesn't share mutable coordinator caches at all.

### 1.6 Timezone bug in `commons.localize_datetime`

`datetime.now()` is naive host-local time; `dt.replace(tzinfo=TIMEZONE)` merely stamps it as Asia/Jerusalem. Wrong results whenever the HA host runs in a different timezone.

**Fix:** At call sites, replace `localize_datetime(datetime.now())` with `datetime.now(TIMEZONE)` (or `homeassistant.util.dt.now()` converted to `TIMEZONE`). Keep `localize_datetime` for attaching TZ to genuinely-Jerusalem-naive API timestamps, and document that distinction in its docstring.

### 1.7 Duplicate fallback HTTP calls in tariff fetching

`_get_kwh_tariff` and `_get_kva_tariff` each independently call `_fetch_tariffs_from_calculators()`, which makes up to 2 HTTP requests and returns BOTH tariffs — half is discarded each time (worst case: 4 requests for 1 payload).

**Fix:** Cache the calculators result for the cycle (single shared in-flight `asyncio.Task` — see Phase 3 task-caching pattern) so both getters consume one fetch.

**Phase 1 acceptance criteria:**
- Unit test: two contracts (smart then non-smart) → non-smart contract's `ESTIMATED_BILL_DICT_NAME` is `None`, not the previous contract's value.
- Unit test: `_get_devices_by_contract_id` returning `[]` is called against the API exactly once per cycle.
- Grep: zero occurrences of `except asyncio.CancelledError` in `custom_components/iec/`.
- `_fetch_tariffs_from_calculators` invoked at most once per update cycle (assert via mock call count).
- `ruff check` passes; existing `tests/coordinator/test_retry.py` passes.

---## Phase 2 — Structural refactor (prerequisite for safe parallelization)

Split `coordinator.py` into focused modules. No behavior changes in this phase — pure extraction with identical logic.

### 2.1 New module layout

```
custom_components/iec/
├── coordinator.py      # IecApiCoordinator: orchestration, token lifecycle, data assembly (~400 lines)
├── data_fetcher.py     # IecDataFetcher: all API calls + caching (the _get_* methods)
├── bill.py             # Pure functions: _calculate_estimated_bill, _get_invoice_reading_dates,
│                       #   _parse_invoice_last_date, _select_meter_data,
│                       #   _extract_valid_future_consumption, _is_backstream_meter_kind,
│                       #   _map_meter_kind_to_remote_reading_param, _build_backstream_totals
├── statistics.py       # _insert_statistics and its helpers (recorder interaction)
└── ...existing files
```

### 2.2 Decompose `_update_data`

Extract the ~350-line body into:
- `_process_contract(contract_id, contract, kwh_tariff, kva_tariff, ...) -> dict` — everything inside the current contract loop, returning that contract's data dict.
- `_process_device(contract_id, device, ...) -> DeviceResult` — everything inside the device loop. Define a small `@dataclass DeviceResult` (daily_readings, future_consumption, backstream flags/totals, estimated_bill) instead of mutating four dicts by side effect.

### 2.3 Fix fragile imports

- `commons.py`: replace `from custom_components.iec import DOMAIN` with `from .const import DOMAIN`.
- `iec_entity.py`: replace `from custom_components.iec import IecApiCoordinator` and `from custom_components.iec.commons import ...` with relative imports (`from .coordinator import IecApiCoordinator`, `from .commons import ...`).

Both currently import through the package `__init__` via an absolute path — a near-circular import that breaks if the component is loaded under a different package root and makes the extraction in 2.1 riskier. Fix these FIRST within this phase.

**Phase 2 acceptance criteria:**
- `coordinator.data` output is byte-identical for a fixtured update cycle (add a snapshot/fixture test before extracting, verify after).
- `bill.py` contains only pure functions (no `self`, no I/O) — verified by it importing nothing from `homeassistant.*` except type-only needs.
- All files < ~600 lines. `ruff check` passes.

---

## Phase 3 — Parallelization (the core objective)

Apply concurrency only after Phases 1–2 land. Every subsection below must respect the shared **concurrency guardrails**:

- Module-level bound: `self._api_semaphore = asyncio.Semaphore(4)` on the fetcher; every outbound IEC API call acquires it. Make the limit a module constant (`MAX_CONCURRENT_API_CALLS = 4`) so it's tunable.
- **In-flight task caching** to prevent cache stampede (concurrent callers of the same cache key must share one request):

```python
def _get_or_create_task(self, cache: dict, key, factory) -> asyncio.Task:
    if key not in cache:
        cache[key] = asyncio.ensure_future(self._guarded(factory()))
    return cache[key]

async def _guarded(self, coro):
    async with self._api_semaphore:
        return await coro
```

Convert `_readings`, `_today_readings`, `_devices_by_contract_id`, `_devices_by_meter_id`, tariff caches, and the calculators fallback (1.7) to store `asyncio.Task` objects (await on read). On task failure, remove the key so the next cycle retries; do not cache exceptions across cycles.
- Failure isolation: contract-level gather uses `return_exceptions=True`; log per-contract failures and still return data for the healthy contracts. Only raise `UpdateFailed` if ALL contracts fail.

### 3.1 Gather independent top-of-cycle fetches

In `_update_data`:
```python
kwh_tariff, kva_tariff = await asyncio.gather(
    self._fetcher.get_kwh_tariff(), self._fetcher.get_kva_tariff()
)
```
(`_load_selected_contracts` and `_load_contract_account_mapping` may also be gathered — verify no shared-state ordering dependency; if `_load_contract_account_mapping` populates `_shared_contract_ids` used later only, they are independent.)

### 3.2 Parallelize `_load_selected_contracts` internals

- `_resolve_bp_number_for_contract` for all unmapped contracts → `asyncio.gather`.
- `self.api.get_contracts(bp_number)` per BP number → `asyncio.gather`, then merge.

### 3.3 Per-contract parallelism

```python
contract_results = await asyncio.gather(
    *(self._process_contract(cid, ...) for cid in self._contract_ids),
    return_exceptions=True,
)
```
Handle exceptions per result: log with contract ID, skip that contract's key in `data`.

### 3.4 Per-device parallelism inside `_process_contract`

Gather `_process_device` across the contract's devices. Within `_process_device`, fetch the period (monthly/weekly) reading and today's DAILY reading concurrently — the DAILY result is needed for `_verify_daily_readings_exist` and the future-consumption fallback anyway, so prefetching it removes a sequential round-trip. Keep the "two days ago MONTHLY" fallback lazy (only fetched when the primary future-consumption extraction fails).

### 3.5 `_estimate_bill` internals

`_get_distribution_tariff(phase)` and `_get_delivery_tariff(phase)` → gather. Leave the dependent chain `_get_account_id → _get_connection_size → _get_power_size` sequential.

### 3.6 Statistics

In `statistics.py`, per-device processing may be gathered (bounded by the same semaphore for its `_get_readings` calls). Recorder executor jobs (`get_last_statistics`, `statistics_during_period`) can run concurrently across devices — they're thread-pool bound, not event-loop blocking.

**Phase 3 acceptance criteria:**
- Test with mocked API (add artificial 50ms latency per call, 3 contracts × 2 devices): total `_update_data` wall time drops by ≥50% vs. sequential baseline; assert max observed concurrent API calls ≤ semaphore limit.
- Test: two devices requesting the same reading key concurrently → underlying API mock called exactly once.
- Test: one contract raising `IECError` → other contracts' data still present in coordinator output; no unhandled exception.
- Test: cancelling the update task mid-gather propagates `CancelledError` (no swallowing) and leaves no orphan tasks (`asyncio.all_tasks()` clean).

---

## Phase 4 — HA best-practice alignment

### 4.1 `entry.runtime_data`

Replace `hass.data[DOMAIN][entry.entry_id]` with:
```python
type IecConfigEntry = ConfigEntry[IecApiCoordinator]
```
Store the coordinator on `entry.runtime_data`; update `sensor.py`, `binary_sensor.py`, `__init__.py` accordingly.

### 4.2 First-refresh error handling

In `async_setup_entry`, remove the broad `except Exception` around `async_config_entry_first_refresh()`. Let `ConfigEntryAuthFailed` and `ConfigEntryNotReady` (raised by the coordinator on `UpdateFailed`) propagate so HA retries setup with backoff instead of creating empty entities.

### 4.3 Service registration

`debug_get_coordinator_data` is re-registered per entry, closes over one coordinator, and is never unregistered. Fix: register once (guard with `hass.services.has_service`), resolve the coordinator(s) at call time from loaded entries, and remove the service when the last entry unloads.

### 4.4 Remove `traceback` usage

In `_async_update_data`, replace `_LOGGER.error(...)` + `traceback.format_exc()` with `_LOGGER.exception("Failed updating IEC data")` and drop the `traceback` import.

### 4.5 Replace the debug service with HA-native diagnostics

The `debug_get_coordinator_data` service logs the FULL coordinator data at INFO level and fires it onto the event bus — this includes contract/personal data and is the wrong mechanism. Create `diagnostics.py` implementing `async_get_config_entry_diagnostics`, returning coordinator data with `async_redact_data()` applied to sensitive keys (`CONF_API_TOKEN`, `CONF_USER_ID`, `CONF_BP_NUMBER`, `CONF_BP_NUMBER_TO_CONTRACT`, contract IDs/addresses, JWT contents). Then remove the debug service registration from `__init__.py` and delete `services.yaml` (it exists only for that service). Note in the PR that this removes a user-visible service (per the AGENTS.md breaking-change policy).

### 4.6 Remove dead `StatisticMeanType` fallback (verify first)

`hacs.json` declares `"homeassistant": "2025.10.2"` as the minimum supported version, which makes the `try/except ImportError` fallback for HA < 2025.10 in `coordinator.py` unreachable for HACS installs. Either remove the fallback (preferred — one less divergent code path) or, if manual installs on older HA must be supported, keep it and lower/remove the `hacs.json` minimum so the two are consistent. Ask the maintainer if unsure; default to removal.

**Phase 4 acceptance criteria:** integration loads, reloads, and unloads cleanly in a test harness (`pytest-homeassistant-custom-component`); a first-refresh network failure results in HA setup retry (ConfigEntryNotReady), not silent empty entities; diagnostics output contains no token, user ID, or BP number values; `hassfest` and HACS validation workflows still pass.

---

## Phase 5 — Logging hygiene

1. Fix every `_LOGGER.exception("message", e)` / `_LOGGER.error("message", e)` where the message has no `%s` placeholder (this currently raises internal logging format errors). `logger.exception()` already appends the traceback — drop the extra argument. Audit all ~15 occurrences in `coordinator.py`.
2. Replace deprecated `_LOGGER.warn(` → `_LOGGER.warning(` (2 occurrences: bill estimation failure, future-consumption fallback).
3. Convert f-string log calls to lazy `%s` formatting.
4. Enable ruff rules `G` (flake8-logging-format) and `LOG` in `.ruff.toml` to enforce 1–3 mechanically; fix all resulting findings.

**Acceptance:** `ruff check` clean with the new rules enabled; no `logging` internal errors when running the test suite with `caplog` at DEBUG.

---

## Phase 6 — Tests & CI

There is currently **no CI workflow that runs the test suite** — `lint.yml` runs ruff + mypy and `validate.yml` runs hassfest/HACS, but `tests/` is never executed. The two existing tests in `tests/coordinator/test_retry.py` are tautological (they assert `error_400.code == 400` and re-derive an arithmetic formula) and exercise none of the actual coordinator code.

### 6.1 Test infrastructure

- Split dependencies: keep `requirements.txt` as-is for the dev environment scripts, but add `requirements-test.txt` with `pytest`, `pytest-asyncio`, `pytest-homeassistant-custom-component` (pin compatible with HA 2025.10.2), `freezegun`.
- Add `tests/conftest.py` with a mocked `IecClient` fixture (contract/device/reading/invoice fixtures as JSON files under `tests/fixtures/`).
- Add `.github/workflows/test.yml` running `pytest tests/` on push/PR to `main` (same Python version as the other workflows — see Phase 7.3).

### 6.2 Test content

1. **`bill.py` unit tests** (pure functions, no mocks needed): `_calculate_estimated_bill` (with/without last invoice, month-boundary spans, missing future consumption), `_get_invoice_reading_dates` (string vs. `date` `last_date`, unsorted invoices, future-dated invoices), `_parse_invoice_last_date`, `_select_meter_data` (exact match / serial-only / code-only / fallback-to-first), `_is_backstream_meter_kind` (int 2, "BackStream", Hebrew "דו כיווני", None).
2. **Fetcher cache tests:** TTL expiry, falsy-value caching, in-flight task dedup, per-cycle invalidation set.
3. **Coordinator integration test** with fully mocked `IecClient`: full update cycle snapshot of `coordinator.data`; the parallelism tests from Phase 3.
4. **Replace** the two tautological tests in `test_retry.py` with real ones: mock `api.load_jwt_token`/`api.check_token` to raise `IECError(401)` and assert retry-then-`ConfigEntryAuthFailed`; raise `IECError(500)` and assert immediate re-raise without retry.

**Acceptance:** meaningful coverage on `bill.py` (target ≥90%) and the fetcher cache layer; `test.yml` green in CI.

---

## Phase 7 — Repo hygiene & documentation (AGENTS.md and friends)

### 7.1 Update AGENTS.md to match the refactored codebase

AGENTS.md is the instruction file future agents (including you, next session) will read — it MUST be updated in the same PRs that change what it describes, not as an afterthought. Required changes:

- **Code Structure section:** add `data_fetcher.py`, `bill.py`, `statistics.py`, `diagnostics.py` with one-line descriptions; remove `services.yaml` (deleted in 4.5).
- **Coordinator Pattern section:** replace "Stores data in `hass.data[DOMAIN][entry.entry_id]`" with the `entry.runtime_data` / `IecConfigEntry` pattern from 4.1.
- **Add a new "Concurrency & Caching Rules" section** codifying the Phase 3 invariants so future agents don't violate them: (a) every IEC API call goes through the fetcher and acquires `MAX_CONCURRENT_API_CALLS` semaphore; (b) never add unbounded `asyncio.gather` over API calls; (c) cache policy — which caches are per-cycle vs. TTL, and that cache reads must go through the in-flight-task helper; (d) never catch `asyncio.CancelledError`; (e) contract-level failures must be isolated, never fail the whole refresh for one contract.
- **Add a "Logging Rules" section:** lazy `%s` formatting, no args to `logger.exception()` without placeholders, `warning` not `warn` (ruff `G`/`LOG` rules enforce this — mention them).
- **Testing section:** currently claims tests run via `lint.yml`/`validate.yml`, which is false. Rewrite to reference `test.yml`, `pytest tests/`, and the fixture/conftest layout from Phase 6. Add "when changing `bill.py` or the fetcher, add/update the corresponding unit tests" to the workflow rules.

### 7.2 Fix existing AGENTS.md drift (independent of the refactor)

- "Data Storage Keys" lists `STATIC_DICT_NAME = "statics"` — the actual constant in `const.py` is `STATICS_DICT_NAME`. Fix, and add the missing keys (`BACKSTREAM_METERS_DICT_NAME`, `BACKSTREAM_TOTALS_DICT_NAME`).
- Tech Stack says Python 3.13+; `.ruff.toml` targets `py312`; CI workflows use Python 3.14. Align all three (see 7.3).
- The debug-logging snippet and scripts references are fine — keep.

### 7.3 Align Python/tooling versions

Pick one Python floor consistent with HA 2025.10 (3.13 is the safe choice) and apply it everywhere: `target-version = "py313"` in `.ruff.toml`, `python-version: "3.13"` in `lint.yml` / new `test.yml` (or keep CI on latest but set ruff's target to the floor — the point is the three files must agree deliberately, documented in AGENTS.md).

### 7.4 Ruff config additions

In `.ruff.toml` `lint.select`, add: `G` (flake8-logging-format), `LOG`, `ASYNC` (flake8-async — catches blocking calls in async code), `BLE001` (blind `except Exception` — several exist in the coordinator; add targeted `# noqa: BLE001` only where a catch-all is genuinely intended, e.g., the top of `_async_update_data`). Fix all resulting findings. Consider raising docstring coverage rather than suppressing new `D` findings in the new modules.

### 7.5 CONTRIBUTING.md

Add a short "Running tests" section (`pip install -r requirements-test.txt && pytest tests/`) and mention the new module layout so external contributors don't put API calls back into `coordinator.py`.

**Phase 7 acceptance criteria:** AGENTS.md accurately describes the post-refactor codebase (an agent following only AGENTS.md can locate every module and run lint + tests successfully); no version-number contradictions between AGENTS.md, `.ruff.toml`, and workflows.

---

## Out of scope (do NOT do)

- No changes to `config_flow.py` logic beyond the `CancelledError` fix (1.4) — no flow-step, schema, or entity/unique-ID changes.
- No new sensors or features.
- No changes to the `iec-api` library pin (`iec-api==0.5.15`) unless a fix strictly requires it — flag instead of upgrading.
- No changes to the update interval or the dummy-listener keepalive mechanism (leave as-is; it's intentional).
- No changes to `release.yml` — it already injects the tag version into `manifest.json` at release time; the in-repo `"version": "0.0.1"` is expected.
- No changes to translations (`en.json`/`he.json`) unless the diagnostics work requires a string.

## Final verification checklist

- [ ] `ruff check .` clean (including new `G`/`LOG`/`ASYNC`/`BLE001` rules) and `./scripts/typecheck` (mypy) clean
- [ ] Full test suite passes locally and in the new `test.yml` workflow
- [ ] Fixture-based `coordinator.data` snapshot unchanged vs. pre-refactor baseline (except the Phase 1.1 bill-leak fix, which is an intentional behavior change — document it in the PR)
- [ ] Load / reload / unload cycle clean, no orphan tasks, no "Task was destroyed but it is pending" warnings
- [ ] `hassfest` + HACS validation green (diagnostics.py and services.yaml removal don't break them)
- [ ] AGENTS.md, CONTRIBUTING.md, and `.ruff.toml` updated in the same PRs as the code they describe
- [ ] CHANGELOG/PR description lists: bug fixes (1.1–1.7), refactor map (old symbol → new module), concurrency model (semaphore limit, failure isolation), removed debug service → diagnostics migration, and all intentional behavior changes
