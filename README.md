# energyscraper

A small CLI for reading Tesla Fleet energy-site metrics and a minimal vehicle list.

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
energyscraper products --json
```

Official Fleet energy endpoints expose live site power and Powerwall state of energy. Tesla's public docs do not currently expose the per-inverter/per-MPPT string voltage and current values as normal `live_status` fields. Apps that show those values are probably using either Tesla's energy device-command bridge or a private/mobile-app endpoint.

The CLI includes a raw gateway gRPC command escape hatch for investigating that Fleet path:

```bash
energyscraper device-command --site 123456789 --category common --command get_system_info_request
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
