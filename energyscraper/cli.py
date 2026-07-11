from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse

import aiohttp
from tesla_fleet_api import TeslaFleetApi, TeslaFleetOAuth
from tesla_fleet_api.const import EnergyDeviceIdentifierType, Method, SERVERS, Scope
from tesla_fleet_api.exceptions import TeslaFleetError


DEFAULT_REGION = "na"
DEFAULT_AUTH_TOKEN_URL = "https://auth.tesla.com/oauth2/v3/token"
DEFAULT_SCOPES = [
    Scope.OPENID,
    Scope.OFFLINE_ACCESS,
    Scope.USER_DATA,
    Scope.VEHICLE_DEVICE_DATA,
    Scope.ENERGY_DEVICE_DATA,
    Scope.ENERGY_CMDS,
]
DEFAULT_PARTNER_SCOPES = [
    Scope.OPENID,
    Scope.USER_DATA,
    Scope.VEHICLE_DEVICE_DATA,
    Scope.ENERGY_DEVICE_DATA,
    Scope.ENERGY_CMDS,
]
MODEL_CODES = {
    "S": "Model S",
    "X": "Model X",
    "3": "Model 3",
    "Y": "Model Y",
    "C": "Cybertruck",
    "R": "Roadster",
    "T": "Semi",
}
STRING_METRIC_TERMS = (
    "mppt",
    "string",
    "inverter_voltage",
    "inverter_current",
    "solar_inverter_voltage",
    "solar_inverter_current",
)
PV_STRING_NAMES = ("A", "B", "C", "D", "E", "F")
ENERGY_DEVICE_TARGETS = {
    EnergyDeviceIdentifierType.GATEWAY_DIN: "Gateway DIN",
    EnergyDeviceIdentifierType.SITE_UUID: "Site UUID",
    EnergyDeviceIdentifierType.SOLAR_INVERTER_DIN: "Solar inverter DIN",
    EnergyDeviceIdentifierType.WALL_CONNECTOR_DIN: "Wall Connector DIN",
}


def default_config_path() -> Path:
    configured = os.environ.get("ENERGYSCRAPER_CONFIG")
    if configured:
        return Path(configured).expanduser()

    config_home = os.environ.get("XDG_CONFIG_HOME")
    if config_home:
        return Path(config_home).expanduser() / "energyscraper" / "config.json"

    return Path.home() / ".config" / "energyscraper" / "config.json"


