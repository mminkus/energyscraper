# Grafana + Prometheus stack

Graphs each PV string, the AC-coupled solar, the site power flows, and the
Powerwall charge, scraped from the `energyscraper exporter`.

## How it fits together

```
energyscraper exporter  (host, :9835)  <--scrape--  Prometheus (:9090)  <--query--  Grafana (:3000)
```

The exporter runs on the host (it needs your RSA key and LAN access to the
gateway). Prometheus and Grafana run in Docker and are pre-wired: Prometheus
scrapes the host exporter, Grafana auto-provisions the Prometheus datasource
and the dashboard.

## 1. Start the exporter on the host

```bash
energyscraper exporter --site <energy_site_id>
# serves http://0.0.0.0:9835/metrics
```

Leave it running (systemd unit, tmux, `nohup`, etc.). Verify:

```bash
curl -s localhost:9835/metrics | head
```

## 2. Start Prometheus + Grafana

```bash
cd deploy
docker compose up -d
```

- Grafana: http://localhost:3000 (anonymous admin is on for a private LAN;
  lock it down if you expose it). Open the dashboard **energyscraper - Solar
  Strings**.
- Prometheus: http://localhost:9090 (check Status -> Targets shows the
  `energyscraper` target UP).

This Docker stack is the **dev/quick-start** environment. For a persistent
deploy, run Prometheus + Grafana natively and the exporter under systemd (see
below).

## Production (systemd)

Run the exporter as a dedicated non-privileged user so it survives reboots
(which tmux/`disown` cannot). Best placed on a box with a LAN route to the
gateway, alongside a native Prometheus.

```bash
# 1. dedicated user + install (adjust the venv path to taste)
sudo useradd --system --home-dir /var/lib/energyscraper --shell /usr/sbin/nologin energyscraper
sudo install -d -o energyscraper -g energyscraper -m 0700 /opt/energyscraper
sudo -u energyscraper python3 -m venv /opt/energyscraper/venv
sudo -u energyscraper /opt/energyscraper/venv/bin/pip install -e /path/to/energyscraper

# 2. state dir + secrets (copied from wherever you paired). Both 0600.
sudo install -d -o energyscraper -g energyscraper -m 0700 /var/lib/energyscraper
sudo install -o energyscraper -g energyscraper -m 0600 \
    ~/.config/energyscraper/config.json            /var/lib/energyscraper/config.json
sudo install -o energyscraper -g energyscraper -m 0600 \
    ~/.config/energyscraper/tedapi_rsa_private.pem /var/lib/energyscraper/tedapi_rsa_private.pem

# 3. install the unit (edit <SITE_ID> and the venv path first)
sudo cp deploy/energyscraper-exporter.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now energyscraper-exporter.service

# 4. verify
systemctl status energyscraper-exporter.service --no-pager
curl -s localhost:9835/metrics | head
```

Then point your native Prometheus at `localhost:9835` (the unit binds loopback,
so no `host.docker.internal` and no `--bind 0.0.0.0` needed):

```yaml
scrape_configs:
  - job_name: energyscraper
    static_configs:
      - targets: ['localhost:9835']
```

The exporter caches the gateway DIN/IP into `config.json` on first run, so the
state dir must stay writable (the unit's `StateDirectory` handles that). Logs:
`journalctl -u energyscraper-exporter -f`.

To run any command manually as the service user, pass the config path yourself
- the `ENERGYSCRAPER_CONFIG` env var only applies inside the unit, and `sudo`
scrubs the environment, so a bare `sudo -u energyscraper ... metrics` looks in
`~/.config` (empty) and reports "Not authenticated":

```bash
sudo -u energyscraper env ENERGYSCRAPER_CONFIG=/var/lib/energyscraper/config.json \
    /opt/energyscraper/venv/bin/energyscraper metrics
```

## Notes

- `host.docker.internal` is mapped to the host gateway so the containers can
  reach the exporter. If Prometheus can't reach it, confirm the exporter is
  bound to `0.0.0.0` (the default) and that the host firewall allows 9835
  from the Docker bridge.
- The exporter reads PV strings locally on every scrape and caches the cloud
  solar-meter/site-power for `--ttl` seconds (default 30) to respect Fleet
  API rate limits, so a 15s Prometheus scrape interval is fine.
- Prometheus retention here is 365 days; adjust in `docker-compose.yml`.
