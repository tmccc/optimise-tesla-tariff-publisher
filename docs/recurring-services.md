# Recurring user services

The two sample units run the publisher and its independent watchdog as the
current user. They need no root runtime, expose no listener, and do not manage
Home Assistant, Docker or the local gateway service.

## Deployment layout

Install a tested release and its Python environment before installing the units:

```text
~/.local/lib/optimise-tesla-tariff-publisher/
    current/       # Tested source release, or a link to an immutable release
    venv/          # Python environment with the optional live dependencies
~/.config/optimise-tesla-tariff-publisher/environment
~/.local/state/optimise-tesla-tariff-publisher/
```

Keep the deployed release on storage available when the user manager starts.
The research checkout is a development source, rather than a boot dependency.
Do not change `current` while either service is executing a control transaction.

The environment file contains configuration and absolute credential-file paths,
not credential values. Systemd does not perform shell variable expansion inside
this file. For example:

```ini
HASS_URL=http://127.0.0.1:8123
HASS_TOKEN_FILE=/absolute/private/path/home-assistant-token
TESSIE_TOKEN_FILE=/absolute/private/path/tessie-token
HASS_ALLOW_STATUS_REFRESH=1
```

Add the installation's explicit HA entity mappings described in the main README.
Protect the environment, token and actual policy files with mode `0600`; the
runtime state directory should be owned by this user and have mode `0700`.
Disabled example policies do not constitute live authorisation.

## Startup and readiness

`After=optimise-tesla-watchdog.service` orders process startup. With `Type=simple`,
it does **not** establish that the watchdog has completed a successful cycle.
The publisher must verify a fresh heartbeat from a separate, armed watchdog,
fresh prices, current account permissions, current Optimise enrolment, actual
gateway mode and ownership before publishing or resuming Savings.

The user units intentionally do not depend on a system-level `docker.service` or
assume that `network-online.target` in the system manager makes their inputs
ready. HA, gateway or network unavailability must be handled by the application's
bounded retries and existing controller policy. A failed dependency must never
be treated as proof that Optimise is inactive.

The publisher wants the watchdog without binding their lifetimes together.
If the watchdog crashes, systemd restarts it; new publication remains gated by
its real heartbeat. Both units restart after unexpected process exit, with a
15-second delay. These units do not use `WatchdogSec`: their JSON heartbeat is
an application protocol, not an `sd_notify` watchdog heartbeat.

For startup without an interactive login, the user manager needs lingering:

```sh
loginctl show-user "$USER" -p Linger
```

If it reports `Linger=no`, an administrator must enable it for this account:

```sh
loginctl enable-linger USERNAME
```

This is an explicit administration step; the application does not run sudo or
change lingering itself. Do not restart or reset the whole user manager merely
because unrelated user units are degraded.

## Install and enable

After configuration, live approval and initial validation:

```sh
mkdir -p "$HOME/.config/systemd/user"
install -m 0644 systemd/optimise-tesla-watchdog.service "$HOME/.config/systemd/user/"
install -m 0644 systemd/optimise-tesla-publisher.service "$HOME/.config/systemd/user/"
systemd-analyze --user verify \
  "$HOME/.config/systemd/user/optimise-tesla-watchdog.service" \
  "$HOME/.config/systemd/user/optimise-tesla-publisher.service"
systemctl --user daemon-reload
systemctl --user enable --now optimise-tesla-watchdog.service optimise-tesla-publisher.service
```

Check both process state and application state:

```sh
systemctl --user status optimise-tesla-watchdog.service optimise-tesla-publisher.service
systemctl --user is-enabled optimise-tesla-watchdog.service optimise-tesla-publisher.service
journalctl --user -u optimise-tesla-publisher.service -u optimise-tesla-watchdog.service -n 50
```

Enabling these units with lingering provides startup on later boots. Validate
recovery using only these services; restarting HA, containers or the whole server
is not needed to test their own restart behaviour.

## Stop and recovery behaviour

To retire recurring control, stop the publisher first and allow the still-running
watchdog to handle the remaining trusted lease under its configured policy:

```sh
systemctl --user stop optimise-tesla-publisher.service
```

Confirm the resulting active gateway mode before stopping the watchdog. Stopping
both processes immediately can remove the mechanism that would otherwise handle
expiry. Disabling future startup is a separate explicit step:

```sh
systemctl --user disable optimise-tesla-publisher.service optimise-tesla-watchdog.service
```

At reboot or process restart, existing ownership and intent records need fresh
reconciliation before any write or return to Savings. Manual mode changes and
Optimise activation must be respected. Systemd restarting a process is not itself
permission to seize control.

A server/network outage can prevent a mode command reaching Tesla. Tariffs have
no documented firmware TTL, and shutdown may interrupt an in-flight request.
The 30-second stop timeout bounds service shutdown, but the durable journal must
retain uncertain outcomes for reconciliation. No service configuration provides
an off-host or atomic cross-provider fallback guarantee.
