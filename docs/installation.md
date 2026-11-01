# Install the experimental recurring publisher

This walkthrough installs the publisher as a normal Linux user, validates its
inputs without controls, then describes a separately authorised first live run.
It targets the `v0.1.0a1` prerelease and the paths used by the supplied user
services. It is a source-checked procedure; a clean-machine installation has
not been independently exercised on every supported environment.

The publisher is a standalone Python controller. The separate
[Optimise HA integration](https://github.com/tmccc/ha-eon-next-optimise) supplies
prices and enrolment data. HACS installs that integration, not this publisher.

## 1. Check the prerequisites

You need:

- Linux/POSIX with Python 3.11 or newer, `venv`, Git and a working user systemd
  manager for the optional persistent services. No desktop or emulator is needed.
- The Optimise integration configured in Home Assistant, with fresh import and
  export prices, successful-fetch timestamps and explicit battery-enrolment data.
- A Tesla account with exactly one eligible battery energy site. Commands refuse
  ambiguous battery sites, even when a readback site ID is configured.
- A Tessie access token whose connected Tesla account permits energy data and
  energy commands. Obtain it through
  [Tessie's developer settings](https://developer.tessie.com/reference/authentication).
  This project does not obtain or renew Tesla credentials itself.
- An existing **local gateway/TEDAPI-backed** reader serving the contract below.
  Tesla's saved cloud mode can lag the gateway, so cloud mode alone is insufficient.

The supported tariff subset is GBP, explicit import/export prices, all-week clock
schedules and zero fixed/demand charges. Negative prices or import prices below
export prices block exact publication; Tesla's API would otherwise adjust them.
Do not run a second independent battery controller alongside this one. An
unknown or active E.ON battery-control state pauses publication rather than
being treated as permission to take over.

### Local gateway dependency

The publisher reads exactly `http://127.0.0.1:8675/api/operation`. It does not
install a gateway reader, accept a configurable alternative URL, or create HA
gateway entities. [pyPowerwall-server](https://github.com/jasonacox/pypowerwall-server)
is the upstream project behind the validated deployment's locally adapted reader.
This prerelease does not distribute that reader or establish stock-version
compatibility across gateway hardware. Follow its own setup
instructions for the appropriate hardware and release, with the API bound to
loopback. Keep its optional control and battery-dispatch features disabled for
this dependency; the publisher uses only a GET from it.

For the upstream server, `PW_BIND_ADDRESS=127.0.0.1` and `PW_PORT=8675`
configure the HTTP listener. Its `PW_HOST`/`--host` option names the **gateway**,
not the HTTP bind address. Do not copy an all-interface or host-network example
without adapting the listener. Leave `PW_CONTROL_SECRET` and MQTT control
features unset/disabled for this reader, and verify local TEDAPI provenance
instead of relying on an automatic cloud fallback. Establishing or commissioning
the gateway's physical network/RSA access is hardware-specific and outside this
publisher installer.

A server name or HTTP 200 is not enough. Confirm that this endpoint is sourced
from the local gateway's actual mode, not a cloud/hybrid saved default, and that
it returns the required fields. Basic LAN-only or unsupported gateway modes may
not expose them. Do not enable the publisher until the reader is available.

Required response shape, with illustrative values only:

```json
{
  "stale": false,
  "real_mode": "self_consumption",
  "backup_reserve_percent": 20,
  "grid_export": "battery_ok",
  "grid_charging": true
}
```

`real_mode` must be `autonomous`, `self_consumption` or `backup`; reserve must be
a finite number from 0 to 100. `grid_export` must be `battery_ok`, `pv_only` or
`never`. `grid_charging` may be omitted/null and is then explicitly
`not_reported`. The reader must report `stale: false`. The HTTP observation time
is not proof of the age of the reader's underlying device poll.

## 2. Install a pinned source release and Python environment

Use a fresh Bash shell and stop on any command failure. The `set -eu` below
terminates that shell on failure or an undefined variable. Inspect existing files
before reusing these paths; do not overwrite an existing deployment or its state.
The commands below do not install, enable or start a service.

```sh
set -eu
umask 077
publisher_base="$HOME/.local/lib/optimise-tesla-tariff-publisher"
publisher_release="$publisher_base/releases/v0.1.0a1"
publisher_config="$HOME/.config/optimise-tesla-tariff-publisher"
publisher_state="$HOME/.local/state/optimise-tesla-tariff-publisher"

python3 --version
git --version
test ! -e "$publisher_release"
test ! -e "$publisher_base/current"
test ! -L "$publisher_base/current"
test ! -e "$publisher_base/venv"
test ! -e "$publisher_config/environment"
test ! -e "$publisher_state"

install -d -m 0700 "$publisher_base/releases" "$publisher_config" "$publisher_state"
git clone --branch v0.1.0a1 --depth 1 \
  https://github.com/tmccc/optimise-tesla-tariff-publisher.git \
  "$publisher_release"
git -C "$publisher_release" rev-parse HEAD
python3 -m venv "$publisher_base/venv"
"$publisher_base/venv/bin/python" -m pip install \
  --index-url https://pypi.org/simple \
  -r "$publisher_release/requirements-live.txt"
ln -s "$publisher_release" "$publisher_base/current"
cd "$publisher_base/current"
```

Record the release commit printed above alongside the release page. This is a
prerelease pin, not a claim that the tag has a cryptographic signature. If your
OS lacks Git or venv support, install only the missing prerequisites with its
package manager; on Ubuntu those packages are `git` and `python3-venv`.
The publisher and its services run without sudo.

For an offline check before supplying credentials:

```sh
"$publisher_base/venv/bin/python" -m unittest discover -s tests -q
"$publisher_base/venv/bin/python" prepare.py \
  --input examples/price-snapshot.json \
  --baseline examples/baseline-tariff.json \
  --as-of 2025-01-15T12:05:00+00:00 \
  --output-dir "$publisher_state/offline-example"
```

The fixture is synthetic. The historical `--as-of` is only an explicit replay;
live checks always use the current clock.

With the reader running, this credential-free check verifies the publisher's
own gateway-response validation and prints only mode/settings observations:

```sh
"$publisher_base/venv/bin/python" - <<'PY'
import json
from active_mode import read_active_mode

observation = read_active_mode()
print(json.dumps({key: observation[key] for key in
    ('mode', 'mode_source', 'hardware_preserve_settings', 'gateway_reported_stale')}, indent=2))
PY
```

If it fails, correct the reader's local source and response contract before
proceeding. Do not replace its unavailable state with invented values.

## 3. Create private token files and configure the entity mapping

Create a named Home Assistant long-lived access token in your profile under
**Security**, as documented by
[Home Assistant](https://www.home-assistant.io/docs/authentication/#managing-account-access).
Use a distinct service token so it can be revoked independently. It is a normal
HA token, not a provider-issued read-only credential. Keep both it and the Tessie
token outside the source checkout.

This interactive snippet writes new files with mode `0600` and never echoes
their values. It refuses to overwrite existing files. Run it in your own terminal,
not in a shared transcript or a command with tokens pasted into arguments.

```sh
"$publisher_base/venv/bin/python" - <<'PY'
from getpass import getpass
import os
from pathlib import Path

directory = Path.home() / '.config' / 'optimise-tesla-tariff-publisher'
for name, label in [('ha-token', 'Home Assistant token'),
                    ('tessie-token', 'Tessie access token')]:
    value = getpass(label + ': ').strip()
    if not value or any(character.isspace() for character in value):
        raise SystemExit('Empty or malformed token; no value printed')
    fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as handle:
        handle.write(value + '\n')
PY
```

Create the dedicated environment file from the example:

```sh
install -m 0600 examples/environment.example "$publisher_config/environment"
```

Edit that file locally. Set **literal absolute paths** for token files, the
private state directory and permission-cache file; systemd does not expand
`$HOME` or shell variables inside `EnvironmentFile`. The file contains paths and
configuration, never token values. A same-host HA instance can use
`HASS_URL=http://127.0.0.1:8123`. Plain HTTP to a non-loopback HA address is refused;
use a normally validated HTTPS endpoint when HA is remote.

Map the five exact entity IDs in your own HA installation:

| Variable | Example entity |
| --- | --- |
| `HASS_IMPORT_PRICE_ENTITY` | `sensor.optimise_import_price` |
| `HASS_EXPORT_PRICE_ENTITY` | `sensor.optimise_export_price` |
| `HASS_PRICES_LAST_SUCCESS_ENTITY` | `sensor.optimise_prices_last_successful_update` |
| `HASS_BATTERY_ENROLLED_ENTITY` | `binary_sensor.optimise_battery_enrolled` |
| `HASS_STATUS_LAST_SUCCESS_ENTITY` | `sensor.optimise_status_last_successful_update` |

The price inputs must be the integration's p/kWh sensors with currency `GBP` and
their period/forecast metadata. A price remaining unchanged is not a failure;
use the successful-update timestamp. Enrolment `off` must be backed by its fresh
successful-status timestamp, not the binary sensor's `last_changed` time.

Set `TESSIE_SCOPE_CACHE_FILE` to a file directly in the private state directory,
for example `.../.local/state/optimise-tesla-tariff-publisher/tessie-scope-cache.json`.
Do not put it in a new `output` directory: `prepare.py` must claim that output
directory with its own marker before anything else makes it nonempty.
It reduces repeated permission lookups, expires after 300 seconds, and invalidates
when the token file changes. Price, tariff, mode and protected-setting reads
remain fresh. Set `HASS_ALLOW_STATUS_REFRESH=1` to permit the enrolment-only HA
telemetry refresh required immediately before control attempts; this does not
enrol a device or change E.ON permissions.

Load only this path-only file into the current terminal:

```sh
set -a
. "$publisher_config/environment"
set +a
```

Changing this terminal environment does not configure or start the user services;
the service files will load the same dedicated environment file themselves.

## 4. Initialise disabled policies and obtain a read-only backup

For a fresh state directory only:

```sh
test ! -e "$publisher_state/execution-policy.json"
test ! -e "$publisher_state/publisher-policy.json"
test ! -e "$publisher_state/recurring-policy.json"
install -m 0600 examples/execution-policy.example.json "$publisher_state/execution-policy.json"
install -m 0600 examples/publisher-policy.example.json "$publisher_state/publisher-policy.json"
install -m 0600 examples/recurring-policy.example.json "$publisher_state/recurring-policy.json"
"$publisher_base/venv/bin/python" prepare.py --live
```

`prepare.py --live` makes configured HA, Tessie and gateway reads. It sends no
Tesla control request, grants no ownership and arms no policy. Its private output
includes `baseline-backup.json`, `readiness.json`, `guard-input.json`, a price
preview and candidate tariff files.

Preserve the original tariff and settings before changing operating mode. These
copies are private evidence, not a verified restoration command:

```sh
test ! -e "$publisher_config/original-tariff-backup.json"
test ! -e "$publisher_config/original-readiness.json"
install -m 0600 "$publisher_state/output/baseline-backup.json" \
  "$publisher_config/original-tariff-backup.json"
install -m 0600 "$publisher_state/output/readiness.json" \
  "$publisher_config/original-readiness.json"
```

Inspect those files locally. Confirm the intended battery site, existing tariff,
reserve and permissions. `baseline-backup.json` contains the selected numeric
`energy_site_id`; set `TESLA_ENERGY_SITE_ID` in the environment file to that
intended site and reload the environment. Ambiguous accounts must be resolved
before continuing. Do not publish these private files or HA backups.

For this initial bootstrap, choose **Self-Powered** yourself in the Tesla app and
confirm the local gateway follows it. Leave reserve, grid charging, export
permission and E.ON/Amber authorisation unchanged. Then rerun:

```sh
"$publisher_base/venv/bin/python" prepare.py --live
```

The report deliberately always says `ready_for_upload: false` and includes
unarmed/watchdog/ownership blockers. Do not edit those report flags. Before
activation, require the real observations below:

- Prices valid and fresh, candidate valid, all paired quoted prices exact.
- Backup integrity verified, with the supported tariff schema and no candidate
  compatibility errors.
- Explicit fresh `optimise_battery_enrolled: false` and energy-command permission.
- Actual gateway `self_consumption`, known reserve/export representation and the
  intended site. A cloud saved mode alone is not sufficient.

## 5. Pin the fresh baseline without inventing ownership

The recurring bootstrap needs a deliberately approved baseline. The backup's
`sha256` is a **canonical JSON hash**; it is not the semantic hash used by the
controller. This snippet derives the correct hash, checks that the newly compiled
candidate differs, and updates only disabled private policy pins:

```sh
"$publisher_base/venv/bin/python" - <<'PY'
from config import STATE_DIR
from prepare import private_json
from runtime import private_document
from tariff_schema import semantic_hash

output = STATE_DIR / 'output'
backup = private_document(output / 'baseline-backup.json')
report = private_document(output / 'readiness.json')
guard = private_document(output / 'guard-input.json')
proposal = private_document(output / 'frozen_current_quote-proposal.json')
baseline = semantic_hash(backup['tariff_rate_response'])
candidate = semantic_hash(proposal['tou_settings']['tariff_content_v2'])
assert report['prices']['valid'] and report['candidate_comparison']['all_exact']
assert report['candidates']['frozen_current_quote']['candidate_valid']
assert report['optimise_battery_enrolled'] is False
assert guard['energy_commands_granted'] is True
assert guard['mode'] == 'self_consumption'
assert candidate != baseline, 'No distinct initial transition to prove; stop for review'
site = backup['energy_site_id']
assert type(site) is int and site > 0
for name in ('publisher-policy.json', 'execution-policy.json', 'recurring-policy.json'):
    path = STATE_DIR / name
    policy = private_document(path)
    assert policy.get('live_execution_enabled') is not True
    assert policy.get('enabled') is not True
    policy['energy_site_id'] = site
    if name == 'recurring-policy.json':
        policy['bootstrap_baseline_hash'] = baseline
    private_json(path, policy)
print('Disabled policies pinned to the inspected baseline; no control request sent.')
PY
```

If the candidate is already identical to the unowned baseline, stop. Matching
prices do not establish publisher ownership. Do not fabricate an ownership file,
lease or journal, alter real prices to force a difference, or delete an existing
pending intent to make the controller proceed.

## 6. Review the live grants before enabling anything

The following edits grant control when services are subsequently started. Decide
whether you accept a Self-Powered expiry fallback, using the observed current
price pair outside the rolling quote window, Savings enablement and optional
automatic return after the controller's own verified fallback. Those assumed
annual prices can affect Tesla's lookahead and are not future Optimise quotes.

Edit the three private JSON policy files, retaining the pins from step 5:

| File | Required explicit changes for this recurring bootstrap |
| --- | --- |
| `execution-policy.json` | `fallback_design_approved: true`, `live_execution_enabled: true`, `allowed_mode: "self_consumption"`, `allow_tariff_writes: false`, `recurring_enabled: true`, `allow_savings_enable: true` |
| `publisher-policy.json` | `live_execution_enabled: true`, `allow_tariff_writes: true`, `one_shot_only: false`, `recurring_enabled: true`, `unquoted_policy: "frozen_current_quote"`, `unquoted_planning_assumption_approved: true`, `fallback_design_approved: true`, `fallback_policy: "self_powered"` |
| `recurring-policy.json` | Keep `enabled: false` initially. Set a new nonempty `generation` string (maximum 128 characters), `manage_savings: true`, `bootstrap_from_self_powered: true`, `fallback_approved: true`, `frozen_assumption_approved: true`. Set `resume_after_own_fallback` only to the choice you explicitly approve. |

All three need the same correct integer `energy_site_id`. The coordinator pins
`approved_candidate_semantic_hash` and `expected_installed_semantic_hash` itself
under its control lock before each authorised write; do not copy a stale example
hash into them. Keep one approval generation through first-write/mode
reconciliation. Preserve the existing permission/reserve safeguards in the
execution example. The fallback file must continue to forbid tariff writes.

## 7. Start the watchdog first, then supervise the publisher

Install the units from the pinned release, using the paths above:

```sh
test ! -e "$HOME/.config/systemd/user/optimise-tesla-watchdog.service"
test ! -e "$HOME/.config/systemd/user/optimise-tesla-publisher.service"
mkdir -p "$HOME/.config/systemd/user"
install -m 0644 systemd/optimise-tesla-watchdog.service "$HOME/.config/systemd/user/"
install -m 0644 systemd/optimise-tesla-publisher.service "$HOME/.config/systemd/user/"
systemd-analyze --user verify \
  "$HOME/.config/systemd/user/optimise-tesla-watchdog.service" \
  "$HOME/.config/systemd/user/optimise-tesla-publisher.service"
systemctl --user daemon-reload
systemctl --user start optimise-tesla-watchdog.service
journalctl --user -u optimise-tesla-watchdog.service -n 20 --no-pager
```

For this fresh bootstrap, require `waiting_for_publisher_state` with no errors.
An enrolment-unverified or persistent-pause status needs investigation. Check the private
`output/watchdog-health.json`: it must describe a separate live PID, continuous
operation, live execution enabled, and a successful cycle less than 90 seconds
old. No ownership or safety lease should have been manufactured. A running
process alone is insufficient; the publisher repeats these readiness checks.

When the observations and grants remain correct, make the final explicit edit
`enabled: true` in `recurring-policy.json`, then start the publisher:

```sh
systemctl --user start optimise-tesla-publisher.service
journalctl --user -u optimise-tesla-publisher.service \
  -u optimise-tesla-watchdog.service -n 60 --no-pager
```

This is the first step that can replace both Tesla tariffs and enable Savings.
The publisher compiles fresh inputs, rechecks the pinned Self-Powered baseline,
writes one paired tariff, and requires independent matching readbacks before
committing ownership and a short lease. Savings enablement is separate and must
be confirmed through the gateway. HTTP 200 alone is not proof of application.

Check `output/recurring-status.json`, `publisher-ownership.json`,
`publisher-lease.json` and `recurring-journal.json`. Require fresh verified
ownership, an unexpired lease of at most five minutes, healthy watchdog, no
unresolved tariff/mode intent and no pause latch. The actual gateway must report
`autonomous` before claiming Savings is active. Subsequent unchanged-price
cycles should renew the lease without another tariff POST. Changed valid quotes
can produce a new verified publication.

An unverified outcome must be reconciled, not resent. See
[readback evidence and its limits](tesla-tariff-readback-evidence.md) and
[controller recovery](recurring-operation.md). A stored tariff readback does not
independently prove which price view Tesla's planning engine consumed. Export
dispatch and financial savings remain unverified outcomes of this prerelease.

## 8. Validate recovery, then enable startup after reboot

While you can supervise the system, stop only the publisher and leave the
watchdog running. With the publisher's owned tariff still installed, fresh
confirmed E.ON non-enrolment and an expired trusted lease, verify the gateway
actually reaches Self-Powered. A cloud saved mode may lag. Then start the
publisher again and verify the recovery behaviour you approved. A persistent
manual/E.ON pause is an intentional handover, not something to clear automatically.

Once that check is satisfactory:

```sh
systemctl --user enable optimise-tesla-watchdog.service optimise-tesla-publisher.service
systemctl --user is-enabled optimise-tesla-watchdog.service optimise-tesla-publisher.service
loginctl show-user "$USER" -p Linger
```

If lingering is not already enabled, an administrator must explicitly enable it
for this account; see [user-service startup](recurring-services.md). Enabling
units and verifying lingering configures boot startup. It does not constitute a
successful host-reboot test. HA/network/gateway startup delays are handled by
pausing until genuine observations are available.

The publisher polls every two minutes; the watchdog checks every minute. Service
crashes restart after 15 seconds. State stays outside the versioned code so
process restarts retain ownership, uncertain intents and manual/E.ON latches.

## Stopping, upgrading and troubleshooting

- While the publisher still owns the tariff and E.ON is confirmed unenrolled,
  stop the publisher first and let the watchdog handle the trusted lease. Confirm
  actual Self-Powered before stopping the watchdog. For an E.ON handover, respect
  the handover latch and leave E.ON's operating mode under its control instead
  of forcing Self-Powered. Disabling future startup is a separate
  `systemctl --user disable` action.
- Preserve state, policies and credentials during upgrades. Install a new
  versioned release separately; change `current` only after a controlled stop.
  Do not overwrite a running release or clear a journal to bypass a pause.
- `private_cycle_error_suppressed` means the cycle failed closed; inspect the
  private observations and source availability locally. Never paste tokens,
  account IDs, unredacted state or HA backups into an issue.
- Scope errors require the correct Tessie permissions/token. Rate limits require
  cooling down; an expired permission cache does not become an assumed grant.
- Unavailable/stale prices, unknown enrolment or gateway observations are inputs
  to investigate, not flags to force true. E.ON enrolment requests handover;
  another tariff or manual mode change can create a persistent review latch.
- Optional [HA alerts](home-assistant-alerts.md) observe price failures, mode and
  net export. They do not replace a publisher heartbeat or an off-host watchdog.

A host or Internet outage can prevent the fallback command from arriving, and
Tesla tariffs have no publisher-enforced expiry on the battery. Read the
[operational limits](../README.md#important-operational-limits) before relying on
this experimental controller.
