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

## Notes

- `host.docker.internal` is mapped to the host gateway so the containers can
  reach the exporter. If Prometheus can't reach it, confirm the exporter is
  bound to `0.0.0.0` (the default) and that the host firewall allows 9835
  from the Docker bridge.
- The exporter reads PV strings locally on every scrape and caches the cloud
  solar-meter/site-power for `--ttl` seconds (default 30) to respect Fleet
  API rate limits, so a 15s Prometheus scrape interval is fine.
- Prometheus retention here is 365 days; adjust in `docker-compose.yml`.
