# energyscraper

> Company:
>
> *"The Pro tier includes advanced diagnostics."*
>
> Engineer:
>
> *"Interesting hypothesis."*

`energyscraper` is a command-line tool for querying Tesla Energy systems using the Tesla Fleet API and the Powerwall device protocol.

It began as a simple way to collect Powerwall metrics for Prometheus, but has grown into a toolkit capable of exposing telemetry that Tesla's public apps don't normally show, including:

- Per-MPPT PV string voltage, current and power
- Powerwall battery telemetry
- AC-coupled solar contribution
- Site power flows (solar, load, battery, grid)
- Vehicle and Wall Connector status

The long-term goal is to export high-resolution metrics into time-series databases such as Prometheus for analysis and visualization.

## Why?

I have a Powerwall 3 installation with four different solar orientations (north, south, east and west) and wanted to understand exactly how each array performs throughout the day and across the seasons.

Tesla's apps provide excellent high-level information, but they intentionally hide much of the underlying telemetry. This project is an attempt to expose that data using the documented Fleet API where possible and the Powerwall's own protocols where appropriate.

## Features

- Tesla Fleet API integration
- Local Powerwall telemetry
- Per-string PV metrics
- Charge-on-Solar aware site metrics
- Prometheus exporter *(coming soon)*
- JSON output for scripting

## Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .
```

Authenticate once with the client ID, client secret, and redirect URI from your Tesla developer app:

```bash
energyscraper auth login \
  --client-id "$TESLA_CLIENT_ID" \
  --client-secret "$TESLA_CLIENT_SECRET" \
  --redirect-uri "$TESLA_REDIRECT_URI"
```

The CLI stores the refresh token in `~/.config/energyscraper/config.json` with file mode `0600`. It does not store the client secret.

Browser auth defaults to the `python-tesla-fleet-api` OAuth helper because that matches Tesla's working authorization-code flow. There is also an experimental PKCE flow available with `--oauth-flow pkce`; it defaults to Tesla's `https://auth.tesla.com/oauth2/v3/token` token endpoint, and `--token-url fleet` compares against the Fleet docs endpoint.

The Tesla app should allow these scopes for the simple commands here:

```text
openid offline_access user_data vehicle_device_data energy_device_data energy_cmds
```

Register the hosted public key once with a partner token:

```bash
energyscraper partner register --domain your-domain.example
energyscraper partner public-key --domain your-domain.example --json
```

The partner token is only used for Tesla partner/admin endpoints like this registration step. The metrics commands use a user token from `energyscraper auth login`. If Tesla reports missing scopes on a partner check, pass the exact partner token scopes with repeated `--scope` flags.

Tesla OAuth uses two different client flows:

```text
client-credentials       Partner/admin setup only
authorization-code       Browser login for user-owned cars and energy sites
```

If the Tesla dashboard shows `OAuth Grant Type: client-credentials`, that client can run `energyscraper partner ...` but cannot run `energyscraper auth login`. Create or change the client so its grant type includes authorization code, then use:

```text
Allowed Origin URL:   https://your-domain.example
Allowed Redirect URI: https://your-domain.example/tesla/callback
```

If Tesla rejects the browser login before showing the account page, try a minimal scope login first:

```bash
energyscraper auth login \
  --redirect-uri "https://your-domain.example/tesla/callback" \
  --oauth-flow library \
  --scope openid \
  --scope offline_access
```

## Commands

```bash
energyscraper metrics
energyscraper energy
energyscraper cars
energyscraper products
energyscraper products --json
energyscraper pair --site <energy_site_id>
energyscraper strings --site <energy_site_id>
energyscraper unpair --site <energy_site_id>
```

`products` prints the useful account topology without the enormous vehicle `cached_data` blobs: vehicle names/models, energy site IDs, asset UUIDs, capabilities, component serials and part numbers, and the device UUIDs Tesla reports for the site. VINs are masked in human-readable output; `--json` remains the unmodified Fleet response.

Tesla's energy command endpoint routes by a device *type*, not by one of the returned component UUIDs. The CLI can safely try the known target types with a read-only system-info request:

```bash
energyscraper products --probe-devices
energyscraper products --probe-devices --json
```

The known target values are `1` gateway DIN, `2` site UUID, `3` solar inverter DIN, and `4` Wall Connector DIN. They can also be selected manually with `device-command --identifier-type`. The command payload has no field for a specific component UUID, so the UUID inventory does not currently let the CLI address each reported device independently.

Official Fleet energy endpoints expose live site power and Powerwall state of energy, but not the per-inverter/per-MPPT string voltage and current values as normal `live_status` fields. Those come from the gateway's TEDAPI, over an RSA-signed channel. The `pair` and `strings` commands below make that data available without connecting to the Powerwall's Wi-Fi or entering a gateway password.

## Powerwall 3 PV string data

