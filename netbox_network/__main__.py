"""
NetBox -> Dynatrace network model. Remote (ActiveGate) Python extension.

What it is for: landing NetBox's device roster and declared cabling as signals shaped for the
Smartscape network model, so the bundled OpenPipeline pipeline (extension/openpipeline/) can enrich
EXT_NETWORK_DEVICE nodes and draw neighbour `calls` edges. Standalone: it depends on no app, and
anything that reads the Dynatrace network model (Infrastructure & Operations, Smartscape, DQL,
other apps) sees the result. This file only polls and reports; the pipeline does Smartscape.

What it must not do: report a partial roster as if complete (D7). A failed endpoint emits a 0 on
netbox.poll and nothing else for that endpoint this cycle.

Emits:
  netbox.device          = 1   one per NetBox device (state metric; dimensions carry the facts)
  netbox.neighbor        = 1   two per connected data cable between active network devices
  netbox.cable.skipped   = n   cables not turned into neighbours, by reason
  netbox.poll            = 1|0 per NetBox endpoint: did we manage to read it completely
"""
from datetime import timedelta

from dynatrace_extension import Extension

from .model import build_neighbors, chassis_mac, device_dimensions, is_network_device
from .netbox import NetboxClient, NetboxError


class NetboxNetworkExtension(Extension):

    def initialize(self):
        try:
            interval = int(self._cfg().get("intervalSeconds", 300) or 300)
        except Exception:
            interval = 300
        self.schedule(self.query, timedelta(seconds=max(60, interval)))

    def _cfg(self) -> dict:
        """Activation config as a flat dict. MUST go through .config: ActivationConfig subclasses
        dict but holds no data itself, so dict(ac) is empty — proven on a real ActiveGate
        2026-08-01. The fallbacks cover SDK variants."""
        ac = self.get_activation_config()
        cfg = getattr(ac, "config", None)
        if isinstance(cfg, dict) and cfg:
            return dict(cfg)
        raw = dict(ac) if hasattr(ac, "keys") else dict(getattr(ac, "__dict__", {}))
        if "netboxUrl" not in raw:
            raw = next((v for v in raw.values() if isinstance(v, dict) and "netboxUrl" in v), raw)
        return raw

    def _poll(self, endpoint: str, ok: bool):
        self.report_metric("netbox.poll", 1 if ok else 0, dimensions={"netbox.endpoint": endpoint})

    def query(self):
        c = self._cfg()
        url, token = str(c.get("netboxUrl", "")).strip(), c.get("netboxToken", "")
        if not (url and token):
            self.logger.error("netboxUrl / netboxToken not configured")
            return
        mac_field = str(c.get("chassisMacField", "chassis_mac") or "").strip()
        host_roles = {r.strip() for r in str(c.get("hostRoles", "")).split(",") if r.strip()}
        emit_neighbors = bool(c.get("emitNeighbors", True))
        nb = NetboxClient(url, token)

        try:
            devices = nb.all("/dcim/devices/")
            # one call for every management interface, not one per device; only needed for the
            # D2 fallback identity
            mgmt = nb.all("/dcim/interfaces/", mgmt_only="true")
        except NetboxError as e:
            self.logger.error(f"device inventory not reported this cycle: {e}")
            self._poll("devices", False)
            return  # neighbours need the device map; half-resolved edges would be worse
        self._poll("devices", True)

        mgmt_by_dev: dict = {}
        for i in mgmt:
            did = (i.get("device") or {}).get("id")
            if did is not None:
                mgmt_by_dev.setdefault(did, []).append(i)

        by_id, ident, sources = {}, {}, {}
        for d in devices:
            mac, src = chassis_mac(d, mgmt_by_dev.get(d.get("id"), []), mac_field)
            by_id[d.get("id")], ident[d.get("id")] = d, mac
            if is_network_device(d, host_roles):
                sources[src] = sources.get(src, 0) + 1
            self.report_metric("netbox.device", 1, dimensions=device_dimensions(d, mac, src, host_roles))
        self.logger.info(f"devices: {len(devices)} reported; network-device identity by source {sources}")

        if not emit_neighbors:
            return
        try:
            cables = nb.all("/dcim/cables/")
        except NetboxError as e:
            self.logger.error(f"cabling not reported this cycle: {e}")
            self._poll("cables", False)
            return
        self._poll("cables", True)

        res = build_neighbors(cables, by_id, ident, host_roles)
        for dims in res.neighbors:
            self.report_metric("netbox.neighbor", 1, dimensions=dims)
        for reason, n in res.skipped.items():
            self.report_metric("netbox.cable.skipped", n, dimensions={"reason": reason})
        self.logger.info(f"neighbors: {len(res.neighbors)} reported, cables skipped {res.skipped}")


def main():
    NetboxNetworkExtension().run()


if __name__ == "__main__":
    main()
