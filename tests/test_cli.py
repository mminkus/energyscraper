from __future__ import annotations

import io
import unittest
from contextlib import redirect_stdout

from tesla_fleet_api.const import EnergyDeviceIdentifierType

from datetime import datetime

from energyscraper.cli import (
    sell_rate_now,
    _find_first_key,
    _fmt_num,
    _int_to_ipv4,
    discover_gateway_ip,
    match_client_state,
    print_products_summary,
    print_pw3_strings,
    product_probe_targets,
    render_prometheus,
    summarize_system_info_probe,
)


HOME_PRODUCT = {
    "energy_site_id": 1234,
    "resource_type": "battery",
    "site_name": "My Home",
    "id": "STE123",
    "asset_site_id": "asset-uuid",
    "gateway_id": "1707000-11-M--TG123",
    "battery_type": "penguin",
    "charge_on_solar_capable": True,
    "components": {
        "battery": True,
        "solar": True,
        "grid": True,
        "load_meter": True,
        "gateways": [
            {
                "device_id": "device-uuid",
                "din": "1707000-11-M--TG123",
                "serial_number": "TG123",
                "part_number": "1707000-11-M",
                "is_active": True,
            }
        ],
    },
}


class ProductsSummaryTests(unittest.TestCase):
    def test_summary_includes_energy_topology_without_vehicle_cache(self) -> None:
        vehicle = {
            "vin": "5YJTEST0000FAKE01",
            "display_name": "Test Vehicle",
            "state": "asleep",
            "cached_data": "do-not-print",
        }
        output = io.StringIO()
        with redirect_stdout(output):
            print_products_summary([vehicle, HOME_PRODUCT])

        rendered = output.getvalue()
        self.assertIn("Test Vehicle", rendered)
        self.assertIn("5YJ***KE01", rendered)
        self.assertNotIn("do-not-print", rendered)
        self.assertIn("device-uuid", rendered)
        self.assertIn("site gateway", rendered)
        self.assertIn("Charge on Solar", rendered)

    def test_probe_targets_follow_reported_components(self) -> None:
        self.assertEqual(
            product_probe_targets(HOME_PRODUCT),
            [
                EnergyDeviceIdentifierType.GATEWAY_DIN,
                EnergyDeviceIdentifierType.SITE_UUID,
                EnergyDeviceIdentifierType.SOLAR_INVERTER_DIN,
            ],
        )

    def test_system_info_probe_summary(self) -> None:
        result = {
            "response": {
                "response": {
                    "message": {
                        "Payload": {
                            "Common": {
                                "Message": {
                                    "GetSystemInfoResponse": {
                                        "device_id": {
                                            "part_number": "1707000-11-M",
                                            "serial_number": "TG123",
                                        },
                                        "firmware_version": {"version": "26.18.1"},
                                        "device_type": 4,
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        self.assertEqual(
            summarize_system_info_probe(result),
            "TG123, part 1707000-11-M, firmware 26.18.1, device type 4",
        )


class GatewayDiscoveryTests(unittest.TestCase):
    # IPs below are RFC 5737 documentation ranges, not real hosts.
    # 3221225994 -> 192.0.2.10, 3325256709 -> 198.51.100.5
    def test_int_to_ipv4_big_endian(self) -> None:
        self.assertEqual(_int_to_ipv4(3221225994), "192.0.2.10")
        self.assertEqual(_int_to_ipv4(3325256709), "198.51.100.5")

    def test_int_to_ipv4_rejects_non_ips(self) -> None:
        self.assertIsNone(_int_to_ipv4(0))
        self.assertIsNone(_int_to_ipv4(None))
        self.assertIsNone(_int_to_ipv4("nope"))

    def test_discover_prefers_active_route(self) -> None:
        net = {
            "response": {
                "message": {
                    "wifi": {"active_route": True, "ipv4_config": {"address": 3221225994}},
                    "eth": {"active_route": False, "ipv4_config": {"address": 3325256709}},
                }
            }
        }
        self.assertEqual(discover_gateway_ip(net), "192.0.2.10")

    def test_discover_falls_back_to_any_nonzero(self) -> None:
        net = {"eth": {"active_route": False, "ipv4_config": {"address": 3325256709}}}
        self.assertEqual(discover_gateway_ip(net), "198.51.100.5")

    def test_discover_returns_none_when_absent(self) -> None:
        self.assertIsNone(discover_gateway_ip({"eth": {"ipv4_config": {"address": 0}}}))


class HelperTests(unittest.TestCase):
    def test_find_first_key_nested(self) -> None:
        obj = {"a": {"b": [{"din": "TG123"}]}}
        self.assertEqual(_find_first_key(obj, "din"), "TG123")
        self.assertIsNone(_find_first_key(obj, "missing"))

    def test_fmt_num_rounds_floats(self) -> None:
        self.assertEqual(_fmt_num(244.20000000000002), "244.2")
        self.assertEqual(_fmt_num(60.0025), "60")
        self.assertEqual(_fmt_num(10), "10")
        self.assertEqual(_fmt_num("Pv_Standby"), "Pv_Standby")

    def test_match_client_state_matches_our_key(self) -> None:
        listing = {
            "response": {
                "message": {
                    "Payload": {
                        "Authorization": {
                            "Message": {
                                "ListAuthorizedClientsResponse": {
                                    "clients": [
                                        {"public_key": "phone", "state": 3},
                                        {"public_key": "ours", "state": 1},
                                    ]
                                }
                            }
                        }
                    }
                }
            }
        }
        self.assertEqual(match_client_state(listing, "ours"), 1)
        self.assertEqual(match_client_state(listing, "phone"), 3)
        self.assertIsNone(match_client_state(listing, "absent"))


class Pw3StringsTests(unittest.TestCase):
    def _vitals(self) -> dict:
        block = {
            "serialNumber": "TG0000000000TEST",
            "partNumber": "1707000-11-M",
            "PVAC_Pout": 3516,
            "PVAC_Vout": 244.20000000000002,
            "PVAC_Fout": 60.0025,
        }
        powers = {"A": 400, "B": 1046, "C": 2070}
        for letter in ("A", "B", "C", "D", "E", "F"):
            block[f"PVAC_PVMeasuredVoltage_{letter}"] = 166.0
            block[f"PVAC_PVCurrent_{letter}"] = 6.3
            block[f"PVAC_PVMeasuredPower_{letter}"] = powers.get(letter, 0)
            block[f"PVAC_PvState_{letter}"] = "Pv_Active" if letter in powers else "Pv_Standby"
        pvs = {f"PVS_String{letter}_Connected": letter in powers for letter in ("A", "B", "C", "D", "E", "F")}
        return {
            "PVAC--1707000-11-M--TG0000000000TEST": block,
            "PVS--1707000-11-M--TG0000000000TEST": pvs,
        }

    def test_ac_coupled_breakout(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            print_pw3_strings(self._vitals(), "1707000-11-M--TG0000000000TEST", "192.0.2.10", solar_meter_w=4794)
        rendered = output.getvalue()
        self.assertIn("DC strings:", rendered)
        self.assertIn("3516.0 W", rendered)
        self.assertIn("AC-coupled:", rendered)
        # 4794 - (400+1046+2070) = 1278
        self.assertIn("1278.0 W", rendered)
        self.assertIn("Total solar:", rendered)
        self.assertIn("4794.0 W", rendered)
        self.assertIn("244.2 V", rendered)  # rounded
        self.assertIn("disconnected", rendered)  # strings D-F flagged

    def test_no_vitals(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            print_pw3_strings(None, "din", "host")
        self.assertIn("No inverter/string data returned.", output.getvalue())


class PrometheusRenderTests(unittest.TestCase):
    def _vitals(self) -> dict:
        block = {"serialNumber": "TG0000000000TEST", "PVAC_Pout": 40, "PVAC_Fout": 60}
        powers = {"A": 400, "B": 1046, "C": 2070}
        for letter in ("A", "B", "C", "D", "E", "F"):
            block[f"PVAC_PVMeasuredVoltage_{letter}"] = 150.0 if letter in powers else 0
            block[f"PVAC_PVCurrent_{letter}"] = 6.3 if letter in powers else 0
            block[f"PVAC_PVMeasuredPower_{letter}"] = powers.get(letter, 0)
            block[f"PVAC_PvState_{letter}"] = "Pv_Active"
        return {"PVAC--1707000-11-M--TG0000000000TEST": block}

    def test_render_contains_expected_metrics(self) -> None:
        cloud = {"solar_power": 4794, "load_power": 500, "battery_power": -100,
                 "grid_power": 0, "percentage_charged": 100}
        out = render_prometheus(self._vitals(), cloud, up=True)
        self.assertIn("energyscraper_up 1.0", out)
        self.assertIn('energyscraper_pv_string_power_watts{string="1"} 400', out)
        self.assertIn('energyscraper_pv_string_power_watts{string="3"} 2070', out)
        self.assertIn("energyscraper_solar_dc_watts 3516", out)
        self.assertIn("energyscraper_solar_total_watts 4794", out)
        self.assertIn("energyscraper_solar_ac_coupled_watts 1278", out)
        self.assertIn('energyscraper_site_power_watts{flow="grid"} 0', out)
        self.assertIn("energyscraper_powerwall_charge_percent 100", out)
        # Every metric family must carry a TYPE line.
        self.assertIn("# TYPE energyscraper_pv_string_power_watts gauge", out)

    def test_render_wall_connector_and_powershare(self) -> None:
        cloud = {"solar_power": 0, "load_power": 500, "wall_connectors": [
            {"wall_connector_power": -11500, "wall_connector_state": 1, "powershare_session_state": 3}]}
        out = render_prometheus(self._vitals(), cloud, up=True)
        # Negative power is the vehicle feeding the house.
        self.assertIn("energyscraper_wall_connector_power_watts -11500", out)
        self.assertIn("energyscraper_powershare_session_state 3", out)

    def test_sell_rate_lookup(self) -> None:
        site = {"tariff_content_v2": {"sell_tariff": {"energy_charges": {
            "September": {"rates": {"hour_19_weekend": 0.699, "hour_19_weekday": 0.6}}}}}}
        sat = datetime.fromisoformat("2026-09-19T19:22:35-07:00")
        fri = datetime.fromisoformat("2026-09-18T19:22:35-07:00")
        self.assertAlmostEqual(sell_rate_now(site, sat), 0.699)
        self.assertAlmostEqual(sell_rate_now(site, fri), 0.6)
        self.assertIsNone(sell_rate_now({}, sat))

    def test_render_site_info_metrics(self) -> None:
        site = {"battery_count": 2, "backup_reserve_percent": 15,
                "components": {"gateways": [{"nameplate_energy_watts": 27000,
                                             "nameplate_power_watts": 23040}]}}
        cloud = {"timestamp": "2026-09-19T19:22:35-07:00"}
        out = render_prometheus(None, cloud, up=False, site=site)
        self.assertIn("energyscraper_battery_count 2", out)
        self.assertIn("energyscraper_battery_nameplate_energy_watthours 27000", out)
        self.assertIn("energyscraper_battery_nameplate_power_watts 23040", out)
        self.assertIn("energyscraper_backup_reserve_percent 15", out)

    def test_render_raw_pack_energy(self) -> None:
        status = {"control": {"systemStatus": {"nominalEnergyRemainingWh": 4400,
                                               "nominalFullPackEnergyWh": 28750},
                              "islanding": {"contactorClosed": False}}}
        out = render_prometheus(None, {}, up=False, status=status)
        self.assertIn("energyscraper_pack_energy_remaining_watthours 4400", out)
        self.assertIn("energyscraper_pack_energy_full_watthours 28750", out)
        self.assertIn("energyscraper_island_contactor_closed 0.0", out)

    def test_ac_coupled_skipped_during_powershare(self) -> None:
        # The gateway folds the vehicle's output into the solar meter, so the
        # meter-minus-strings derivation would report a phantom array.
        cloud = {"solar_power": 8582, "wall_connectors": [
            {"wall_connector_power": -9027.24, "powershare_session_state": 2}]}
        out = render_prometheus(self._vitals(), cloud, up=True)
        self.assertIn("energyscraper_solar_total_watts 8582", out)
        self.assertNotIn("solar_ac_coupled_watts", out)
        self.assertIn("energyscraper_wall_connector_power_watts -9027.24", out)
        self.assertIn("energyscraper_powershare_session_state 2", out)

    def test_render_island_status(self) -> None:
        out = render_prometheus(self._vitals(), {"island_status": "off_grid"}, up=True)
        self.assertIn('energyscraper_island_status{status="off_grid"} 1.0', out)

    def test_render_skips_wall_connector_when_absent(self) -> None:
        out = render_prometheus(self._vitals(), {"wall_connectors": []}, up=True)
        self.assertNotIn("wall_connector_power_watts", out)

    def test_render_down_when_no_vitals(self) -> None:
        out = render_prometheus(None, {}, up=False)
        self.assertIn("energyscraper_up 0.0", out)
        self.assertNotIn("pv_string_power_watts", out)


if __name__ == "__main__":
    unittest.main()
