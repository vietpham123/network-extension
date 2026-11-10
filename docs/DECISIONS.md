# Decision register

Append-only. A decision is superseded by a new entry, never edited. Each entry records the
alternatives rejected, so it is not relitigated.

Code cites these as `D1`..`D7`.

- **D1: Dynatrace model only.** The output is standard `EXT_NETWORK_DEVICE` fields and `calls`
  edges. Data that only NetBox has goes under `netbox.*`. The extension depends on no app.
  *Rejected:* emitting `com.dynatrace.extension.network_device.*`. That prefix belongs to Dynatrace,
  and every metric under it is a measurement such as uptime, CPU or traffic, which NetBox does not have.
- **D2: never invent an identifier.** The order is: custom field, then the MAC of the management
  interface, then nothing. The docs allow a non-MAC identifier, but SNMP Autodiscovery then creates a
  duplicate node, and Smartscape nodes cannot be merged. *Rejected:* `netbox-<id>` as the identifier.
- **D3: write only fields that SNMP leaves empty.** Each write to a node replaces the field's value.
  On a test tenant, the Cisco SNMP extension writes `location`, `contact`, `vendor`, `description`,
  `fqdn` and `device_type`, and leaves `serial_number` and `part_number` empty. If NetBox wrote the
  first set, the values would alternate between NetBox and SNMP every poll. The site and rack
  therefore go to `netbox.site` / `netbox.rack`, not `location`. **`ip` and `mac` are the exception.**
  NetBox does write them, so that devices only NetBox knows about still get syslog and trap
  correlation.
- **D4: hosts are not network devices.** The network model defines no edge to `HOST`, so cables to
  hosts are counted as `host_end` and draw no edge.
- **D5: a cable is a neighbour relationship, not a dependency.** It is emitted in both directions, the
  way Dynatrace's own LLDP discovery draws edges. On a test tenant, 26 of the 37 device edges had a
  reverse edge. Power cables are skipped, because a PDU is not a network neighbour and the model has
  no power relationship.
- **D6: only connected cables.** Planned and decommissioning cables are intent, not topology.
- **D7: no partial results.** If any page fails, that endpoint emits `netbox.poll=0` and nothing else
  for the cycle. A partial roster looks the same as removed devices. NetBox's `next` links are
  rebased onto the configured URL, because NetBox often advertises an internal hostname that the
  ActiveGate cannot reach.
