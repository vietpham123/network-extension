"""
NetBox -> network model mapping, pinned.

Every case here is a way the mapping can be silently WRONG rather than absent: a MAC in the wrong
case mints a duplicate Smartscape node that can never be merged, a planned cable becomes a real
dependency, a hypervisor shows up as a switch.
"""
import io
import json
import urllib.error

import pytest

from netbox_network.model import (
    build_neighbors, chassis_mac, device_dimensions, normalize_mac, primary_address,
)
from netbox_network.netbox import NetboxClient, NetboxError

HOSTS = {"server", "hypervisor", "storage"}


def dev(did, name, role, ip=None, status="active", **kw):
    d = {"id": did, "name": name, "role": {"slug": role}, "status": {"value": status},
         "custom_fields": {}, "device_type": {"model": "C9300", "part_number": "C9300-48P",
                                              "manufacturer": {"name": "Cisco"}}}
    if ip:
        d["primary_ip4"] = {"address": f"{ip}/24"}
    d.update(kw)
    return d


def term(device_id, otype="dcim.interface"):
    return {"object_type": otype, "object": {"device": {"id": device_id}}}


def cable(cid, a, b, status="connected"):
    return {"id": cid, "status": {"value": status},
            "a_terminations": a if isinstance(a, list) else [a],
            "b_terminations": b if isinstance(b, list) else [b]}


class TestMac:
    def test_matches_the_form_snmp_writes_to_smartscape(self):
        # test tenant 2026-11-10: chassis_mac = "02:0A:58:2B:01:01"
        for raw in ("02:0a:58:2b:01:01", "02-0A-58-2B-01-01", "020a.582b.0101", "020A582B0101"):
            assert normalize_mac(raw) == "02:0A:58:2B:01:01", raw

    def test_values_that_are_not_a_burned_in_address_give_None_not_a_guess(self):
        for bad in (None, "", "00:00:00:00:00:00", "FF:FF:FF:FF:FF:FF", "02:0A:58:2B:01",
                    "02:0A:58:2B:01:01:FF", "zz:zz:zz:zz:zz:zz", 12345):
            assert normalize_mac(bad) is None, bad


class TestIdentity:
    def test_custom_field_wins_over_mgmt_interface(self):
        d = dev(1, "sw", "access", custom_fields={"chassis_mac": "aa:bb:cc:dd:ee:01"})
        mgmt = [{"name": "mgmt0", "mac_address": "aa:bb:cc:dd:ee:99"}]
        assert chassis_mac(d, mgmt, "chassis_mac") == ("AA:BB:CC:DD:EE:01", "custom_field")

    def test_mgmt_fallback_reads_netbox_42_primary_mac_object(self):
        mgmt = [{"name": "mgmt0", "mac_address": None,
                 "primary_mac_address": {"mac_address": "aa:bb:cc:dd:ee:99"}}]
        assert chassis_mac(dev(1, "sw", "access"), mgmt, "chassis_mac") == ("AA:BB:CC:DD:EE:99", "mgmt_interface")

    def test_no_mac_anywhere_means_no_identity(self):
        # measured: 0 of 15 interfaces on a real NetBox 4.6 install carry a MAC. This is the common case.
        assert chassis_mac(dev(1, "sw", "access"), [{"name": "eth0"}], "chassis_mac") == (None, "none")

    def test_invalid_custom_field_falls_through_rather_than_being_trusted(self):
        d = dev(1, "sw", "access", custom_fields={"chassis_mac": "n/a"})
        assert chassis_mac(d, [], "chassis_mac") == (None, "none")


class TestDeviceDimensions:
    def test_no_dimension_is_null_or_empty(self):
        d = dev(1, "sw", "access", serial="", asset_tag=None)
        dims = device_dimensions(d, None, "none", HOSTS)
        assert all(isinstance(v, str) and v for v in dims.values())
        assert "chassis.mac" not in dims and "serial_number" not in dims

    def test_hosts_are_reported_but_flagged_as_not_network_devices(self):
        dims = device_dimensions(dev(1, "esx-9", "hypervisor", ip="10.9.9.9"), None, "none", HOSTS)
        assert dims["netbox.is_network_device"] == "false"

    def test_ipv6_only_device_still_has_an_address(self):
        d = dev(1, "sw", "access", primary_ip6={"address": "2001:db8::1/64"})
        assert primary_address(d) == "2001:db8::1"

    def test_netbox_3x_device_role_key(self):
        d = dev(1, "sw", "access")
        d["device_role"] = d.pop("role")
        assert device_dimensions(d, None, "none", HOSTS)["netbox.role"] == "access"


