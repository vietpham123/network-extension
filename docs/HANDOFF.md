# Handoff

**Status (2026-11-10):** 0.1.0 written and unit-tested (18 tests). Driven against a real NetBox 4.6
(devices read, failed cables endpoint reported as `netbox.poll=0`). Never signed or deployed, so the
OpenPipeline behaviour is unverified.

**Exact next action:** run `tools/reconcile_chassis_mac.py --context <ctx>` (dry run) against a
NetBox that describes devices already in Smartscape, then sign, deploy to a test tenant, and work
through every `[OPEN]` item in the README with:

    smartscapeNodes EXT_NETWORK_DEVICE | filter isNotNull(netbox.id)
    smartscapeEdges calls | filter source_type == "EXT_NETWORK_DEVICE"
