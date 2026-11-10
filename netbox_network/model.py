"""
NetBox records -> Dynatrace network model signals. Pure functions, no I/O.

What this is for: turning NetBox devices and cables into metric dimensions shaped for the
Smartscape network model (EXT_NETWORK_DEVICE + `calls` edges), so the bundled OpenPipeline
pipeline can enrich nodes and draw neighbour edges. Identity, eligibility and neighbour logic all
live here so they can be tested without the SDK or a NetBox.

What it must not do:
  - invent an identifier. A device whose chassis MAC is unknown gets no node identity (D2) — a
    guessed identifier mints a second node beside the one SNMP already created, and Smartscape
    nodes cannot be merged afterwards.
  - impose a direction on a cable. A cable is a neighbour relationship; it is emitted in both
    directions, matching how Dynatrace's own LLDP/CDP discovery draws `calls` edges (D5).

Field names come from the Semantic Dictionary network metrics model where one exists
(sys.name, device.address, chassis.mac, if.name); NetBox-only facts are namespaced netbox.* (D3).
Decision ids (D1..) refer to docs/DECISIONS.md.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

POWER_TERMINATIONS = {"dcim.poweroutlet", "dcim.powerport", "dcim.powerfeed"}

_HEX = re.compile(r"[0-9A-Fa-f]")


def normalize_mac(raw) -> str | None:
    """Canonical chassis-MAC form, or None when the value cannot be a real burned-in address.

    UPPERCASE, colon-separated — measured 2026-11-10 on a test tenant: every EXT_NETWORK_DEVICE
    written by the Dynatrace SNMP extensions carries chassis_mac like "02:0A:58:2B:01:01". The
    Smartscape id is derived from this string, so "02:0a:58..." would be a DIFFERENT node.
    """
    if not raw or not isinstance(raw, str):
        return None
    digits = "".join(_HEX.findall(raw))
    stripped = re.sub(r"[\s:.\-]", "", raw)
    if len(digits) != 12 or len(stripped) != 12:
        return None
    digits = digits.upper()
    if digits in ("000000000000", "FFFFFFFFFFFF"):
        return None
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2))


def interface_mac(iface: dict) -> str | None:
    """NetBox >= 4.2 moved MACs to their own objects; the interface carries primary_mac_address.
    Older NetBox has a plain mac_address string."""
    pm = iface.get("primary_mac_address")
    if isinstance(pm, dict) and pm.get("mac_address"):
        return normalize_mac(pm["mac_address"])
    return normalize_mac(iface.get("mac_address"))


def chassis_mac(device: dict, mgmt_ifaces: list[dict], field_name: str) -> tuple[str | None, str]:
    """(mac, identity.source). D2: explicit custom field first, then the management interface.

    The custom field is the reliable path: SNMP defines chassis MAC as "the first valid MAC of the
    device", an ordering NetBox cannot reproduce. tools/reconcile_chassis_mac.py fills it from what
    Smartscape already observed. The mgmt-interface fallback is a heuristic and says so in
    identity.source, so its nodes can be audited for duplicates.
    """
    cf = (device.get("custom_fields") or {}).get(field_name) if field_name else None
    mac = normalize_mac(cf)
    if mac:
        return mac, "custom_field"
    for iface in sorted(mgmt_ifaces, key=lambda i: i.get("name") or ""):
        mac = interface_mac(iface)
        if mac:
            return mac, "mgmt_interface"
    return None, "none"


def primary_address(device: dict) -> str | None:
    for key in ("primary_ip4", "primary_ip6"):
        addr = ((device.get(key) or {}).get("address") or "").split("/")[0]
        if addr:
            return addr
    return None


def _slug(obj) -> str | None:
    return obj.get("slug") if isinstance(obj, dict) else None


def role_slug(device: dict) -> str | None:
    # NetBox 4.x: role. 3.x: device_role.
    return _slug(device.get("role")) or _slug(device.get("device_role"))


def device_name(device: dict) -> str:
    # name is nullable in NetBox (unnamed devices); display is always set.
    return device.get("name") or device.get("display") or f"netbox-{device.get('id')}"


def is_active(device: dict) -> bool:
    return ((device.get("status") or {}).get("value")) == "active"


def is_network_device(device: dict, host_roles: set[str]) -> bool:
    """D4: NetBox is a whole-estate CMDB. Hypervisors and storage are hosts, not network devices;
    upserting them as EXT_NETWORK_DEVICE would list them in the Network devices view."""
    return (role_slug(device) or "") not in host_roles


def _clean(dims: dict) -> dict:
    # A dimension cannot be null, and an empty string would be written to the node as a value.
    return {k: str(v) for k, v in dims.items() if v not in (None, "")}


def device_dimensions(device: dict, mac: str | None, source: str, host_roles: set[str]) -> dict:
    """Dimensions for netbox.device. Node-field names for what NetBox owns (serial_number,
    part_number); everything else netbox.* so it never overwrites a field SNMP writes (D3)."""
    dt = device.get("device_type") or {}
    return _clean({
        "sys.name": device_name(device),
        "device.address": primary_address(device),
        "chassis.mac": mac,
        "identity.source": source,
        "netbox.id": device.get("id"),
        "netbox.status": (device.get("status") or {}).get("value"),
        "netbox.role": role_slug(device),
        "netbox.is_network_device": "true" if is_network_device(device, host_roles) else "false",
        "netbox.site": _slug(device.get("site")),
        "netbox.rack": (device.get("rack") or {}).get("name"),
        "netbox.tenant": _slug(device.get("tenant")),
        "netbox.platform": _slug(device.get("platform")),
        "netbox.manufacturer": (dt.get("manufacturer") or {}).get("name"),
        "netbox.device_type": dt.get("model"),
        "netbox.asset_tag": device.get("asset_tag"),
        "serial_number": device.get("serial"),
        "part_number": dt.get("part_number"),
    })


@dataclass
class NeighborResult:
    neighbors: list[dict] = field(default_factory=list)
    # every cable not turned into neighbours is counted by reason, never dropped silently: an
    # absent edge must be distinguishable from one we chose not to draw
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str):
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def _side(terms: list[dict]) -> tuple[set, bool, list[str]]:
    """(device ids, any power termination, interface names) for one cable side."""
    ids, power, names = set(), False, []
    for t in terms or []:
        if t.get("object_type") in POWER_TERMINATIONS:
            power = True
        obj = t.get("object") or {}
        did = (obj.get("device") or {}).get("id")
        if did is not None:
            ids.add(did)
        if t.get("object_type") == "dcim.interface" and obj.get("name"):
            names.append(obj["name"])
    return ids, power, sorted(names)


def _end(device: dict, mac, ifnames: list[str], prefix: str) -> dict:
    return {
        f"{prefix}sys.name": device_name(device),
        f"{prefix}device.address": primary_address(device),
        f"{prefix}chassis.mac": mac,
        # one name for a plain cable; a LAG/breakout has several, so if.name is left out rather
        # than picking one arbitrarily
        f"{prefix}if.name": ifnames[0] if len(ifnames) == 1 else None,
    }


def build_neighbors(cables: list[dict], devices: dict, ident: dict, host_roles: set[str]) -> NeighborResult:
    """Cables -> netbox.neighbor dimension sets, two per cable (one per direction, D5).

    devices: {netbox id: device dict}; ident: {netbox id: chassis mac or None}.
    """
    out = NeighborResult()
    for cable in cables:
        status = ((cable.get("status") or {}).get("value")) or "connected"
        if status != "connected":
            # D6: planned / decommissioning cables are intent, not topology
            out.skip(f"cable_{status}")
            continue
        a_ids, a_pow, a_if = _side(cable.get("a_terminations"))
        b_ids, b_pow, b_if = _side(cable.get("b_terminations"))
        if a_pow or b_pow:
            # D5: the network model has no power relationship; a PDU is not a network neighbour
            out.skip("power")
            continue
        if len(a_ids) != 1 or len(b_ids) != 1 or a_ids == b_ids:
            out.skip("not_two_devices")
            continue
        a, b = devices.get(next(iter(a_ids))), devices.get(next(iter(b_ids)))
        if not a or not b:
            out.skip("unknown_device")
            continue
        if not (is_active(a) and is_active(b)):
            out.skip("inactive_device")
            continue
        if not (is_network_device(a, host_roles) and is_network_device(b, host_roles)):
            out.skip("host_end")  # D4: the network model has no edge to HOST
            continue
        base = {"netbox.cable.id": cable.get("id"), "netbox.cable.type": cable.get("type")}
        ea, eb = ident.get(a.get("id")), ident.get(b.get("id"))
        for (dev, mac, ifs), (peer, pmac, pifs) in (((a, ea, a_if), (b, eb, b_if)),
                                                    ((b, eb, b_if), (a, ea, a_if))):
            out.neighbors.append(_clean({**_end(dev, mac, ifs, ""), **_end(peer, pmac, pifs, "neighbor."), **base}))
    return out
