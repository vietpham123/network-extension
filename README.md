# NetBox network extension — `custom:netbox.network`

**Status: experimental. Not yet validated on a tenant (see Open questions).**

> An independent open-source project, **not an official Dynatrace product** and not covered by any
> Dynatrace support agreement. Apache 2.0, provided as-is.

A standalone Dynatrace extension that brings NetBox into the Dynatrace **network model** from the
Semantic Dictionary. It needs no particular app. Anything that reads the network model sees NetBox's
data: Infrastructure & Operations → Network devices, Smartscape, DQL, notebooks, and any app built
on `EXT_NETWORK_DEVICE`.

Model references:
[Smartscape – Extensions – Networking](https://docs.dynatrace.com/docs/semantic-dictionary/model/smartscape/ext-network) ·
[Network topology how-to](https://docs.dynatrace.com/docs/ingest-from/extensions/develop-your-extensions/how-to-guides/network-topology) ·
[Network metrics how-to](https://docs.dynatrace.com/docs/ingest-from/extensions/develop-your-extensions/how-to-guides/network-metrics)

## What you get

NetBox is a system of record and does not poll devices, so this extension does not create its own
set of devices. It **enriches the `EXT_NETWORK_DEVICE` nodes that Dynatrace's SNMP extensions and
SNMP Autodiscovery already create**, and adds the cabling NetBox declares as neighbour edges:

- **On each device node:** `netbox.site`, `netbox.rack`, `netbox.role`, `netbox.tenant`,
  `netbox.platform`, `netbox.manufacturer`, `netbox.device_type`, `netbox.asset_tag`,
  `netbox.status`, `netbox.id`, plus the model fields `serial_number` and `part_number`.
- **Neighbour edges:** each connected data cable becomes a `calls` edge in both directions. Dynatrace's
  own LLDP/CDP neighbour discovery draws its edges the same way.

```
smartscapeNodes EXT_NETWORK_DEVICE
| fieldsAdd netbox.site, netbox.rack, netbox.role, serial_number
| filter isNotNull(netbox.id)
```

### Signals

The pipeline needs a signal to carry NetBox's data, and the Semantic Dictionary has no standard
metric for "a CMDB record". The extension therefore emits these, under the technology namespace
`netbox.*`, the same pattern the Dictionary uses for `aws.*` or `snmp.*`:

| Metric | Value | Used for |
|---|---|---|
| `netbox.device` | 1 per NetBox device | Enriching the node, when the device is active, a network device, and has a chassis MAC |
| `netbox.neighbor` | 1 per direction per cable | The `calls` edge |
| `netbox.cable.skipped` | count, by `reason` | Showing why a cable has no edge: `power`, `cable_planned`, `host_end`, `inactive_device`, … |
| `netbox.poll` | 1 or 0, by `netbox.endpoint` | A 0 means NetBox could not be read. It does not mean NetBox is empty |

Dimension names come from the network metrics model wherever it defines one: `sys.name`,
`device.address`, `chassis.mac`, `if.name`.

## Identity: matching NetBox to what Dynatrace already sees

The network model identifies a device by its **chassis MAC**, and NetBox seldom stores one. On a real
NetBox 4.6 install, none of the 15 interfaces had a MAC. The bundled tool copies the MAC from
Smartscape into a NetBox custom field, matching devices by IP:

```bash
export NETBOX_URL=https://netbox.example.com NETBOX_TOKEN=...      # write token only for --apply
python3 tools/reconcile_chassis_mac.py --context <dtctl-context>            # dry run, prints the plan
python3 tools/reconcile_chassis_mac.py --context <dtctl-context> --apply    # writes custom field chassis_mac
```

Any IP that matches more than one node, and any device whose field already holds a different MAC,
is reported and left unchanged.

Every device reports `identity.source` (`custom_field`, `mgmt_interface` or `none`). Devices with
`none` stay in the roster but do not reach Smartscape:

```
timeseries n=sum(netbox.device), by:{identity.source, netbox.is_network_device}
```

## Configuration (ActiveGate monitoring configuration)

| Field | Default | |
|---|---|---|
| NetBox URL / API token | — | Read access to dcim devices, interfaces and cables |
| Chassis MAC custom field | `chassis_mac` | Where the device's identity is read from |
| Roles that are hosts | `server,hypervisor,storage` | Listed in the roster, but never network devices and never edge endpoints |
| Draw neighbor edges | on | Turn off to get device metadata only |
| Poll interval | 300 s | NetBox changes on human timescales |

## Design decisions

Recorded with the alternatives rejected in [`docs/DECISIONS.md`](docs/DECISIONS.md). Code comments cite them as `D1`..`D7`.

## Open questions (verify on a tenant before relying on this)

- `[OPEN]` **Is `openpipeline:` accepted in a Python extension manifest?** The docs show it on SNMP
  (declarative) extensions only.
- `[OPEN]` **Can a node processor with `extractNode: true` and an empty `fieldsToExtract` add an
  edge** without clearing fields that SNMP wrote? `netbox-neighbor-calls-edge` relies on this.
- `[OPEN]` **Name flip-flop.** The node name is set from NetBox's name. If that differs from SNMP's
  `sysName`, the node name alternates between them.
- `[OPEN]` **IP flip-flop.** If NetBox's primary IP is not the address SNMP polls, `ip` alternates
  between the two.
- `[OPEN]` **Node retention.** The lifetime of a Smartscape node that stops being upserted is not
  documented.
- `[OPEN]` **The positive match path of the reconcile tool has not been run.** It has so far been run
  only against a NetBox and a tenant that share no devices.
- **Not modelled yet: interfaces.** `EXT_NETWORK_INTERFACE` identity is the interface name plus the
  chassis MAC. SNMP writes short names such as `Te1/1/3`, while NetBox usually stores long names such
  as `TenGigabitEthernet1/1/3`. Emitting interfaces would create duplicate nodes until the names are
  normalised. `if.name` is already carried on `netbox.neighbor` for when they are.

## Build, test, deploy

```bash
python3 -m pytest tests                  # pure mapping + NetBox client; no SDK needed
dt-sdk build -k <your developer cert>    # sign with your own certificate
```

Upload the certificate to **Settings → Extensions → trusted certificates**, upload the zip, and add a
monitoring configuration on an ActiveGate group. Then check **Settings → OpenPipeline → Metrics** for
the "NetBox network" ingest source and pipeline.
