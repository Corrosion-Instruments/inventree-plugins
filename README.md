# Corrosion Instruments InvenTree plugins

This private repository holds Corrosion Instruments' custom InvenTree plugins. Each plugin has its own directory and Python package, so new plugins can be added without creating another repository.

| Plugin | Package | Purpose |
| --- | --- | --- |
| Supplier Scan | [`plugins/supplier-scan`](plugins/supplier-scan) | Scan supplier barcodes and receive stock |

The repository contains plugin **source code**, not InvenTree database records, plugin activation/settings, uploaded media, or secrets. Those must be configured or backed up separately for each InvenTree instance.

Do not put credentials in this repository or in `plugins.txt`. When installing from this private GitHub repository, configure read-only Git access on the server and pin the plugin to a tested commit or release.
