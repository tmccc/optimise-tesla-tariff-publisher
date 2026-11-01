# Optional Home Assistant notifications

The [package example](../examples/home-assistant-alerts.example.yaml) adds
notifications for lost Optimise prices, observed Self-Powered operation and
sustained grid export.
It uses the existing Optimise integration and local gateway entities. It does
not call a Powerwall control service or change the publisher's policies.
Installing the publisher does not install this package.

## Behaviour

| Observation | Notification |
| --- | --- |
| Import/export price is nonnumeric, or the successful-update timestamp is unavailable/invalid, for at least two minutes | Price-update failure |
| The last successful price update is more than 15 minutes old | Price-update failure on the next check |
| An update timestamp remains more than one minute in the future for two minutes | Price-update failure |
| Fresh local mode changes from `autonomous` or `backup` to `self_consumption` | Self-Powered alert immediately when HA observes the transition |
| Self-Powered is already present at startup or after telemetry reconnects | Alert after mode and freshness have been stable for two minutes |
| Price inputs are valid and stable for two minutes | Price recovery, if a price incident was previously notified |
| Fresh mode is `autonomous` or `backup` for two minutes | Left Self-Powered, if that incident was previously notified |
| Fresh local net grid export exceeds 200 W continuously for one minute | Export-start alert, including flow and charge readings |
| Net export drops to 50 W or less for two minutes | Silently re-arm the next export-start alert and dismiss its HA notification |

Each incident has a restoring helper and a stable device-notification tag.
An incident creates a Home Assistant persistent notification, dismissed on
recovery. It may also be dismissed manually or lost on HA restart; the incident
helper still restores. The price/mode automations poll once per minute, so a
two-minute stability threshold usually alerts within two to three minutes.
Their startup trigger waits two minutes and then samples
fresh state; triggers arriving during that wait are ignored by `mode: single`.

Unknown mode or stale telemetry never produces a recovery notification. A
transition entirely between gateway polls may not be observed. Price monitoring
checks the provider data in HA, not the phone app's process or screen.

Export monitoring uses the site's net grid power, with **negative kW meaning
export**. Its delayed binary sensor uses separate start/re-arm thresholds to
avoid repeated notifications near zero. The incident helper restores across
restart; an existing session is not announced again merely because HA restarted.
Unknown, nonnumeric, stale or incorrectly scaled data renders the derived sensor
unknown and cannot re-arm it. No export-stop phone notification is sent. Net
export can come from solar, stored battery energy, or both; the message includes
the available battery/solar readings rather than attributing all export to the
battery. The signal is sampled telemetry, not continuous physical measurement.

The price-recovery guard assumes the Optimise integration's five-minute update
cadence. It measures stability using the inputs' `last_changed`, including the
success timestamp. If that timestamp changes every two minutes or faster,
adapt recovery to track a continuously healthy template binary sensor; otherwise
recovery can remain pending despite valid updates. Unchanged price values alone
are never treated as a failed update.

## Adapt before installing

Replace these placeholders throughout the package:

| Placeholder | Required source |
| --- | --- |
| `notify.existing_alert_devices` | Your existing device notification group/service |
| `sensor.optimise_import_price` | Current Optimise import-price sensor |
| `sensor.optimise_export_price` | Current Optimise export-price sensor |
| `sensor.optimise_prices_last_successful_update` | Successful price-fetch timestamp |
| `sensor.powerwall_operating_mode` | Actual local gateway mode, using the canonical values above |
| `binary_sensor.powerwall_data_fresh` | An independent indication that gateway telemetry is fresh |
| `sensor.powerwall_grid_power` | Fresh local net grid power in kW; negative is export |
| `sensor.powerwall_battery_power` | Battery power in kW for the export message |
| `sensor.powerwall_solar_power` | Solar power in kW for the export message |
| `sensor.powerwall_percentage_charged` | Battery charge percentage for the export message |

The price entity defaults match the publisher's example mapping. The gateway
entities are operator-supplied: the publisher's loopback API reader does not
create them in HA. A cloud saved/default mode is unsuitable for this alert
because it can differ from the gateway's active mode.

The example contains no notification group definition, phone/device identifiers,
credentials, account IDs or live state. Its notifier placeholder must be replaced
with a real registered service. Mobile notification-tag behaviour depends on the
notifier. A successful service call does not prove delivery to an offline device;
notifications and helper updates are not an atomic transaction.

## Install using your existing configuration layout

This file is a Home Assistant **package**, containing `input_boolean`, `template`
and `automation` sections. Use your existing package setup or follow HA's
[packages documentation](https://www.home-assistant.io/docs/configuration/packages/).
Do not paste the whole package into `automations.yaml`, which expects only a list
of automation rows.

For a setup that keeps automations and helpers separately, copy the three helper
definitions into the appropriate helper configuration and append only the
`automation` list entries to the existing automation list. UI-created helpers
with the same entity IDs can be used instead of the YAML helpers. Keep helper
`initial` omitted so existing incident state can restore after restart.
Install the template binary sensor in the existing template configuration, or
create an equivalent UI template binary-sensor helper with 60-second on and
120-second off delays. Preserve its state template's `none` result on invalid
data so an outage does not masquerade as stopped export.

Check for existing helpers and automation IDs before adding anything. Do not
install a second copy over an already configured deployment. Use HA's
configuration check, then the reload/restart method appropriate to your HA
version and installation. Installing new YAML integrations/packages may require
a restart; configuring equivalent UI helpers and individually reloading added
automations can avoid restarting an existing installation.

## Validation and limits

The original notification-only deployment was checked with 34 synthetic
scenarios in HA's template engine, automation-schema validation and healthy live
action traces. These included transient failures, stale or unavailable success
timestamps, fresh Self-Powered entry, stale telemetry and both Savings/Backup
recovery. A labelled setup notification exercised its existing device group.
The public package generalises entity/notifier names; validate those mappings
against your own installation before relying on it.

The export extension was checked with ten template cases covering start/re-arm
boundaries, grid-balancing noise, import, hysteresis, unavailable/stale data and
wrong units, plus native message rendering and automation-schema validation.

These alerts observe prices, mode and net export. They are not a publisher-process heartbeat,
an off-host watchdog, or a guarantee of export dispatch. A stopped publisher can
remain unnoticed by price alerts while the independent Optimise feed is healthy;
its expiry fallback may subsequently produce the mode alert. A stopped HA server
cannot execute these notifications.

References: [helper restoration](https://www.home-assistant.io/integrations/input_boolean/),
[automation modes](https://www.home-assistant.io/docs/automation/modes/),
[automation triggers](https://www.home-assistant.io/docs/automation/trigger/).
