# Optimise → Tesla tariff publisher

[![Tests](https://github.com/tmccc/optimise-tesla-tariff-publisher/actions/workflows/tests.yml/badge.svg)](https://github.com/tmccc/optimise-tesla-tariff-publisher/actions/workflows/tests.yml)

Experimental Python tools for turning Home Assistant Optimise price sensors into
paired Tesla Powerwall import/export tariff proposals, with validation, readback
checks and a guarded Self-Powered fallback.

This is a standalone experimental controller. The separate
[E.ON Next Optimise Home Assistant integration](https://github.com/tmccc/ha-eon-next-optimise)
provides the source readings. This repository does not install a HACS component.
It includes an optional recurring publisher, an independent watchdog and user
service templates for startup after reboot. Installing the code starts nothing;
live execution and design approvals default to **off**.

## Start with the offline example

Use Python 3.11 or newer on Linux/POSIX. The offline compiler and tests use the
standard library; operational HTTPS children additionally require `aiohttp`.

```sh
git clone https://github.com/tmccc/optimise-tesla-tariff-publisher.git
cd optimise-tesla-tariff-publisher
python3 -m unittest discover -s tests -v

python3 prepare.py \
  --input examples/price-snapshot.json \
  --baseline examples/baseline-tariff.json \
  --as-of 2025-01-15T12:05:00+00:00
```

The examples contain synthetic data. `--as-of` explicitly replays that old fixture;
live validation uses the real clock. Preparation writes a private assessment and
proposals, without sending Tesla controls. Output/state belongs in the configured
private state directory, outside version control.

## What is implemented

- Exact decimal conversion from p/kWh to GBP/kWh; export earnings are not negated again.
- Interval pairing, freshness, gaps, overlaps, DST and year-boundary checks.
- Date-specific annual seasons, keeping tomorrow's quoted prices on tomorrow's date.
- Normalisation of flattened GET tariffs and documented POST wrappers.
- Semantic price hashes and independent comparisons of both quoted channels.
- An explicitly approved frozen-current-pair assumption for unquoted periods.
- Separate tariff and fallback policies; site/hash pins, short leases, ownership
  records, an independent watchdog and an at-most-once attempt journal.
- A supervised one-shot sender and an explicitly armed recurring controller.
- Paired import/export updates when quotes change, with no-POST lease renewal
  when the installed tariff still matches fresh quotes.
- A Self-Powered expiry fallback and separately approved Savings enable/resume,
  with persistent pauses for manual changes and Optimise activation.

Core planning functions are pure. Tests use synthetic fixtures and fake transports.
The public copy uses explicit credential files and native Python HTTPS children;
it does not extract authentication from Home Assistant storage or require Docker.

## Optional live configuration

```sh
python3 -m pip install -r requirements-live.txt
```

Supply credential **file paths**, never token values in command-line arguments,
Git, screenshots or issue reports:

| Variable | Purpose |
| --- | --- |
| `OPTIMISE_TESLA_STATE_DIR` | Private runtime state, policies and reports |
| `HASS_URL` | Home Assistant REST endpoint; defaults to loopback |
| `HASS_TOKEN_FILE` | Explicit Home Assistant bearer-token file |
| `TESSIE_TOKEN_FILE` | Explicit Tessie token file |
| `TESLA_ENERGY_SITE_ID` | Optional explicit site selection for readback; live commands also require pinned site IDs |
| `HASS_ALLOW_STATUS_REFRESH` | Set to `1` only to permit the explicit enrolment-telemetry refresh |

Token files must be regular, owned by the current user, mode `0600`, and not
symbolic links. Examples include paths only. Authentication values stay inside
the processes making the requests and are not returned in diagnostics.

Configure the HA entity mapping for your installation:

| Variable | Default entity |
| --- | --- |
| `HASS_IMPORT_PRICE_ENTITY` | `sensor.optimise_import_price` |
| `HASS_EXPORT_PRICE_ENTITY` | `sensor.optimise_export_price` |
| `HASS_PRICES_LAST_SUCCESS_ENTITY` | `sensor.optimise_prices_last_successful_update` |
| `HASS_BATTERY_ENROLLED_ENTITY` | `binary_sensor.optimise_battery_enrolled` |
| `HASS_STATUS_LAST_SUCCESS_ENTITY` | `sensor.optimise_status_last_successful_update` |

See `python3 prepare.py --help` and the reader/config modules for the exact input
contract. A deliberately enabled telemetry refresh may call
HA's `update_entity` service; this does not grant battery permissions or alter
Optimise enrolment.

Active-mode verification currently requires an existing read-only gateway API at
`http://127.0.0.1:8675/api/operation`. It must report a non-stale canonical mode.
The reader does not create that service or change its configuration. Observation
time is distinguished from the gateway's unavailable precise polling timestamp.
Cloud default-mode readback can lag the active state, so it is retained separately.

The `examples/*.example.json` policy files have all approvals and live flags
disabled, and null account/hash/approval pins. Actual policy files belong in
`OPTIMISE_TESLA_STATE_DIR`, not the source checkout. They must also be owned by
the current user, regular non-symlink files with mode `0600`. Merely installing or importing this code
does not arm a controller. The one-shot sender requires a separately running,
healthy, armed watchdog and explicit operator approval. Its short test lease is
at most three minutes; the watchdog normally acts on the next polling cycle.
The one-shot path never returns to Savings automatically. Recurring Savings
management requires a separate explicit grant and a fresh verified lease.

The command transports currently require a single eligible battery energy site
on the account. `TESLA_ENERGY_SITE_ID` can select a readback site; it does not
remove that live-command limitation. Ambiguous assets are refused.

Preparation does not inherit design consent. `--design-approvals PATH` accepts
explicit choices; omitting it leaves those choices unapproved. It never arms
either live policy. Recurring setup additionally requires its own explicit
controller policy; a one-shot approval does not enable recurring execution.

## Recurring operation

Read [the controller and recovery notes](docs/recurring-operation.md) before
arming recurring execution. The publisher checks every two minutes; the separate
watchdog monitors the currently installed tariff's short lease. A running process
alone is insufficient: every publication and Savings enable requires fresh
prices, a healthy independent watchdog, actual gateway mode, permissions,
ownership and confirmed Optimise non-enrolment.

The [user-service deployment guide](docs/recurring-services.md) describes the
user-local release, virtual environment and configuration layout, startup after
reboot, status checks and safe stopping order. These services expose no browser
or management listener and do not require the Android emulator.

## Important operational limits

- Tesla's documented API adjusts negative prices and import prices below export
  prices. This code blocks incompatible exact mirrors rather than silently
  changing the supplier's quotes.
- Tesla requires complete repeating tariff schedules. A rolling forecast is not
  an annual price quotation. Frozen prices outside the quote window are an
  **assumption**, requiring explicit approval, and can affect optimiser lookahead.
- A Tesla tariff has no documented TTL. Stopping uploads does not expire it.
  A failed host or network cannot guarantee that a fallback command reaches Tesla.
- A successful HTTP status alone does not create verified ownership. A durable
  before/after intent, protected settings and two distinct matching readbacks
  are needed to resolve a recurring write whose application acknowledgement is
  unknown. The record retains that uncertainty; an ambiguous POST is not resent.
- Enrolled or unknown Optimise control, stale observations, changed tariffs or
  unexpected settings pause the controller instead of overriding another owner.
- Gateway settings omitted by a provider remain explicitly unknown. Reported
  representation continuity is not a claim that an unreported setting was measured.
- Source readings remain forecasts, not independently verified settlement or
  billing records. No VAT, margins or historical prices are invented.

The original prototype was exercised in supervised testing. Offline tests cover
the compiler, transports, recurring decisions and crash recovery. Validate the
complete deployed setup against its real inputs before live use; these checks
do not make this an unattended production controller.

## Development and boundaries

```sh
python3 -m unittest discover -s tests -v
```

GitHub Actions runs the offline suite on Python 3.11–3.14 with read-only repository
permissions. No account secrets or live network credentials are needed by CI.

Runtime state, actual policy files, logs, backups, account identifiers, receipts,
screenshots and live test reports are excluded from this repository. Please use
synthetic fixtures when reporting an issue. No vendor APKs or account traces are
bundled. This project is not affiliated with E.ON Next, Amber, Tesla or Tessie.

## References

- [Tesla Fleet API energy endpoints](https://developer.tesla.com/docs/fleet-api/endpoints/energy)
- [Tessie Fleet API mirror](https://developer.tessie.com/reference/access-tesla-fleet-api)
- [Home Assistant REST API](https://contributors.home-assistant.io/core/internals/rest-api/)

MIT licence; see [LICENSE](LICENSE).
