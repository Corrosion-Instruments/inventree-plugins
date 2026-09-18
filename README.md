# Corrosion Instruments InvenTree plugins

This repository holds Corrosion Instruments' custom InvenTree plugins. Each plugin has its own directory and Python package, so new plugins can be added without creating another repository.

| Plugin | Package | Purpose |
| --- | --- | --- |
| Supplier Scan | [`plugins/supplier-scan`](plugins/supplier-scan) | Scan supplier barcodes and receive stock |
| DipTrace BOM | [`plugins/diptrace-bom`](plugins/diptrace-bom) | Normalize DipTrace BOMs, synchronize JLCPCB-owned stock, and finalize BOMs |

The repository contains plugin **source code**, not InvenTree database records, plugin activation/settings, uploaded media, or secrets. Those must be configured or backed up separately for each InvenTree instance.

Do not put credentials in this repository or in `plugins.txt`. Install from the public HTTPS repository and pin production to a tested commit or release.
