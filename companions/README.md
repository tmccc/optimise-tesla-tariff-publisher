# Optional read-only reporting companions

These experimental programs report prices, publisher health and one bounded
energy-observation cycle. They are separate from the armed tariff publisher.
They never send Tesla controls, change publisher policy or call HA services;
their HA writes are restricted to their own sensor states. Nothing is installed
or enabled by checking out this directory.

The examples contain demonstration dates and placeholder paths. They are not
evidence of offer eligibility, recommended tariff products or ready-to-run live
configuration. Keep real configuration, credentials, SQLite databases, captured
quotes and generated reports outside Git.

## Components

- `customer_pricing/customer_pricing.py` applies an explicitly confirmed,
  date-bounded personal offer over preserved supplier rates. UK clock windows
  follow GMT/BST. Raw and effective rates stay separate, missing prices remain
  missing, and three pence is not multiplied by VAT again. Confirm whether the
  supplier feed already includes the offer before enabling another adjustment.
- `price_comparison/collector.py` compares Optimise with Octopus Agile import and
  export prices and wholesale indices. This version selects the **North West
  region, G**. Supply the desired Octopus product identifiers explicitly; the
  sample does not choose products for another installation. Existing raw scalar
  histories retain their basis; new effective scalar histories begin when this
  companion first publishes them.
- `publisher_observer/observer.py` reads local publisher records, gateway power
  and storage data, and HA prices. It integrates bounded directional power
  samples, exposes missing coverage, freezes ready window results and preserves
  original results when adding a customer-adjusted revision. Its optional
  Self-Powered comparison is a conditional model, not proven controller savings.

The observer expects compatible read-only local gateway endpoints on
`127.0.0.1:8675` and freshness metadata on `127.0.0.1:8677`. Those services are
external prerequisites, not supplied or reconfigured here. HA access is limited
to `http://127.0.0.1:8123`. The publisher status directory defaults to
`~/.local/state/optimise-tesla-tariff-publisher/output`.

The billing overlay is deliberately distinct from the Tesla planning
approximation. True overnight export earnings must not be reduced simply
because Tesla's planning tariff uses a lower synthetic overnight sell rate.

The [native health view](../examples/publisher-health-view.example.yaml) and
[result-alert package](../examples/cycle-result-alerts.example.yaml) are generic
starting points. Adjust the dashboard URL and notification action for your own
installation. They are not installed automatically. The result alerts retain a
last-window key after notification acceptance, retry while unsent and use a
stable mobile tag; a delivery accepted immediately before an HA crash can still
be retried, so this is not a guarantee of exactly-once physical delivery.

## Run and test

From the repository root, make the three source directories importable:

```sh
export PYTHONPATH="$PWD/companions/customer_pricing:$PWD/companions/price_comparison:$PWD/companions/publisher_observer"
python3 companions/price_comparison/collector.py --config /absolute/private/path/comparison.json
python3 companions/publisher_observer/observer.py --config /absolute/private/path/observation.json
```

Use copies of the `config.example.json` files with real private paths and actual
HA entity IDs. Observation dates must be deliberately configured; the example
dates are historical. `--watch` keeps either program running, but creating a
persistent service is a separate installation decision. Both programs reject a
duplicate process using their private state-directory lock. They open no new
listening network interfaces.

Set `customer_pricing_file` to an absolute private policy path only after the
account holder has confirmed the amounts, windows, eligibility dates and that
the original feed excludes them. Leaving it `null` preserves original API
prices. The shared module can also be installed under
`~/.local/lib/optimise-customer-pricing`.

The tests are offline: HTTP calls, credentials and live HA writes are mocked or
rejected. Run each group from the repository root:

```sh
python3 -m unittest discover -s companions/customer_pricing -p 'test_*.py'
python3 -m unittest discover -s companions/price_comparison -p 'test_*.py'
python3 -m unittest discover -s companions/publisher_observer -p 'test_*.py'
```

The standard library `tempfile` module honors `TMPDIR` for scratch data. The
companion arithmetic and runtime sources need no additional Python packages;
they use the host's `Europe/London` timezone database.
