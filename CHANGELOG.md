# Changelog

What changed in each release and what you need to do about it. Versions follow `version` in
`extension/extension.yaml`.

## 0.1.0 — unreleased

First version. Enriches Dynatrace `EXT_NETWORK_DEVICE` nodes with NetBox data and draws NetBox
cabling as neighbor `calls` edges, through a bundled OpenPipeline pipeline. Not yet validated on a
tenant — see "Open questions" in the README before relying on it.

**Action:** run `tools/reconcile_chassis_mac.py` first. A device with no chassis MAC in NetBox
cannot be matched to its Smartscape node.
