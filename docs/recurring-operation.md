# Recurring controller and recovery

The recurring path temporarily supplies Tesla's Savings mode with paired import
and export prices from the separate Home Assistant Optimise integration. It does
not repair Tesla's supplier link, enrol a battery with E.ON, change third-party
permissions, or require the Android emulator. E.ON enrolment remains a separate
connection and takes priority over this temporary controller.

## Decisions and prerequisites

`recurring.py` runs a cycle once, or repeats with `--watch`. A cycle collects fresh
configured HA prices and enrolment states, Tessie tariff/settings/scopes, the
local gateway's actual operating mode and the separate watchdog's heartbeat.
Only the exact supported tariff subset is compiled. Both price channels are
checked against the genuine quote intervals.

With an explicitly enabled private recurring policy:

| Observed state | Controller behaviour |
| --- | --- |
| Owned tariff, changed valid quotes, fresh prerequisites | Persist a pinned intent, then attempt one paired tariff update |
| Owned tariff, same valid quotes | Reverify and renew the short lease; send no tariff POST |
| Verified tariff and live lease, initial approved Self-Powered bootstrap or its own verified fallback | Enable Savings only with separate mode approval and final checks |
| Unusable prices or unhealthy watchdog while running Savings on its own tariff | Invalidate its lease for the independent watchdog's guarded Self-Powered fallback |
| Optimise battery enrolled | Persist a handover pause; publish nothing and send no mode command |
| Unknown/stale enrolment or unavailable observations | Pause; absence of data does not establish non-enrolment |
| External tariff change, manual operating-mode change or conflicting evidence | Persist a manual pause rather than overwrite it |
| Unresolved earlier tariff/mode command | Reconcile evidence; do not send a replacement command |

Automatic recovery from the controller's own verified fallback needs explicit
`resume_after_own_fallback` approval. It cannot treat an ordinary manual
Self-Powered selection as its own fallback. An initial
`bootstrap_from_self_powered` permission is consumed when Savings is verified.
Persisted manual and E.ON pause latches require operator review; fresh readings
alone never clear them.

## Approvals and private state

All actual policies and journals belong in `OPTIMISE_TESLA_STATE_DIR`, outside
Git. The recurring policy is `recurring-policy.json`; the separate transport
policies remain `publisher-policy.json` and `execution-policy.json`. Copying an
example or installing a user service grants no approval.

The recurring tariff policy explicitly combines `recurring_enabled: true` with
`one_shot_only: false`, the existing live flags, the pinned site, and the current
cycle's baseline/candidate hashes. The coordinator pins these hashes immediately
before an attempt while holding the shared control lock. Savings enable has its
own `allow_savings_enable` grant in the fallback policy. That fallback policy
continues to forbid tariff writes; the tariff and mode transports stay separate.

The private output directory retains these distinct records:

- `publisher-ownership.json` describes the verified installed tariff and its
  original publication time.
- `publisher-lease.json` contains the source windows and a trusted short deadline.
  Renewal does not claim another tariff POST or move the original write time.
- `tariff-pending.json` records an attempted baseline-to-candidate transition
  before dispatch, including an uncertain outcome after a crash.
- `mode-intent.json` records an authorised mode transition before dispatch.
- `recurring-journal.json` retains bootstrap consumption, controller actions and
  manual/E.ON pause latches.
- `recurring-status.json` reports the latest completed decision.

Keep these records through process restarts and deployments. Removing a pending
intent to make the process proceed destroys the protection against duplicate
commands. Ownership is never inferred merely because a tariff resembles the
controller's candidate.

## What verification establishes

A tariff POST sends the complete paired import/export schedule to one fixed
Tesla endpoint. If its application acknowledgement is unknown, recovery requires
the saved distinct baseline/candidate intent, HTTP 200 and two ordered, fresh,
matching reads with unchanged protected settings. The recovered record still
states that application acknowledgement was unknown. No positive acknowledgement
is invented and an ambiguous request is not resent.

Mode enable and fallback send exactly one mode field to the fixed operation
endpoint. Immediately before dispatch, the native child waits for the host to
recheck local policy, its durable lease, refreshed E.ON status and actual gateway
mode, and durably record the command intent. The cloud's saved
`default_real_mode` can lag the gateway, so it is recorded separately and cannot
prove that an operating-mode change took effect.

Backup reserve, grid-charging and export permissions are checked for continuity.
An omitted provider field remains `not_reported`, rather than becoming an
invented boolean. The controller never grants energy-command permissions or
changes Amber/E.ON authorisation.

## Optional early-bird planning prices

The recurring policy may include `planning_price_policy`, using the bounded
structure in [the example](../examples/planning-price-policy.example.json).
This option requires explicit operator approval; a personal discount is never
inferred from an account, a product name or a price difference.

The supported `eon_earlybird_planning_v1` approximation subtracts 3p/kWh from
**both** import and export during 00:00–06:00, and adds 3p/kWh to export alone
during 16:00–19:00. Windows and inclusive start/exclusive expiry dates use
`Europe/London`, including GMT/BST changes. This differs from actual customer
prices: the real early-bird overnight offer discounts import alone. Keep billing
and observed-cost calculations on those true prices. The synthetic overnight
sell rate can discourage overnight exports; its financial effect depends on the
energy actually exported during those hours.

Only genuine quoted intervals receive the planning adjustment. The compiler
first fills the existing approved `frozen_current_quote` schedule from **raw**
source prices, then adjusts quoted clock periods. Unquoted future and elapsed
calendar placeholders retain their original raw frozen pair. Applying a bonus
to an equal-price unquoted placeholder could otherwise create a crossed pair
that neither the source nor this compiler supports. These placeholders remain
planning assumptions rather than future or historical quotes.

Raw quote rows, their source timestamps and the raw assumed pair stay in the
manifest. Separate `planning_quoted_intervals`, policy hash, rule dates and
`planning_adjustment_scope: quoted_intervals_only` disclose the projection.
The writer rebuilds the entire candidate from those raw rows and the exact
policy before allowing an upload. Negative or crossed results are still
rejected; neither rule silently clamps them. Already customer-adjusted source
entities are refused to prevent applying the incentive twice.

After a successful matching ownership commit, `planning-status.json` records
the verified planning policy, quote horizon and tariff hash. A proposal alone
cannot create that status. Lease renewal refreshes `verified_at` while retaining
the original `tariff_written_at`; it does not claim another tariff POST.

## Inspection and stopping

Use the service commands in [the deployment guide](recurring-services.md) and
inspect the private status/journal files locally. Reports and service output
contain bounded reason codes and safe acknowledgement metadata. Do not publish
private files: they can contain account identifiers, tariff details and local
paths even though credentials are excluded.

For a controlled stop, stop the publisher first. Let the watchdog handle its
remaining trusted lease, confirm the gateway's actual resulting mode, then stop
the watchdog if retiring the controller. Stopping both immediately removes the
process that would normally handle expiry.

No Tesla tariff TTL is enforced by the battery. A host outage, loss of Internet,
Tesla/Tessie failure or an interrupted shutdown can prevent a fallback command
from arriving. User-service restart and durable reconciliation improve recovery;
they cannot provide an off-host or atomic cross-provider guarantee.
