#!/usr/bin/env python3
"""
Fill NetBox's `chassis_mac` device custom field from what Dynatrace already observed.

What it is for: the custom:netbox.network extension can only enrich a Smartscape EXT_NETWORK_DEVICE if it knows that device's chassis MAC, and NetBox rarely
holds one (measured on a real NetBox 4.6 install 2026-11-10: 0 of 15 interfaces carry a MAC). SNMP
extensions do know it. This joins the two on IP address: NetBox primary IP == one of the node's ip[].

What it must not do:
  - write anything without --apply. Default is a dry run that prints the plan.
  - guess. An IP that matches more than one node (IP reuse, or a Neighbor duplicate) is reported
    and left alone; so is a device whose field already holds a different MAC.

Usage:
    python3 tools/reconcile_chassis_mac.py --context <dtctl-ctx>            # dry run
    python3 tools/reconcile_chassis_mac.py --context <dtctl-ctx> --apply    # needs a write token
Env: NETBOX_URL, NETBOX_TOKEN. Needs dtctl authenticated against the tenant.
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from netbox_network.model import normalize_mac, primary_address  # noqa: E402
from netbox_network.netbox import NetboxClient  # noqa: E402

DQL = ("smartscapeNodes EXT_NETWORK_DEVICE | fieldsAdd chassis_mac, ip, monitoring_mode "
       "| filter isNotNull(chassis_mac) | fields name, chassis_mac, ip, monitoring_mode")


def smartscape_devices(context: str) -> list[dict]:
    # Context is mandatory: a bare dtctl query uses whichever context is active, and against the
    # wrong tenant it returns empty with exit 0 — which here would read as "nothing to reconcile".
    p = subprocess.run(["dtctl", "query", "--context", context, DQL, "-o", "json"],
                       capture_output=True, text=True)
    if p.returncode != 0:
        sys.exit(f"dtctl failed: {(p.stderr or p.stdout).strip()[:300]}")
    out = json.loads(p.stdout)
    recs = out["result"]["records"] if isinstance(out, dict) else out
    if not recs:
        sys.exit(f"no EXT_NETWORK_DEVICE nodes with a chassis_mac on context {context} — wrong tenant?")
    return recs


def ensure_field(nb_url, token, name):
    h = {"Authorization": f"Token {token}", "Content-Type": "application/json", "Accept": "application/json"}

    def call(method, path, body=None):
        req = urllib.request.Request(f"{nb_url}/api{path}", method=method, headers=h,
                                     data=json.dumps(body).encode() if body else None)
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r) if r.status != 204 else {}

    if call("GET", f"/extras/custom-fields/?name={name}").get("count"):
        return call
    payload = {"name": name, "label": "Chassis MAC", "type": "text",
               "description": "First burned-in MAC of the device; Dynatrace network-model identity"}
    try:
        call("POST", "/extras/custom-fields/", {**payload, "object_types": ["dcim.device"]})
    except urllib.error.HTTPError:
        call("POST", "/extras/custom-fields/", {**payload, "content_types": ["dcim.device"]})  # NetBox 3.x
    print(f"created custom field {name}")
    return call


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--context", required=True, help="dtctl context of the tenant to read")
    ap.add_argument("--field", default="chassis_mac")
    ap.add_argument("--apply", action="store_true", help="write to NetBox (default: dry run)")
    a = ap.parse_args()
    nb_url, token = os.environ["NETBOX_URL"].rstrip("/"), os.environ["NETBOX_TOKEN"]

    by_ip: dict[str, list[dict]] = {}
    for n in smartscape_devices(a.context):
        for ip in n.get("ip") or []:
            by_ip.setdefault(ip, []).append(n)

    plan, report = [], {"match": 0, "already_set": 0, "conflict": 0, "ambiguous": 0, "no_node": 0, "no_ip": 0}
    for d in NetboxClient(nb_url, token).all("/dcim/devices/"):
        ip = primary_address(d)
        if not ip:
            report["no_ip"] += 1
            continue
        nodes = by_ip.get(ip, [])
        macs = {normalize_mac(n["chassis_mac"]) for n in nodes} - {None}
        if not macs:
            report["no_node"] += 1
            continue
        if len(macs) > 1:
            report["ambiguous"] += 1
            print(f"AMBIGUOUS {d['name']} {ip}: nodes {[n['name'] for n in nodes]} — left alone")
            continue
        mac = macs.pop()
        have = normalize_mac((d.get("custom_fields") or {}).get(a.field))
        if have == mac:
            report["already_set"] += 1
        elif have:
            report["conflict"] += 1
            print(f"CONFLICT {d['name']}: NetBox {have} vs Smartscape {mac} — left alone")
        else:
            report["match"] += 1
            plan.append((d["id"], d["name"], mac))
            print(f"SET {d['name']} {ip} -> {mac}")

    print(json.dumps(report))
    if not a.apply:
        print(f"dry run: {len(plan)} device(s) would be updated. Re-run with --apply to write.")
        return
    call = ensure_field(nb_url, token, a.field)
    for did, name, mac in plan:
        call("PATCH", f"/dcim/devices/{did}/", {"custom_fields": {a.field: mac}})
    print(f"updated {len(plan)} device(s)")


if __name__ == "__main__":
    main()