def _fleet():
    devs = {1: dev(1, "acc-1", "access", ip="10.0.0.1"), 2: dev(2, "core-1", "core", ip="10.0.0.2"),
            3: dev(3, "pdu-1", "pdu", ip="10.0.0.3"), 4: dev(4, "esx-1", "hypervisor", ip="10.0.0.4"),
            6: dev(6, "new-sw", "access", status="planned")}
    ident = {i: f"AA:BB:CC:DD:EE:0{i}" for i in devs}
    return devs, ident


def iface(device_id, name):
    return {"object_type": "dcim.interface", "object": {"name": name, "device": {"id": device_id}}}


class TestNeighbors:
    def test_a_cable_is_a_neighbour_in_both_directions(self):
        # Dynatrace's own LLDP discovery draws calls both ways (test tenant 2026-11-10: 26 of 37
        # device edges have their reverse). No role ranking, no imposed dependency direction.
        devs, ident = _fleet()
        res = build_neighbors([cable(10, iface(1, "Gi1/0/1"), iface(2, "Te1/1/1"))], devs, ident, HOSTS)
        pairs = {(n["sys.name"], n["neighbor.sys.name"]) for n in res.neighbors}
        assert pairs == {("acc-1", "core-1"), ("core-1", "acc-1")}
        fwd = next(n for n in res.neighbors if n["sys.name"] == "acc-1")
        assert fwd["chassis.mac"] == ident[1] and fwd["neighbor.chassis.mac"] == ident[2]
        assert (fwd["if.name"], fwd["neighbor.if.name"]) == ("Gi1/0/1", "Te1/1/1")

    def test_power_is_not_a_network_neighbour(self):
        devs, ident = _fleet()
        c = cable(12, term(3, "dcim.poweroutlet"), term(2, "dcim.powerport"))
        res = build_neighbors([c], devs, ident, HOSTS)
        assert res.neighbors == [] and res.skipped == {"power": 1}

    def test_planned_cable_is_not_topology(self):
        devs, ident = _fleet()
        res = build_neighbors([cable(13, term(1), term(2), status="planned")], devs, ident, HOSTS)
        assert res.neighbors == [] and res.skipped == {"cable_planned": 1}

    def test_cable_to_inactive_device_or_host_is_skipped_and_counted(self):
        devs, ident = _fleet()
        res = build_neighbors([cable(14, term(1), term(6)), cable(15, term(1), term(4))], devs, ident, HOSTS)
        assert res.neighbors == [] and res.skipped == {"inactive_device": 1, "host_end": 1}

    def test_lag_is_one_neighbour_pair_without_a_guessed_interface(self):
        devs, ident = _fleet()
        c = cable(16, [iface(1, "Gi1"), iface(1, "Gi2")], [iface(2, "Te1"), iface(2, "Te2")])
        res = build_neighbors([c], devs, ident, HOSTS)
        assert len(res.neighbors) == 2
        assert all("if.name" not in n and "neighbor.if.name" not in n for n in res.neighbors)

    def test_missing_mac_drops_the_dimension_not_the_signal(self):
        devs, ident = _fleet()
        ident[2] = None
        res = build_neighbors([cable(19, term(1), term(2))], devs, ident, HOSTS)
        fwd = next(n for n in res.neighbors if n["sys.name"] == "acc-1")
        assert "neighbor.chassis.mac" not in fwd   # pipeline skips the edge; the metric survives


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestClient:
    def test_follows_next_but_keeps_the_configured_host(self):
        seen = []
        pages = [{"results": [1], "next": "http://netbox-internal:8080/api/dcim/devices/?limit=1&offset=1"},
                 {"results": [2], "next": None}]

        def opener(req, timeout):
            seen.append(req.full_url)
            return _Resp(json.dumps(pages[len(seen) - 1]).encode())

        nb = NetboxClient("https://netbox.example.com/", "t", opener=opener)
        assert nb.all("/dcim/devices/") == [1, 2]
        assert seen[1] == "https://netbox.example.com/api/dcim/devices/?limit=1&offset=1"

    def test_a_failed_page_raises_instead_of_returning_a_partial_roster(self):
        calls = []

        def opener(req, timeout):
            calls.append(1)
            if len(calls) == 2:
                raise urllib.error.URLError("boom")
            return _Resp(json.dumps({"results": [1], "next": "http://x/api/p/?offset=1"}).encode())

        with pytest.raises(NetboxError):
            NetboxClient("http://x", "t", opener=opener).all("/p/")