def resolve_config_path(value: str | None) -> Path:
    return Path(value).expanduser() if value else default_config_path()


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def save_config(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.chmod(tmp_path, 0o600)
    tmp_path.replace(path)


def print_json(data: Any) -> None:
    json.dump(data, sys.stdout, indent=2, sort_keys=False, default=str)
    print()


def unwrap_response(data: Any) -> Any:
    if isinstance(data, dict) and "response" in data:
        return data["response"]
    return data


def extract_code(value: str) -> str:
    value = value.strip()
    parsed = urlparse(value)
    query = parse_qs(parsed.query)
    if "code" in query and query["code"]:
        return query["code"][0]
    return value


def base64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def generate_pkce_pair() -> tuple[str, str]:
    verifier = base64url(secrets.token_bytes(64))
    challenge = base64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def default_auth_token_url(region: str) -> str:
    if region == "cn":
        return "https://auth.tesla.cn/oauth2/v3/token"
    return DEFAULT_AUTH_TOKEN_URL


def resolve_token_url(value: str | None, region: str) -> str:
    value = value or os.environ.get("TESLA_TOKEN_URL")
    if not value or value == "auth":
        return default_auth_token_url(region)
    if value == "fleet":
        if region == "cn":
            return "https://auth.tesla.cn/oauth2/v3/token"
        return "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token"
    return value


def build_authorize_url(
    client_id: str,
    redirect_uri: str,
    scopes: list[str],
    state: str,
    region: str,
    code_challenge: str,
    nonce: str,
) -> str:
    domain = "auth.tesla.cn" if region == "cn" else "auth.tesla.com"
    params = {
        "response_type": "code",
        "client_id": client_id,
        "locale": "en-US",
        "prompt": "login",
        "redirect_uri": redirect_uri,
        "scope": " ".join(scopes),
        "state": state,
        "nonce": nonce,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"https://{domain}/oauth2/v3/authorize?{urlencode(params, quote_via=quote)}"


def require_value(name: str, arg_value: str | None, env_name: str) -> str:
    value = arg_value or os.environ.get(env_name)
    if not value:
        raise CliError(f"Missing {name}. Pass it as an option or set {env_name}.")
    return value


def product_items(products: dict[str, Any]) -> list[dict[str, Any]]:
    response = unwrap_response(products)
    if isinstance(response, list):
        return [item for item in response if isinstance(item, dict)]
    return []


def energy_site_id(product: dict[str, Any]) -> int | None:
    for key in ("energy_site_id", "site_id"):
        value = product.get(key)
        if value is not None:
            return int(value)
    return None


def product_components(product: dict[str, Any]) -> dict[str, Any]:
    components = product.get("components")
    return components if isinstance(components, dict) else {}


def product_name(product: dict[str, Any]) -> str:
    name = first_present(product, "site_name", "asset_site_name", "name")
    if name:
        return str(name)
    components = product_components(product)
    if product.get("resource_type") in {"charger", "wall_connector"} or components.get("wall_connectors"):
        return "Wall Connector"
    product_id = product.get("id")
    return f"Energy site {product_id}" if product_id else "Energy site"


def mask_vin(vin: Any) -> str:
    value = str(vin or "")
    if len(value) < 8:
        return value or "unknown"
    return f"{value[:3]}***{value[-4:]}"


def vehicle_model(vin: str | None) -> str:
    if not vin or len(vin) < 4:
        return "Unknown"
    return MODEL_CODES.get(vin[3], "Unknown")


def fmt_percent(value: Any) -> str:
    if value is None:
        return "unknown"
    try:
        return f"{float(value):.0f}%"
    except (TypeError, ValueError):
        return str(value)


def fmt_power(value: Any) -> str:
    if value is None:
        return "unknown"
    try:
        watts = float(value)
    except (TypeError, ValueError):
        return str(value)
    if abs(watts) >= 1000:
        return f"{watts / 1000:.2f} kW"
    return f"{watts:.0f} W"


def serial_from_din(value: Any) -> str | None:
    if not isinstance(value, str) or "--" not in value:
        return None
    return value.rsplit("--", 1)[-1] or None


def is_wall_connector_site(product: dict[str, Any], info: dict[str, Any], live: dict[str, Any]) -> bool:
    product_components = product.get("components") if isinstance(product.get("components"), dict) else {}
    info_components = info.get("components") if isinstance(info.get("components"), dict) else {}
    wall_connectors = product_components.get("wall_connectors") or info_components.get("wall_connectors") or live.get("wall_connectors")
    has_energy = bool(product_components.get("battery") or product_components.get("solar") or info_components.get("battery") or info_components.get("solar"))
    return product.get("resource_type") == "wall_connector" or (bool(wall_connectors) and not has_energy)


def print_wall_connector_summary(site: dict[str, Any], product: dict[str, Any], info: dict[str, Any], live: dict[str, Any]) -> None:
    product_components = product.get("components") if isinstance(product.get("components"), dict) else {}
    info_components = info.get("components") if isinstance(info.get("components"), dict) else {}
    component_connectors = product_components.get("wall_connectors") or info_components.get("wall_connectors") or []
    live_connectors = live.get("wall_connectors") or []
    connector_by_din = {
        connector.get("din"): connector
        for connector in component_connectors
        if isinstance(connector, dict) and connector.get("din")
    }

    name = first_present(product, "site_name", "asset_site_name", "name", "id") or first_present(info, "site_name", "name", "site_number") or f"Site {site['site_id']}"
    print(f"{name} ({site['site_id']})")
    print("  Type: Wall Connector")
    if not live_connectors and not component_connectors:
        print("  Wall connectors: none reported")
        return

    connectors = live_connectors or component_connectors
    for connector in connectors:
        if not isinstance(connector, dict):
            continue
        din = connector.get("din")
        component = connector_by_din.get(din, {})
        serial = connector.get("serial_number") or component.get("serial_number") or serial_from_din(din) or "unknown"
        power = connector.get("wall_connector_power")
        state = connector.get("wall_connector_state")
        fault = connector.get("wall_connector_fault_state")
        details = [f"power {fmt_power(power)}"]
        if state is not None:
            details.append(f"state {state}")
        if fault is not None:
            details.append(f"fault {fault}")
        print(f"  Connector {serial}: {', '.join(details)}")


def fmt_watts(value: Any) -> str:
    if value is None:
        return "unknown"
    try:
        return f"{float(value):,.0f} W"
    except (TypeError, ValueError):
        return str(value)


def fmt_voltage(value: Any) -> str:
    number = as_float(value)
    if number is None:
        return "unknown"
    if number.is_integer():
        return f"{number:,.0f} V"
    return f"{number:,.1f} V"


def fmt_current(value: Any) -> str:
    number = as_float(value)
    if number is None:
        return "unknown"
    return f"{number:,.1f} A"


def as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def first_present(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    return None


def mask_serial(serial: Any) -> str:
    if not serial:
        return "unknown"
    value = str(serial)
    if len(value) <= 8:
        return value
    return f"{value[:5]}***{value[-3:]}"


def metric_value(data: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value is not None:
            return value
    return None


def flatten_named_vitals(data: Any) -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            name = node.get("name")
            if isinstance(name, str) and any(term in name.lower() for term in STRING_METRIC_TERMS):
                for key in ("floatValue", "intValue", "stringValue", "boolValue", "float_value", "int_value", "string_value", "bool_value", "value"):
                    if key in node:
                        found.append((name, node[key]))
                        break
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(data)
    return found


def find_string_metrics(data: Any) -> list[tuple[str, Any]]:
    found: list[tuple[str, Any]] = []

    def visit(node: Any, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                next_path = f"{path}.{key}" if path else str(key)
                lowered = next_path.lower()
                if not isinstance(value, (dict, list)) and any(term in lowered for term in STRING_METRIC_TERMS):
                    found.append((next_path, value))
                visit(value, next_path)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                visit(item, f"{path}[{index}]")

    found.extend(flatten_named_vitals(data))
    visit(data, "")

    deduped: list[tuple[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for name, value in found:
        marker = (name, repr(value))
        if marker not in seen:
            deduped.append((name, value))
            seen.add(marker)
    return deduped


def parse_json_if_needed(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def is_disabled_string(row: dict[str, Any]) -> bool:
    state = str(row.get("state") or "").lower()
    connected = row.get("connected")
    voltage = as_float(row.get("voltage"))
    current = as_float(row.get("current"))
    power = as_float(row.get("power"))
    if connected is False or "disabled" in state:
        return True
    if "standby" in state and not any(value and abs(value) > 0.01 for value in (voltage, current, power)):
        return True
    return False


def build_vitals_inverters(vitals: Any) -> list[dict[str, Any]]:
    vitals = parse_json_if_needed(vitals)
    if not isinstance(vitals, dict):
        return []

    inverters: list[dict[str, Any]] = []
    for device_key in sorted(vitals):
        if not str(device_key).startswith("PVAC--"):
            continue
        device = vitals.get(device_key)
        if not isinstance(device, dict):
            continue

        device_suffix = str(device_key).split("PVAC--", 1)[1]
        pvs = vitals.get(f"PVS--{device_suffix}", {})
        if not isinstance(pvs, dict):
            pvs = {}

        serial = device.get("serialNumber") or str(device_key).split("--")[-1]
        rows: list[dict[str, Any]] = []
        for index, letter in enumerate(PV_STRING_NAMES, start=1):
            voltage = metric_value(
                device,
                f"PVAC_PVMeasuredVoltage_{letter}",
                f"PVAC_PvVoltage_{letter}",
            )
            current = metric_value(
                device,
                f"PVAC_PVCurrent_{letter}",
                f"PVAC_PvCurrent_{letter}",
            )
            power = metric_value(
                device,
                f"PVAC_PVMeasuredPower_{letter}",
                f"PVAC_PvPower_{letter}",
            )
            if power is None and as_float(voltage) is not None and as_float(current) is not None:
                power = (as_float(voltage) or 0.0) * (as_float(current) or 0.0)
            state = metric_value(
                device,
                f"PVAC_PvState_{letter}",
                f"PVAC_PVState_{letter}",
            )
            connected = metric_value(
                pvs,
                f"PVS_String{letter}_Connected",
                f"PVS_String_{letter}_Connected",
            )
            if all(value is None for value in (voltage, current, power, state, connected)):
                continue
            rows.append(
                {
                    "number": index,
                    "name": letter,
                    "voltage": voltage,
                    "current": current,
                    "power": power,
                    "state": state,
                    "connected": connected,
                }
            )

        if rows:
            total = sum(as_float(row.get("power")) or 0.0 for row in rows if not is_disabled_string(row))
            inverters.append(
                {
                    "serial": serial,
                    "part_number": device.get("partNumber"),
                    "total_power": total,
                    "strings": rows,
                }
            )
    return inverters


def build_simple_string_inverters(strings: Any) -> list[dict[str, Any]]:
    strings = parse_json_if_needed(strings)
    if not isinstance(strings, dict):
        return []

    groups: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for name, values in strings.items():
        if not isinstance(name, str) or not isinstance(values, dict) or not name:
            continue
        suffix = name[1:] if len(name) > 1 else ""
        groups.setdefault(suffix, []).append((name, values))

    inverters: list[dict[str, Any]] = []
    for suffix, items in sorted(groups.items(), key=lambda item: item[0]):
        rows: list[dict[str, Any]] = []
        for index, (name, values) in enumerate(sorted(items), start=1):
            voltage = metric_value(values, "Voltage", "voltage", "measured_voltage")
            current = metric_value(values, "Current", "current")
            power = metric_value(values, "Power", "power", "measured_power")
            state = metric_value(values, "State", "state")
            connected = metric_value(values, "Connected", "connected")
            rows.append(
                {
                    "number": index,
                    "name": name,
                    "voltage": voltage,
                    "current": current,
                    "power": power,
                    "state": state,
                    "connected": connected,
                }
            )
        total = sum(as_float(row.get("power")) or 0.0 for row in rows if not is_disabled_string(row))
        inverters.append(
            {
                "serial": f"inverter {suffix}" if suffix else "unknown",
                "part_number": None,
                "total_power": total,
                "strings": rows,
            }
        )
    return inverters


def build_local_inverters(vitals: Any, strings: Any) -> list[dict[str, Any]]:
    inverters = build_vitals_inverters(vitals)
    if inverters:
        return inverters
    return build_simple_string_inverters(strings)


def print_local_summary(data: dict[str, Any]) -> None:
    site_name = data.get("site_name")
    if isinstance(site_name, dict) and "error" in site_name:
        site_name = None
    print(site_name or "Powerwall")

    firmware = data.get("firmware")
    if firmware is not None and not isinstance(firmware, dict):
        print(f"Firmware: {firmware}")
    battery_level = data.get("battery_level")
    if battery_level is not None and not isinstance(battery_level, dict):
        print(f"Powerwall: {fmt_percent(battery_level)}")

    print()
    print("Solar")
    inverters = build_local_inverters(data.get("vitals"), data.get("strings"))
    if not inverters:
        print("  No inverter/string data returned.")
    for inverter in inverters:
        print(f"  Inverter serial {mask_serial(inverter.get('serial'))}")
        for row in inverter["strings"]:
            print(f"    String {row['number']}:")
            if is_disabled_string(row):
                print("      Disabled")
            else:
                print(f"      Voltage: {fmt_voltage(row.get('voltage'))}")
                print(f"      Current: {fmt_current(row.get('current'))}")
                print(f"      Power: {fmt_watts(row.get('power'))}")
        print(f"    Total power: {fmt_watts(inverter.get('total_power'))}")

    print()
    print("Meters")
    power = data.get("power")
    if isinstance(power, dict) and "error" not in power:
        print(f"  Solar: {fmt_watts(power.get('solar'))}")
        print(f"  Home: {fmt_watts(power.get('load') if power.get('load') is not None else power.get('home'))}")
        print(f"  Powerwall: {fmt_watts(power.get('battery'))}")
        print(f"  Grid: {fmt_watts(power.get('site') if power.get('site') is not None else power.get('grid'))}")
    else:
        print("  No meter data returned.")


def print_table(
    rows: list[dict[str, str]],
    columns: list[tuple[str, str]],
    indent: str = "",
) -> None:
    if not rows:
        print(f"{indent}No rows.")
        return

    widths = {
        key: max(len(title), *(len(row.get(key, "")) for row in rows))
        for key, title in columns
    }
    header = "  ".join(title.ljust(widths[key]) for key, title in columns)
    print(f"{indent}{header}")
    print(f"{indent}{'  '.join('-' * widths[key] for key, _ in columns)}")
    for row in rows:
        print(f"{indent}{'  '.join(row.get(key, '').ljust(widths[key]) for key, _ in columns)}")


def enabled_capabilities(product: dict[str, Any]) -> list[str]:
    components = product_components(product)
    labels = {
        "battery": "battery",
        "solar": "solar",
        "grid": "grid",
        "load_meter": "load meter",
    }
    capabilities = [label for key, label in labels.items() if components.get(key)]
    if product.get("charge_on_solar_capable"):
        capabilities.append("Charge on Solar")
    return capabilities


def gateway_role(product: dict[str, Any], device: dict[str, Any]) -> str:
    gateway_id = str(product.get("gateway_id") or "")
    if gateway_id and gateway_id in {
        str(device.get("din") or ""),
        str(device.get("serial_number") or ""),
    }:
        return "site gateway"
    return ""


def print_products_summary(items: list[dict[str, Any]], probes: list[dict[str, Any]] | None = None) -> None:
    energy_products = [item for item in items if energy_site_id(item) is not None]
    vehicles = [item for item in items if item.get("vin")]
    print(f"Products: {len(items)} total, {len(energy_products)} energy site(s), {len(vehicles)} vehicle(s)")

    if vehicles:
        print()
        print("Vehicles")
        print_table(
            [
                {
                    "name": str(item.get("display_name") or "Unnamed"),
                    "model": vehicle_model(item.get("vin")),
                    "state": str(item.get("state") or "unknown"),
                    "vin": mask_vin(item.get("vin")),
                }
                for item in vehicles
            ],
            [("name", "Name"), ("model", "Model"), ("state", "State"), ("vin", "VIN")],
        )

    probes_by_site = {
        probe["site_id"]: probe
        for probe in probes or []
        if probe.get("site_id") is not None
    }
    if energy_products:
        print()
        print("Energy")
    for index, product in enumerate(energy_products):
        if index:
            print()
        site_id = energy_site_id(product)
        components = product_components(product)
        print(product_name(product))
        print(f"  Type: {product.get('resource_type') or 'energy'}")
        print(f"  Site ID: {site_id}")
        if product.get("id"):
            print(f"  Site serial: {product['id']}")
        if product.get("asset_site_id"):
            print(f"  Asset site UUID: {product['asset_site_id']}")
        if product.get("gateway_id"):
            print(f"  Gateway ID: {product['gateway_id']}")
        if product.get("battery_type"):
            print(f"  Battery type: {product['battery_type']}")
        capabilities = enabled_capabilities(product)
        if capabilities:
            print(f"  Capabilities: {', '.join(capabilities)}")

        gateways = [device for device in components.get("gateways", []) if isinstance(device, dict)]
        if gateways:
            print("  Devices (reported by Tesla under gateways):")
            print_table(
                [
                    {
                        "serial": str(device.get("serial_number") or serial_from_din(device.get("din")) or "unknown"),
                        "part": str(device.get("part_number") or "unknown"),
                        "active": "yes" if device.get("is_active") else "no",
                        "role": gateway_role(product, device),
                        "uuid": str(device.get("device_id") or "unknown"),
                    }
                    for device in gateways
                ],
                [
                    ("serial", "Serial"),
                    ("part", "Part number"),
                    ("active", "Active"),
                    ("role", "Role"),
                    ("uuid", "Device UUID"),
                ],
                indent="    ",
            )

        connectors = [device for device in components.get("wall_connectors", []) if isinstance(device, dict)]
        if connectors:
            print("  Wall Connectors:")
            print_table(
                [
                    {
                        "serial": str(device.get("serial_number") or serial_from_din(device.get("din")) or "unknown"),
                        "part": str(device.get("part_number") or "unknown"),
                        "active": "yes" if device.get("is_active") else "no",
                        "uuid": str(device.get("device_id") or "unknown"),
                    }
                    for device in connectors
                ],
                [
                    ("serial", "Serial"),
                    ("part", "Part number"),
                    ("active", "Active"),
                    ("uuid", "Device UUID"),
                ],
                indent="    ",
            )

        probe = probes_by_site.get(site_id)
        if probe:
            print("  Read-only device probes:")
            rows = [
                {
                    "target": str(result.get("target") or "unknown"),
                    "result": summarize_system_info_probe(result),
                }
                for result in probe.get("targets", [])
                if isinstance(result, dict)
            ]
            print_table(rows, [("target", "Target"), ("result", "Result")], indent="    ")


def find_nested_mapping(data: Any, key: str) -> dict[str, Any] | None:
    if isinstance(data, dict):
        for current_key, value in data.items():
            if current_key.lower().replace("_", "") == key.lower().replace("_", "") and isinstance(value, dict):
                return value
            found = find_nested_mapping(value, key)
            if found is not None:
                return found
    elif isinstance(data, list):
        for value in data:
            found = find_nested_mapping(value, key)
            if found is not None:
                return found
    return None


def summarize_system_info_probe(result: dict[str, Any]) -> str:
    if result.get("error"):
        return str(result["error"])
    response = result.get("response")
    system_info = find_nested_mapping(response, "GetSystemInfoResponse")
    if system_info:
        device_id = system_info.get("device_id")
        if not isinstance(device_id, dict):
            device_id = {}
        firmware = system_info.get("firmware_version")
        if not isinstance(firmware, dict):
            firmware = {}
        serial = device_id.get("serial_number") or serial_from_din(system_info.get("din")) or "unknown"
        part = device_id.get("part_number") or "unknown"
        version = firmware.get("version")
        summary = f"{serial}, part {part}"
        if version:
            summary += f", firmware {version}"
        if system_info.get("device_type") is not None:
            summary += f", device type {system_info['device_type']}"
        return summary
    timeout = find_nested_mapping(response, "Timeout")
    if timeout:
        return str(timeout.get("description") or "timed out")
    return "response received (no system info)"


class CliError(RuntimeError):
    pass


async def response_payload(resp: aiohttp.ClientResponse) -> Any:
    try:
        return await resp.json(content_type=None)
    except Exception:
        return await resp.text()


async def get_partner_token(
    session: aiohttp.ClientSession,
    client_id: str,
    client_secret: str,
    region: str,
    scopes: list[str],
) -> str:
    audience = SERVERS.get(region)
    if not audience:
        raise CliError(f"Unknown Tesla Fleet region: {region}")

    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "audience": audience,
    }
    if scopes:
        data["scope"] = " ".join(str(scope) for scope in scopes)

    async with session.post(
        "https://fleet-auth.prd.vn.cloud.tesla.com/oauth2/v3/token",
        data=data,
    ) as resp:
        data = await response_payload(resp)
        if not resp.ok:
            raise CliError(f"Could not get partner token: {data}")
        if not isinstance(data, dict) or not data.get("access_token"):
            raise CliError(f"Tesla did not return a partner access token: {data}")
        return str(data["access_token"])


async def exchange_auth_code(
    session: aiohttp.ClientSession,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    code: str,
    region: str,
    token_url: str,
    code_verifier: str,
) -> dict[str, Any]:
    audience = SERVERS.get(region)
    if not audience:
        raise CliError(f"Unknown Tesla Fleet region: {region}")

    async with session.post(
        token_url,
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "code": code,
            "audience": audience,
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
    ) as resp:
        data = await response_payload(resp)
        if not resp.ok:
            raise CliError(f"Could not exchange authorization code: {data}")
        if not isinstance(data, dict) or not data.get("access_token"):
            raise CliError(f"Tesla did not return an access token: {data}")
        return data


async def refresh_user_token(
    session: aiohttp.ClientSession,
    client_id: str,
    refresh_token: str,
    token_url: str,
) -> dict[str, Any]:
    async with session.post(
        token_url,
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        },
    ) as resp:
        data = await response_payload(resp)
        if not resp.ok:
            raise CliError(f"Could not refresh Tesla token: {data}")
        if not isinstance(data, dict) or not data.get("access_token"):
            raise CliError(f"Tesla did not return a refreshed access token: {data}")
        return data


async def get_api(
    session: aiohttp.ClientSession,
    args: argparse.Namespace,
) -> tuple[TeslaFleetApi, dict[str, Any], Path]:
    config_path = resolve_config_path(getattr(args, "config", None))
    config = load_config(config_path)
    region = getattr(args, "region", None) or os.environ.get("TESLA_REGION") or config.get("region") or DEFAULT_REGION

    env_access_token = os.environ.get("TESLA_ACCESS_TOKEN")
    if env_access_token:
        return TeslaFleetApi(session=session, access_token=env_access_token, region=region), config, config_path

    client_id = getattr(args, "client_id", None) or os.environ.get("TESLA_CLIENT_ID") or config.get("client_id")
    refresh_token = config.get("refresh_token")
    access_token = config.get("access_token")
    expires = int(config.get("expires", 0))
    token_url = config.get("token_url") or resolve_token_url(getattr(args, "token_url", None), region)

    if not client_id or not refresh_token:
        raise CliError("Not authenticated yet. Run `energyscraper auth login` first.")

    if not access_token or expires <= int(time.time()) + 60:
        refreshed = await refresh_user_token(session, client_id, refresh_token, token_url)
        access_token = refreshed["access_token"]
        config["access_token"] = access_token
        config["refresh_token"] = refreshed.get("refresh_token", refresh_token)
        config["expires"] = int(time.time()) + int(refreshed.get("expires_in", 0))
        config["region"] = region
        config["token_url"] = token_url
        save_config(config_path, config)
    return TeslaFleetApi(session=session, access_token=access_token, region=region), config, config_path


async def login(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(args.config)
    config = load_config(config_path)
    client_id = require_value("client ID", args.client_id, "TESLA_CLIENT_ID")
    client_secret = require_value("client secret", args.client_secret, "TESLA_CLIENT_SECRET")
    redirect_uri = require_value("redirect URI", args.redirect_uri, "TESLA_REDIRECT_URI")
    region = args.region or os.environ.get("TESLA_REGION") or config.get("region") or DEFAULT_REGION
    scopes = args.scope or list(DEFAULT_SCOPES)
    token_url = resolve_token_url(args.token_url, region)
    state = args.state or secrets.token_urlsafe(24)
    nonce = args.nonce or secrets.token_urlsafe(24)
    code_verifier, code_challenge = generate_pkce_pair()

    async with aiohttp.ClientSession() as session:
        if args.oauth_flow == "library":
            oauth = TeslaFleetOAuth(
                session=session,
                region=region,
                client_id=client_id,
                client_secret=client_secret,
                redirect_uri=redirect_uri,
            )
            login_url = oauth.get_login_url(scopes=scopes, state=state)
        else:
            login_url = build_authorize_url(client_id, redirect_uri, scopes, state, region, code_challenge, nonce)
        print("Open this URL and authorize the app:")
        print(login_url)
        print()
        if args.oauth_flow == "library":
            print("This login uses python-tesla-fleet-api's built-in OAuth flow.")
        else:
            print(
                "This login uses PKCE. If Tesla shows 'Client authentication failed', "
                "verify that the client allows authorization-code login and that the "
                "redirect URI exactly matches the dashboard."
            )
        raw_code = args.code or input("Paste the redirected URL or code: ")
        code = extract_code(raw_code)
        if args.oauth_flow == "library":
            await oauth.get_refresh_token(code)
            if not oauth.refresh_token:
                raise CliError("Tesla did not return a refresh token. Check app scopes and redirect URI.")
            token_url = resolve_token_url("fleet", region)
            token = {
                "access_token": await oauth.access_token(),
                "refresh_token": oauth.refresh_token,
                "expires_in": max(0, oauth.expires - int(time.time())),
            }
        else:
            token = await exchange_auth_code(session, client_id, client_secret, redirect_uri, code, region, token_url, code_verifier)
            if not token.get("refresh_token"):
                raise CliError("Tesla did not return a refresh token. Check app scopes and redirect URI.")
        config.update(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "region": region,
                "token_url": token_url,
                "access_token": token["access_token"],
                "refresh_token": token["refresh_token"],
                "expires": int(time.time()) + int(token.get("expires_in", 0)),
                "updated_at": int(time.time()),
            }
        )
        save_config(config_path, config)

    print(f"Authenticated. Token config saved to {config_path}.")
    return 0


async def auth_status(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(args.config)
    config = load_config(config_path)
    if not config:
        print(f"No config found at {config_path}.")
        return 1

    expires = int(config.get("expires", 0))
    expires_in = max(0, expires - int(time.time()))
    print(f"Config: {config_path}")
    print(f"Region: {config.get('region', DEFAULT_REGION)}")
    print(f"Client ID: {config.get('client_id', 'missing')}")
    print(f"Token URL: {config.get('token_url', DEFAULT_AUTH_TOKEN_URL)}")
    print(f"Refresh token: {'present' if config.get('refresh_token') else 'missing'}")
    print(f"Access token expires in: {expires_in}s")
    return 0


async def auth_logout(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(args.config)
    if config_path.exists():
        config_path.unlink()
        print(f"Removed {config_path}.")
    else:
        print(f"No config found at {config_path}.")
    return 0


async def collect_energy_sites(
    api: TeslaFleetApi,
    site_ids: list[int] | None = None,
    include_device: bool = False,
) -> list[dict[str, Any]]:
    products = await api.products()
    product_by_site = {
        site_id: product
        for product in product_items(products)
        if (site_id := energy_site_id(product)) is not None
    }
    resolved_site_ids = site_ids or list(product_by_site)

    if not resolved_site_ids:
        return []

    async def collect(site_id: int) -> dict[str, Any]:
        site = api.energySites.create(site_id)
        calls: dict[str, Any] = {
            "live_status": site.live_status(),
            "site_info": site.site_info(),
        }
        if include_device:
            calls.update(
                {
                    "gateway_system_info": site.get_system_info(),
                    "gateway_networking": site.get_networking_status(),
                    "gateway_teg_config": site.get_teg_config(),
                    "energy_site_net_config": site._command("energysitenet", "get_config_request"),
                }
            )

        names = list(calls)
        results = await asyncio.gather(*calls.values(), return_exceptions=True)
        record: dict[str, Any] = {
            "site_id": site_id,
            "product": product_by_site.get(site_id),
        }
        for name, result in zip(names, results, strict=True):
            if isinstance(result, BaseException):
                record[name] = {"error": str(result)}
            else:
                record[name] = result
        record["string_metrics"] = find_string_metrics(record)
        return record

    return await asyncio.gather(*(collect(site_id) for site_id in resolved_site_ids))


async def collect_cars(api: TeslaFleetApi, wake: bool = False, wake_delay: float = 8.0) -> list[dict[str, Any]]:
    response = await api._request(Method.GET, "api/1/vehicles")
    vehicles = unwrap_response(response)
    if not isinstance(vehicles, list):
        return []

    async def collect(vehicle_info: dict[str, Any]) -> dict[str, Any]:
        vin = vehicle_info.get("vin")
        record: dict[str, Any] = {
            "vin": vin,
            "name": first_present(vehicle_info, "display_name", "vehicle_name") or "Unnamed",
            "model": vehicle_model(vin),
            "state": vehicle_info.get("state"),
            "battery_level": first_present(vehicle_info, "battery_level"),
        }
        if not vin:
            return record

        vehicle = api.vehicles.createFleet(vin)
        try:
            if wake and vehicle_info.get("state") != "online":
                await vehicle.wake_up()
                await asyncio.sleep(wake_delay)
            data = await vehicle.vehicle_data(["charge_state"])
            charge_state = unwrap_response(data).get("charge_state", {})
            if isinstance(charge_state, dict):
                record["battery_level"] = first_present(charge_state, "battery_level", "usable_battery_level")
                record["charging_state"] = charge_state.get("charging_state")
        except TeslaFleetError as exc:
            record["error"] = exc.message
            if exc.status:
                record["status"] = exc.status
        except Exception as exc:
            record["error"] = str(exc)
        return record

    return await asyncio.gather(
        *(collect(vehicle) for vehicle in vehicles if isinstance(vehicle, dict))
    )


def print_energy_summary(sites: list[dict[str, Any]]) -> None:
    if not sites:
        print("No energy sites found.")
        return

    for index, site in enumerate(sites):
        if index:
            print()
        live = unwrap_response(site.get("live_status", {}))
        info = unwrap_response(site.get("site_info", {}))
        product = site.get("product") or {}
        if not isinstance(live, dict):
            live = {}
        if not isinstance(info, dict):
            info = {}
        if not isinstance(product, dict):
            product = {}

        if is_wall_connector_site(product, info, live):
            print_wall_connector_summary(site, product, info, live)
            continue

        name = first_present(product, "site_name", "asset_site_name", "name") or first_present(info, "site_name", "name") or f"Site {site['site_id']}"
        print(f"{name} ({site['site_id']})")
        print(f"  Powerwall: {fmt_percent(live.get('percentage_charged'))}")
        print(
            "  Power: "
            f"solar {fmt_power(live.get('solar_power'))}, "
            f"load {fmt_power(live.get('load_power'))}, "
            f"battery {fmt_power(live.get('battery_power'))}, "
            f"grid {fmt_power(live.get('grid_power'))}"
        )
        grid = first_present(live, "grid_status", "island_status")
        mode = first_present(live, "operation_mode", "default_real_mode") or info.get("default_real_mode")
        reserve = first_present(info, "backup_reserve_percent", "backup_reserve_percentage")
        print(f"  Grid: {grid or 'unknown'}  Mode: {mode or 'unknown'}  Reserve: {fmt_percent(reserve)}")

        metrics = site.get("string_metrics") or []
        if metrics:
            print("  Inverter/string metrics found:")
            for key, value in metrics[:12]:
                print(f"    {key}: {value}")
            if len(metrics) > 12:
                print(f"    ... {len(metrics) - 12} more")
        else:
            print(
                "  Inverter/string metrics: not in Fleet responses; "
                f"run `energyscraper strings --site {site['site_id']}`"
            )


def print_car_summary(cars: list[dict[str, Any]]) -> None:
    rows = []
    for car in cars:
        rows.append(
            {
                "name": str(car.get("name") or "Unnamed"),
                "model": str(car.get("model") or "Unknown"),
                "battery": fmt_percent(car.get("battery_level")),
                "state": str(car.get("state") or "unknown"),
                "charging": str(car.get("charging_state") or ""),
                "note": str(car.get("error") or ""),
            }
        )
    print_table(
        rows,
        [
            ("name", "Name"),
            ("model", "Model"),
            ("battery", "Battery"),
            ("state", "State"),
            ("charging", "Charging"),
            ("note", "Note"),
        ],
    )


def product_probe_targets(product: dict[str, Any]) -> list[EnergyDeviceIdentifierType]:
    components = product_components(product)
    targets: list[EnergyDeviceIdentifierType] = [EnergyDeviceIdentifierType.SITE_UUID]
    if product.get("resource_type") == "battery" or components.get("gateways"):
        targets.insert(0, EnergyDeviceIdentifierType.GATEWAY_DIN)
    if components.get("solar"):
        targets.append(EnergyDeviceIdentifierType.SOLAR_INVERTER_DIN)
    if product.get("resource_type") in {"charger", "wall_connector"} or components.get("wall_connectors"):
        targets.append(EnergyDeviceIdentifierType.WALL_CONNECTOR_DIN)
    return targets


async def probe_energy_products(api: TeslaFleetApi, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    async def probe_product(product: dict[str, Any]) -> dict[str, Any]:
        site_id = energy_site_id(product)
        if site_id is None:
            return {"site_id": None, "targets": []}
        site = api.energySites.create(site_id)
        targets = product_probe_targets(product)
        calls = [
            site._command(
                "common",
                "get_system_info_request",
                identifier_type=identifier_type,
            )
            for identifier_type in targets
        ]
        responses = await asyncio.gather(*calls, return_exceptions=True)
        results: list[dict[str, Any]] = []
        for identifier_type, response in zip(targets, responses, strict=True):
            result: dict[str, Any] = {
                "identifier_type": int(identifier_type),
                "target": ENERGY_DEVICE_TARGETS[identifier_type],
            }
            if isinstance(response, TeslaFleetError):
                status = f" ({response.status})" if response.status else ""
                result["error"] = f"Tesla API{status}: {response.message}"
            elif isinstance(response, BaseException):
                result["error"] = str(response)
            else:
                result["response"] = response
            results.append(result)
        return {"site_id": site_id, "targets": results}

    energy_products = [item for item in items if energy_site_id(item) is not None]
    return await asyncio.gather(*(probe_product(product) for product in energy_products))


async def products_command(args: argparse.Namespace) -> int:
    async with aiohttp.ClientSession() as session:
        api, _, _ = await get_api(session, args)
        products = await api.products()
        items = product_items(products)
        probes = await probe_energy_products(api, items) if args.probe_devices else []
    if args.json:
        if probes:
            print_json({"products": products, "device_probes": probes})
        else:
            print_json(products)
    else:
        print_products_summary(items, probes)
    return 0


async def partner_command(args: argparse.Namespace) -> int:
    client_id = require_value("client ID", args.client_id, "TESLA_CLIENT_ID")
    client_secret = require_value("client secret", args.client_secret, "TESLA_CLIENT_SECRET")
    region = args.region or os.environ.get("TESLA_REGION") or DEFAULT_REGION
    scopes = args.scope or list(DEFAULT_PARTNER_SCOPES)

    async with aiohttp.ClientSession() as session:
        token = await get_partner_token(session, client_id, client_secret, region, scopes)
        api = TeslaFleetApi(
            session=session,
            access_token=token,
            region=region,
            charging_scope=False,
            energy_scope=False,
            user_scope=False,
            vehicle_scope=False,
        )
        if args.partner_command == "register":
            result = await api.partner.register(args.domain)
        elif args.partner_command == "public-key":
            result = await api.partner.public_key(args.domain)
        else:
            raise CliError(f"Unknown partner command: {args.partner_command}")

    if args.json:
        print_json(result)
    elif args.partner_command == "register":
        print(f"Registered Tesla partner domain: {args.domain}")
    else:
        print_json(result)
    return 0


async def energy_command(args: argparse.Namespace) -> int:
    async with aiohttp.ClientSession() as session:
        api, _, _ = await get_api(session, args)
        sites = await collect_energy_sites(api, args.site, include_device=args.device)
    if args.json:
        print_json({"energy_sites": sites})
    else:
        print_energy_summary(sites)
    return 0


async def cars_command(args: argparse.Namespace) -> int:
    async with aiohttp.ClientSession() as session:
        api, _, _ = await get_api(session, args)
        cars = await collect_cars(api, wake=args.wake, wake_delay=args.wake_delay)
    if args.json:
        print_json({"cars": cars})
    else:
        print_car_summary(cars)
    return 0


async def metrics_command(args: argparse.Namespace) -> int:
    async with aiohttp.ClientSession() as session:
        api, _, _ = await get_api(session, args)
        energy_task = collect_energy_sites(api, args.site, include_device=args.device)
        cars_task = collect_cars(api, wake=args.wake, wake_delay=args.wake_delay)
        sites, cars = await asyncio.gather(energy_task, cars_task)

    if args.json:
        print_json({"energy_sites": sites, "cars": cars})
    else:
        print_energy_summary(sites)
        print()
        print("Cars")
        print_car_summary(cars)
    return 0


async def device_command(args: argparse.Namespace) -> int:
    params = json.loads(args.params) if args.params else None
    async with aiohttp.ClientSession() as session:
        api, _, _ = await get_api(session, args)
        site = api.energySites.create(args.site)
        result = await site._command(
            args.category,
            args.command,
            params=params,
            identifier_type=args.identifier_type,
        )

    if args.json:
        print_json(result)
    else:
        print_json(result)
    return 0


def default_rsa_key_path() -> Path:
    return default_config_path().parent / "tedapi_rsa_private.pem"


def load_or_create_rsa_key(path: Path) -> tuple[Any, bytes, bool]:
    """Return (private_key, public_key_der_pkcs1, created).

    Loads an existing RSA private key from ``path`` or generates a new
    RSA-4096 key there. The public key is returned in DER PKCS1 form, which
    is the format the gateway stores and reports for authorized clients.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    created = False
    if path.exists():
        private_key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    else:
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=4096)
        pem = private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        # Create with 0o600 at open time so the private key is never briefly
        # world-readable between write and chmod.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(pem)
        os.chmod(path, 0o600)
        created = True

    public_key_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.PKCS1,
    )
    return private_key, public_key_der, created


def _collect_client_states(obj: Any) -> list[tuple[str, int]]:
    """Recursively pull (public_key, state) pairs from a gateway auth response.

    Tolerates Tesla's mixed PascalCase/snake_case response shapes.
    """
    found: list[tuple[str, int]] = []
    if isinstance(obj, dict):
        pub = obj.get("public_key", obj.get("PublicKey"))
        state = obj.get("state", obj.get("State"))
        if isinstance(pub, str) and state is not None:
            try:
                found.append((pub, int(state)))
            except (TypeError, ValueError):
                pass
        for value in obj.values():
            found.extend(_collect_client_states(value))
    elif isinstance(obj, list):
        for value in obj:
            found.extend(_collect_client_states(value))
    return found


def match_client_state(result: Any, pubkey_b64: str) -> int | None:
    """Return the authorization state for our public key, or None if absent.

    State values: 1 PENDING, 2 PENDING_VERIFICATION, 3 VERIFIED.
    """
    for pub, state in _collect_client_states(result):
        if pub == pubkey_b64:
            return state
    return None


def _find_first_key(obj: Any, target: str) -> Any:
    """Return the first value for ``target`` anywhere in a nested response."""
    if isinstance(obj, dict):
        if target in obj:
            return obj[target]
        for value in obj.values():
            found = _find_first_key(value, target)
            if found is not None:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_first_key(value, target)
            if found is not None:
                return found
    return None


def _int_to_ipv4(value: Any) -> str | None:
    try:
        packed = int(value)
    except (TypeError, ValueError):
        return None
    if packed <= 0:
        return None
    return ".".join(str((packed >> shift) & 0xFF) for shift in (24, 16, 8, 0))


def discover_gateway_ip(networking_status: Any) -> str | None:
    """Pick a reachable gateway IPv4 from a get_networking_status response.

    Prefers an interface with an active route; falls back to any interface
    carrying a non-zero address.
    """
    candidates: list[tuple[bool, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            cfg = node.get("ipv4_config")
            if isinstance(cfg, dict) and cfg.get("address"):
                candidates.append((bool(node.get("active_route")), cfg["address"]))
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(networking_status)
    candidates.sort(key=lambda item: not item[0])  # active routes first
    for _active, address in candidates:
        ip = _int_to_ipv4(address)
        if ip and not ip.startswith("0."):
            return ip
    return None


def _build_cloud_v1r_transport(
    rsa_key_path: str,
    timeout: int,
    fleet_base: str,
    token: str,
    site_id: int,
) -> Any:
    """A v1r transport that delivers the RSA-signed RoutableMessage over the
    Tesla cloud (Hermes) via the Fleet ``device_command`` endpoint instead of
    the local ``/tedapi/v1r`` HTTPS path. This is how the cloud diagnostics
    apps read string data remotely: same signature, same key, cloud relay.
    """
    import math
    import uuid

    import requests
    from pypowerwall.tedapi import tedapi_v1r as _v1r_mod

    # Reuse whatever protobuf module the installed transport already binds,
    # so the RoutableMessage type matches across pypowerwall versions.
    TEDAPIv1r = _v1r_mod.TEDAPIv1r
    combined_pb2 = _v1r_mod.combined_pb2

    class _CloudV1r(TEDAPIv1r):
        def login(self) -> bool:  # never needed: the RSA signature is the credential
            return True

        def get_din(self):  # DIN comes from the Fleet API, not /tedapi/din
            return None

        def post_v1r(self, envelope_bytes: bytes, din: str):
            routable = combined_pb2.RoutableMessage()
            routable.to_destination.domain = combined_pb2.DOMAIN_ENERGY_DEVICE
            # Cloud/Hermes needs an explicit routing target (the gateway DIN);
            # the local transport omits this because the gateway is the direct peer.
            try:
                routable.to_destination.routing_address = din.encode()
            except Exception:  # noqa: BLE001
                pass
            routable.protobuf_message_as_bytes = envelope_bytes
            routable.uuid = str(uuid.uuid4()).encode()

            expires_at = math.ceil(time.time()) + 12
            tlv_payload = self._build_tlv_payload(din, expires_at, routable.protobuf_message_as_bytes)
            signature = self._sign(tlv_payload)
            routable.signature_data.signer_identity.public_key = self._public_key_der
            routable.signature_data.rsa_data.expires_at = expires_at
            routable.signature_data.rsa_data.signature = signature

            payload_b64 = base64.b64encode(routable.SerializeToString()).decode("ascii")
            url = f"{fleet_base}/api/1/energy_sites/{site_id}/device_command"
            try:
                resp = requests.post(
                    url,
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                    json={"data": payload_b64},
                    timeout=timeout,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[cloud] device_command request error: {exc}", file=sys.stderr)
                return None

            if resp.status_code != 200:
                print(f"[cloud] device_command HTTP {resp.status_code}: {resp.text[:400]}", file=sys.stderr)
                return None

            try:
                data = resp.json()
            except ValueError:
                print(f"[cloud] non-JSON response: {resp.text[:400]}", file=sys.stderr)
                return None

            reply_b64 = data.get("response") if isinstance(data, dict) else None
            if not isinstance(reply_b64, str):
                print(f"[cloud] unexpected device_command response shape: {json.dumps(data)[:400]}", file=sys.stderr)
                return None

            try:
                reply = combined_pb2.RoutableMessage()
                reply.ParseFromString(base64.b64decode(reply_b64))
            except Exception as exc:  # noqa: BLE001
                print(f"[cloud] could not decode reply RoutableMessage: {exc}", file=sys.stderr)
                return None

            fault = reply.signed_message_status.message_fault
            if fault != combined_pb2.MESSAGEFAULT_ERROR_NONE:
                name = combined_pb2.MessageFault_E.Name(fault)
                print(f"[cloud] gateway fault: {name}", file=sys.stderr)
                return None

            inner = reply.protobuf_message_as_bytes
            if inner and b"authorization not verified" in inner.lower():
                print("[cloud] RSA key registered but not VERIFIED by the gateway.", file=sys.stderr)
                return None
            return inner or None

    return _CloudV1r(host="cloud", password="unused", rsa_key_path=rsa_key_path, timeout=timeout)


def fetch_pw3_vitals(
    host: str,
    din: str,
    rsa_key_path: str,
    timeout: int,
    via: str = "local",
    fleet_base: str | None = None,
    token: str | None = None,
    site_id: int | None = None,
) -> dict[str, Any] | None:
    """Read Powerwall 3 string vitals over the RSA-signed v1r channel.

    Authenticated purely by the registered RSA key; the DIN is supplied from
    the Fleet API so no gateway password / TEDAPI login is required. ``via``
    selects the transport: ``local`` POSTs to the gateway over the LAN,
    ``cloud`` relays the same signed message through the Fleet API. Returns
    the leader inverter's data (followers need a WiFi session, same limit as
    the cloud diagnostics apps).
    """
    try:
        from pypowerwall.tedapi import TEDAPI
    except ImportError as exc:
        raise CliError("Install local Powerwall support with `pip install -e '.[local]'`.") from exc

    import logging

    # The v1r constructor runs a connect() that tries a password login we do
    # not use; silence its expected failure noise, then drive the signed path.
    pw_logger = logging.getLogger("pypowerwall")
    previous = pw_logger.level
    pw_logger.setLevel(logging.CRITICAL)
    try:
        tedapi = TEDAPI(
            host=host,
            v1r=True,
            password="unused",
            rsa_key_path=rsa_key_path,
            timeout=timeout,
        )
        if via == "cloud":
            if not (fleet_base and token and site_id):
                raise CliError("Cloud transport needs Fleet base URL, token, and site ID.")
            tedapi.v1r_transport = _build_cloud_v1r_transport(
                rsa_key_path, timeout, fleet_base, token, site_id
            )
        tedapi.din = din  # pre-seed so the password-gated connect()/login() is skipped
    finally:
        pw_logger.setLevel(previous)

    return tedapi.get_pw3_vitals(force=True)


def _fmt_num(value: Any) -> str:
    """Round floats to one decimal for display; leave non-numbers untouched."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    return f"{round(value, 1):g}"


def print_pw3_strings(
    vitals: dict[str, Any] | None,
    din: str,
    host: str,
    solar_meter_w: float | None = None,
) -> None:
    if not vitals:
        print("No inverter/string data returned.")
        return
    inverters = {key: block for key, block in vitals.items() if key.startswith("PVAC--")}
    if not inverters:
        print("No inverter (PVAC) data in the vitals payload.")
        return

    # Per-string connected flags come from the PVS block(s).
    connected: dict[str, bool] = {}
    for key, block in vitals.items():
        if key.startswith("PVS--"):
            for letter in PV_STRING_NAMES:
                value = block.get(f"PVS_String{letter}_Connected")
                if value is not None:
                    connected[letter] = bool(value)

    string_total = 0.0
    for key, block in inverters.items():
        serial = block.get("serialNumber") or key.split("--")[-1]
        print(f"Inverter serial {mask_serial(serial)}")
        for index, letter in enumerate(PV_STRING_NAMES, start=1):
            voltage = block.get(f"PVAC_PVMeasuredVoltage_{letter}")
            current = block.get(f"PVAC_PVCurrent_{letter}")
            power = block.get(f"PVAC_PVMeasuredPower_{letter}")
            state = block.get(f"PVAC_PvState_{letter}")
            if voltage is None and current is None and power is None:
                continue
            annotations = []
            if state and state != "Pv_Active":
                annotations.append(state)
            if connected.get(letter) is False:
                annotations.append("disconnected")
            note = f"  [{', '.join(annotations)}]" if annotations else ""
            print(f"  String {index}: {_fmt_num(voltage)} V, {_fmt_num(current)} A, {_fmt_num(power)} W{note}")
            if isinstance(power, (int, float)):
                string_total += power
        pout = block.get("PVAC_Pout")
        vout = block.get("PVAC_Vout")
        fout = block.get("PVAC_Fout")
        if pout is not None:
            print(f"  Inverter AC out: {_fmt_num(pout)} W, {_fmt_num(vout)} V, {_fmt_num(fout)} Hz")

    if isinstance(solar_meter_w, (int, float)):
        # DC strings + AC-coupled = metered solar total; show it as an addition.
        # Round first and derive AC as (total - DC) so the printed column adds up
        # exactly rather than drifting by 0.1 from independent rounding.
        dc_r = round(string_total, 1)
        total_r = round(solar_meter_w, 1)
        ac_r = round(total_r - dc_r, 1)
        rows = [("DC strings", dc_r), ("AC-coupled", ac_r)]
        total_row = ("Total solar", total_r)
        label_w = max(len(label) for label, _ in [*rows, total_row]) + 1
        val_w = max(len(f"{value:.1f}") for _, value in [*rows, total_row])

        def fmt_row(label: str, value: float) -> str:
            return f"  {label + ':':<{label_w}} {value:>{val_w}.1f} W"

        print(fmt_row(*rows[0]))
        print(fmt_row(*rows[1]))
        print("  " + "-" * (label_w + val_w + 3))
        print(fmt_row(*total_row))
    else:
        print(f"  DC strings: {_fmt_num(string_total)} W")
    print(f"  (host {host}, din {mask_serial(din)})")


async def strings_command(args: argparse.Namespace) -> int:
    key_path = Path(args.rsa_key_path).expanduser() if args.rsa_key_path else default_rsa_key_path()
    if not key_path.exists():
        raise CliError(f"No RSA key at {key_path}. Run `energyscraper pair --site {args.site}` first.")

    fleet_base = None
    token = None
    async with aiohttp.ClientSession() as session:
        api, _, _ = await get_api(session, args)
        site = api.energySites.create(args.site)
        info = await site.get_system_info()
        din = _find_first_key(info, "din")
        try:
            live = await site.live_status()
            solar_meter_w = _find_first_key(live, "solar_power")
        except TeslaFleetError:
            solar_meter_w = None
        host = args.host or os.environ.get("PW_HOST")
        if args.via == "local" and not host:
            net = await site.get_networking_status()
            host = discover_gateway_ip(net)
        if args.via == "cloud":
            fleet_base = api.server
            token = api._access_token  # pyright: ignore[reportPrivateUsage]

    if not din:
        raise CliError("Could not determine the gateway DIN from Fleet API.")
    if args.via == "local" and not host:
        raise CliError("Could not determine the gateway IP; pass --host.")
    if args.via == "cloud" and not (fleet_base and token):
        raise CliError("Could not resolve the Fleet API base URL or access token for cloud mode.")

    vitals = fetch_pw3_vitals(
        host or "cloud",
        din,
        str(key_path),
        args.timeout,
        via=args.via,
        fleet_base=fleet_base,
        token=token,
        site_id=args.site,
    )
    if args.json:
        print_json({"solar_power_meter_w": solar_meter_w, "vitals": vitals or {}})
    else:
        print_pw3_strings(vitals, din, host, solar_meter_w)
    return 0 if vitals else 1


async def pair_command(args: argparse.Namespace) -> int:
    key_path = Path(args.rsa_key_path).expanduser() if args.rsa_key_path else default_rsa_key_path()
    private_key, public_key_der, created = load_or_create_rsa_key(key_path)
    del private_key  # only the on-disk private key is needed later, by pypowerwall
    pubkey_b64 = base64.b64encode(public_key_der).decode("ascii")

    print(f"RSA key: {key_path}" + ("  (generated)" if created else "  (reused)"))

    async with aiohttp.ClientSession() as session:
        api, _, _ = await get_api(session, args)
        site = api.energySites.create(args.site)

        existing = await site.list_authorized_clients()
        state = match_client_state(existing, pubkey_b64)
        if state == 3:
            print(f"This key is already registered and VERIFIED on site {args.site}.")
            print("Ready for local v1r use with:")
            print(f"  energyscraper local --host <gateway-ip> --rsa-key-path {key_path}")
            return 0
        if state is not None:
            print(f"This key is already registered (state {state}); continuing to verification.")
        elif not args.yes:
            print()
            print("This will WRITE a new authorized client (RSA public key) to your gateway.")
            print(f"  Site:        {args.site}")
            print(f"  Description: {args.description}")
            reply = input("Proceed with the gateway write? [y/N]: ").strip().lower()
            if reply not in ("y", "yes"):
                print("Aborted. No write sent.")
                return 1

        if state is None:
            result = await site.add_authorized_client(public_key_der, description=args.description)
            state = match_client_state(result, pubkey_b64)
            if state == 3:
                print("Key registered and auto-verified via cloud. No breaker toggle needed.")

        if state != 3:
            print("Polling for verification (cloud may auto-verify)...")
            for attempt in range(args.poll_attempts):
                await asyncio.sleep(args.poll_delay)
                listing = await site.list_authorized_clients()
                state = match_client_state(listing, pubkey_b64)
                print(f"  attempt {attempt + 1}/{args.poll_attempts}: state {state}")
                if state == 3:
                    break

    if state == 3:
        print()
        print("VERIFIED. Key is now authorized on the gateway.")
        print("Next, read strings locally over your LAN:")
        print(f"  energyscraper local --host <gateway-ip> --rsa-key-path {key_path}")
        return 0

    print()
    print(f"Key registered but not yet VERIFIED (state {state}).")
    print("If the cloud did not auto-verify, toggle any Powerwall breaker OFF then")
    print("ON within 30 seconds, then re-run `energyscraper pair` to confirm.")
    return 1


async def unpair_command(args: argparse.Namespace) -> int:
    if args.public_key:
        pubkey_b64 = args.public_key
        source = "from --public-key"
    else:
        key_path = Path(args.rsa_key_path).expanduser() if args.rsa_key_path else default_rsa_key_path()
        if not key_path.exists():
            raise CliError(f"No RSA key at {key_path}. Pass --public-key to remove another client.")
        _, public_key_der, _ = load_or_create_rsa_key(key_path)
        pubkey_b64 = base64.b64encode(public_key_der).decode("ascii")
        source = f"from {key_path}"

    async with aiohttp.ClientSession() as session:
        api, _, _ = await get_api(session, args)
        site = api.energySites.create(args.site)

        listing = await site.list_authorized_clients()
        state = match_client_state(listing, pubkey_b64)
        if state is None:
            print(f"Key ({source}) is not registered on site {args.site}. Nothing to remove.")
            return 0
        print(f"Key ({source}) is registered with state {state}.")

        if not args.yes:
            print()
            print("This will WRITE to your gateway: remove this authorized client key.")
            reply = input("Proceed with the removal? [y/N]: ").strip().lower()
            if reply not in ("y", "yes"):
                print("Aborted. No write sent.")
                return 1

        await site._command(
            "authorization",
            "remove_authorized_client_request",
            {"key_type": 1, "public_key": pubkey_b64},
        )

        listing = await site.list_authorized_clients()
        state = match_client_state(listing, pubkey_b64)

    if state is None:
        print("Removed. The key is no longer in the gateway's authorized client list.")
        return 0
    print(f"Removal sent, but the key still shows state {state}. Re-run to retry, or")
    print("check the raw response with `energyscraper device-command --category")
    print("authorization --command list_authorized_clients_request --json`.")
    return 1


async def local_command(args: argparse.Namespace) -> int:
    try:
        import pypowerwall
    except ImportError as exc:
        raise CliError("Install local Powerwall support with `pip install -e '.[local]'`.") from exc

    kwargs = {
        "host": args.host,
        "password": args.password,
        "email": args.email,
        "timezone": args.timezone,
        "gw_pwd": args.gateway_password,
        "rsa_key_path": args.rsa_key_path,
        "auto_select": True,
    }
    kwargs = {key: value for key, value in kwargs.items() if value is not None}
    powerwall = pypowerwall.Powerwall(**kwargs)

    data: dict[str, Any] = {
        "site_name": safe_call(powerwall.site_name),
        "firmware": safe_call(powerwall.version),
        "battery_level": safe_call(powerwall.level),
        "power": safe_call(powerwall.power),
        "strings": safe_call(powerwall.strings),
        "strings_verbose": safe_call(powerwall.strings, False, True),
        "vitals": safe_call(powerwall.vitals),
        "alerts": safe_call(powerwall.alerts),
    }

    if args.json:
        print_json(data)
    else:
        print_local_summary(data)
    return 0


def safe_call(func: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return func(*args, **kwargs)
    except Exception as exc:
        return {"error": str(exc)}


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="Path to token config file.")
    parser.add_argument("--region", choices=["na", "eu", "cn"], help="Tesla Fleet API region.")
    parser.add_argument("--client-id", help="Tesla developer app client ID. Can also use TESLA_CLIENT_ID.")
    parser.add_argument("--token-url", help="OAuth token URL for refresh. Use 'auth', 'fleet', or a full URL.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Tesla energy and vehicle metrics CLI.")
    subparsers = parser.add_subparsers(dest="command")

    auth = subparsers.add_parser("auth", help="Manage Tesla OAuth tokens.")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)

    login_parser = auth_sub.add_parser("login", help="Authenticate with Tesla Fleet OAuth.")
    add_common_args(login_parser)
    login_parser.add_argument("--client-secret", help="Tesla developer app client secret. Can also use TESLA_CLIENT_SECRET.")
    login_parser.add_argument("--redirect-uri", help="Tesla developer app redirect URI. Can also use TESLA_REDIRECT_URI.")
    login_parser.add_argument("--oauth-flow", choices=["library", "pkce"], default="library", help="OAuth implementation to use.")
    login_parser.add_argument("--scope", action="append", help="OAuth scope. Can be passed multiple times.")
    login_parser.add_argument("--code", help="Authorization code or redirected URL.")
    login_parser.add_argument("--state", help="OAuth state. Defaults to a random value.")
    login_parser.add_argument("--nonce", help="OIDC nonce. Defaults to a random value.")
    login_parser.set_defaults(func=login)

    status_parser = auth_sub.add_parser("status", help="Show local auth config status.")
    status_parser.add_argument("--config", help="Path to token config file.")
    status_parser.set_defaults(func=auth_status)

    logout_parser = auth_sub.add_parser("logout", help="Delete local auth config.")
    logout_parser.add_argument("--config", help="Path to token config file.")
    logout_parser.set_defaults(func=auth_logout)

    metrics_parser = subparsers.add_parser("metrics", help="Show energy and car metrics.")
    add_common_args(metrics_parser)
    metrics_parser.add_argument("--site", action="append", type=int, help="Energy site ID. Can be passed multiple times.")
    metrics_parser.add_argument("--device", action="store_true", help="Also request gateway device config/status commands.")
    metrics_parser.add_argument("--wake", action="store_true", help="Wake sleeping cars before requesting charge state.")
    metrics_parser.add_argument("--wake-delay", type=float, default=8.0, help="Seconds to wait after a wake request.")
    metrics_parser.add_argument("--json", action="store_true", help="Print raw JSON.")
    metrics_parser.set_defaults(func=metrics_command)

    energy_parser = subparsers.add_parser("energy", help="Show energy site metrics.")
    add_common_args(energy_parser)
    energy_parser.add_argument("--site", action="append", type=int, help="Energy site ID. Can be passed multiple times.")
    energy_parser.add_argument("--device", action="store_true", help="Also request gateway device config/status commands.")
    energy_parser.add_argument("--json", action="store_true", help="Print raw JSON.")
    energy_parser.set_defaults(func=energy_command)

    cars_parser = subparsers.add_parser("cars", help="List cars with model, name, and battery percent.")
    add_common_args(cars_parser)
    cars_parser.add_argument("--wake", action="store_true", help="Wake sleeping cars before requesting charge state.")
    cars_parser.add_argument("--wake-delay", type=float, default=8.0, help="Seconds to wait after a wake request.")
    cars_parser.add_argument("--json", action="store_true", help="Print raw JSON.")
    cars_parser.set_defaults(func=cars_command)

    products_parser = subparsers.add_parser("products", help="Show Tesla products attached to the account.")
    add_common_args(products_parser)
    products_parser.add_argument(
        "--probe-devices",
        action="store_true",
        help="Run read-only system-info probes for the site's known Fleet device target types.",
    )
    products_parser.add_argument("--json", action="store_true", help="Print raw JSON.")
    products_parser.set_defaults(func=products_command)

    partner = subparsers.add_parser("partner", help="Register or check the Tesla partner domain.")
    partner_sub = partner.add_subparsers(dest="partner_command", required=True)

    partner_register = partner_sub.add_parser("register", help="Register the hosted public key with Tesla.")
    partner_register.add_argument("--region", choices=["na", "eu", "cn"], help="Tesla Fleet API region.")
    partner_register.add_argument("--client-id", help="Tesla developer app client ID. Can also use TESLA_CLIENT_ID.")
    partner_register.add_argument("--client-secret", help="Tesla developer app client secret. Can also use TESLA_CLIENT_SECRET.")
    partner_register.add_argument("--scope", action="append", help="Partner token scope. Can be passed multiple times.")
    partner_register.add_argument("--domain", required=True, help="Domain hosting the Tesla public key, for example your-domain.example.")
    partner_register.add_argument("--json", action="store_true", help="Print raw JSON.")
    partner_register.set_defaults(func=partner_command)

    partner_key = partner_sub.add_parser("public-key", help="Fetch Tesla's registered public key for a domain.")
    partner_key.add_argument("--region", choices=["na", "eu", "cn"], help="Tesla Fleet API region.")
    partner_key.add_argument("--client-id", help="Tesla developer app client ID. Can also use TESLA_CLIENT_ID.")
    partner_key.add_argument("--client-secret", help="Tesla developer app client secret. Can also use TESLA_CLIENT_SECRET.")
    partner_key.add_argument("--scope", action="append", help="Partner token scope. Can be passed multiple times.")
    partner_key.add_argument("--domain", required=True, help="Domain hosting the Tesla public key, for example your-domain.example.")
    partner_key.add_argument("--json", action="store_true", help="Print raw JSON.")
    partner_key.set_defaults(func=partner_command)

    raw_parser = subparsers.add_parser("device-command", help="Run a raw energy gateway gRPC command via Fleet API.")
    add_common_args(raw_parser)
    raw_parser.add_argument("--site", type=int, required=True, help="Energy site ID.")
    raw_parser.add_argument("--category", required=True, help="Command category, for example common, teg, or energysitenet.")
    raw_parser.add_argument("--command", required=True, help="Command name, for example get_system_info_request.")
    raw_parser.add_argument("--params", help="JSON object for command params.")
    raw_parser.add_argument(
        "--identifier-type",
        type=int,
        choices=[int(value) for value in ENERGY_DEVICE_TARGETS],
        default=int(EnergyDeviceIdentifierType.GATEWAY_DIN),
        help="Target type: 1 gateway DIN, 2 site UUID, 3 solar inverter DIN, 4 Wall Connector DIN.",
    )
    raw_parser.add_argument("--json", action="store_true", help="Print raw JSON.")
    raw_parser.set_defaults(func=device_command)

    pair_parser = subparsers.add_parser("pair", help="Register an RSA key with the gateway for local v1r (Powerwall 3) access.")
    add_common_args(pair_parser)
    pair_parser.add_argument("--site", type=int, required=True, help="Energy site ID.")
    pair_parser.add_argument("--rsa-key-path", help="RSA private key path. Defaults to the config dir's tedapi_rsa_private.pem.")
    pair_parser.add_argument("--description", default="energyscraper LAN client", help="Human-readable client description stored on the gateway.")
    pair_parser.add_argument("--poll-attempts", type=int, default=6, help="Verification poll attempts after registration.")
    pair_parser.add_argument("--poll-delay", type=float, default=5.0, help="Seconds between verification polls.")
    pair_parser.add_argument("--yes", action="store_true", help="Skip the interactive confirmation before writing to the gateway.")
    pair_parser.set_defaults(func=pair_command)

    unpair_parser = subparsers.add_parser("unpair", help="Remove a previously paired RSA key from the gateway.")
    add_common_args(unpair_parser)
    unpair_parser.add_argument("--site", type=int, required=True, help="Energy site ID.")
    unpair_parser.add_argument("--rsa-key-path", help="RSA private key path. Defaults to the config dir's tedapi_rsa_private.pem.")
    unpair_parser.add_argument("--public-key", help="Base64 DER PKCS1 public key to remove instead of the local key file's.")
    unpair_parser.add_argument("--yes", action="store_true", help="Skip the interactive confirmation before writing to the gateway.")
    unpair_parser.set_defaults(func=unpair_command)

    strings_parser = subparsers.add_parser("strings", help="Read Powerwall 3 PV string vitals over the RSA-signed local v1r channel (no gateway password).")
    add_common_args(strings_parser)
    strings_parser.add_argument("--site", type=int, required=True, help="Energy site ID.")
    strings_parser.add_argument("--rsa-key-path", help="RSA private key path. Defaults to the config dir's tedapi_rsa_private.pem.")
    strings_parser.add_argument("--host", help="Gateway IP for local mode. Auto-discovered from Fleet networking status if omitted. Can also use PW_HOST.")
    strings_parser.add_argument("--via", choices=["local", "cloud"], default="local", help="Transport: 'local' talks to the gateway over the LAN (works). 'cloud' relays the signed query through the Fleet API device_command endpoint (EXPERIMENTAL: authenticates but the signed-energy 'data' schema is unconfirmed and currently returns HTTP 500).")
    strings_parser.add_argument("--timeout", type=int, default=15, help="Per-request timeout in seconds.")
    strings_parser.add_argument("--json", action="store_true", help="Print the raw vitals payload.")
    strings_parser.set_defaults(func=strings_command)

    local_parser = subparsers.add_parser("local", help="Read local Powerwall/TEDAPI metrics with optional pypowerwall support.")
    local_parser.add_argument("--host", default=os.environ.get("PW_HOST"), help="Powerwall host/IP. Can also use PW_HOST.")
    local_parser.add_argument("--email", default=os.environ.get("PW_EMAIL"), help="Tesla account email or gateway email. Can also use PW_EMAIL.")
    local_parser.add_argument("--password", default=os.environ.get("PW_PASSWORD"), help="Customer password, often last 5 chars of gateway password. Can also use PW_PASSWORD.")
    local_parser.add_argument("--gateway-password", default=os.environ.get("PW_GW_PWD"), help="Full gateway Wi-Fi password from QR sticker. Can also use PW_GW_PWD.")
    local_parser.add_argument("--timezone", default=os.environ.get("PW_TIMEZONE", "UTC"), help="Timezone for local Powerwall calls.")
    local_parser.add_argument("--rsa-key-path", default=os.environ.get("PW_RSA_KEY_PATH"), help="RSA private key path for Powerwall 3 v1r mode.")
    local_parser.add_argument("--json", action="store_true", help="Print raw JSON.")
    local_parser.set_defaults(func=local_command)

    parser.set_defaults(func=metrics_command)
    return parser


async def async_main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        args.command = "metrics"
        args.config = None
        args.region = None
        args.client_id = None
        args.site = None
        args.device = False
        args.wake = False
        args.wake_delay = 8.0
        args.json = False
    try:
        return await args.func(args)
    except CliError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except TeslaFleetError as exc:
        detail = f" ({exc.status})" if exc.status else ""
        print(f"Tesla API error{detail}: {exc.message}", file=sys.stderr)
        if exc.data:
            print_json(exc.data)
        return 1


def main() -> None:
    raise SystemExit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