The `strings` command reads Powerwall 3 per-string voltage/current/power directly from the gateway over the local network. It authenticates with an RSA key you register once; the gateway DIN and LAN IP are discovered automatically from the Fleet API, so no gateway password is needed.

Register a key (one-time; writes an authorized client to the gateway):

```bash
energyscraper pair --site <energy_site_id>
```

If the gateway does not auto-verify via the cloud, toggle any one Powerwall breaker off then back on within 30 seconds and re-run `pair` to confirm. This is the same physical confirmation Tesla requires when pairing a phone or vehicle.

Then read the strings (run this from a machine on the same LAN as the gateway):

```bash
energyscraper strings --site <energy_site_id>
energyscraper strings --site <energy_site_id> --json
```

Output lists each string's voltage, current, and power, marks disconnected/standby strings, and breaks out solar production by source:

```text
String total (DC)                     sum of the Powerwall's DC strings
Total solar (meter)                   Fleet solar meter total (all solar)
AC-coupled solar (meter - strings)    production from any AC-coupled inverter
```

The `AC-coupled solar` line is useful when a separate AC inverter (for example an older string inverter on a different roof face) feeds the same site: it is the difference between the metered solar total and the Powerwall's own DC strings.

### Example

Early morning and overcast, on a Powerwall 3 with three DC strings plus a separate AC-coupled inverter (serial and DIN are masked in human-readable output):

```text
$ energyscraper strings --site <energy_site_id>
Inverter serial TG000***EST
  String 1: 65 V, 1.5 A, 97.5 W
  String 2: 165 V, 1.5 A, 247.5 W
  String 3: 195 V, 1.6 A, 312 W
  String 4: 0 V, 0 A, 0 W
  String 5: 0 V, 0 A, 0 W
  String 6: 0 V, 0 A, 0 W  [Pv_Active_Parallel]
  Inverter AC out: -60 W, 246.4 V, 60 Hz
  DC strings:  657.0 W
  AC-coupled:  180.1 W
  --------------------
  Total solar: 837.1 W
  (host 192.0.2.10, din 17070***EST)
```

The totals read as an addition: the DC strings plus the AC-coupled inverter equal the metered solar total.

In this installation the DC strings map to roof faces as String 1 = north, String 2 = west, String 3 = east; strings 4-6 are unused. The south-facing array is on a separate AC-coupled inverter, so it never appears as a DC string - it shows up only in the `AC-coupled solar` figure (about 180 W here), computed as the metered solar total minus the DC string total.

`strings` reports the leader inverter. On multi-inverter sites the additional inverters require a Wi-Fi session to the gateway, the same limitation the cloud diagnostics apps have.

By default `strings` talks to the gateway over the local network. There is an experimental `--via cloud` mode that relays the same RSA-signed query through the Fleet `device_command` endpoint so it could work away from home. It currently authenticates but the signed-energy request body schema is unconfirmed and the endpoint returns an error, so `--via cloud` is not usable yet. Use the default local mode.

To revoke the registered key later:

```bash
energyscraper unpair --site <energy_site_id>
```

Historical note: earlier investigation assumed string data required a private Tesla endpoint or separately provisioned Powerhub telemetry. It does not; the RSA-signed TEDAPI path above is sufficient.

The CLI includes a raw gateway gRPC command escape hatch for investigating that Fleet path:

```bash
energyscraper device-command --site 123456789 --category common --command get_system_info_request
energyscraper device-command --site 123456789 --category common --command get_system_info_request --identifier-type 3
energyscraper device-command --site 123456789 --category energysitenet --command get_config_request --json
```

Run `energyscraper energy --device --json` first. If Fleet returns any string-like fields in the standard or known gateway responses, the summary will call them out. If not, the next step is to try read-only `device-command` category/command pairs until we identify the device-vitals command that returns inverter string data.

There is also an optional local hook for older or still-accessible Powerwall/TEDAPI setups:

```bash
pip install -e '.[local]'
energyscraper local --host 192.168.91.1 --gateway-password "$PW_GW_PWD" --email you@example.com
```

That prints a solar monitoring summary with each inverter serial, string voltage/current/power, total string power, and the meter aggregate values for solar, home, Powerwall, and grid. Use `--json` if you want the raw `pypowerwall` payloads.

For Powerwall 3 wired LAN/v1r mode, pass `--rsa-key-path /path/to/tedapi_rsa_private.pem` and the Powerwall vendor-subnet host instead.

## Roadmap

- **Prometheus exporter** (the original motivation): expose the live metrics - site power flows, Powerwall state of energy, per-string PV voltage/current/power, and AC-coupled solar - on an HTTP endpoint in Prometheus text format, for long-term graphing and alerting.
- **Continuous scrape/daemon mode** for high-resolution history.
- **Multi-inverter support**: read follower Powerwall 3 inverters, not just the leader.
- **Remote string reads**: finish the experimental `--via cloud` transport so string data works away from home.
- **Optional per-string labels** (for example roof orientation) in the output.
