# DipTrace BOM for InvenTree

This plugin provides a guarded workflow for turning a DipTrace CSV/XLSX export into an InvenTree bill of materials:

1. Select an existing InvenTree assembly / finished-product part.
2. Upload a DipTrace BOM containing `Designator`, `Footprint`, `Comment`, `Quantity`, and `JLCPCB Part #`.
3. Preview normalized, grouped lines and exact identifier matches.
4. Compare local available stock with JLCPCB public stock and the configured account's private component-library buckets.
5. Manually resolve ambiguous/unmatched rows (or explicitly create them when enabled).
6. Merge into or replace the assembly BOM in one database transaction.

The private JLCPCB inventory is displayed as external availability. It is **not** added to InvenTree physical stock.

## Installation

Install this package in the same Python environment as InvenTree, restart the InvenTree server and worker, activate `DipTraceBomPlugin`, then restart once more if requested by InvenTree.

For a checked-out repository:

```text
pip install -e "/path/to/inventree-plugins/plugins/diptrace-bom"
```

For a pinned Git install, use the repository URL plus the `plugins/diptrace-bom` subdirectory supported by your deployment workflow.

The plugin requires InvenTree 1.5.2 or newer.

## Settings

Configure these under **Admin Center → Plugins → DipTrace BOM → Settings**:

- `JLCPCB App ID`
- `JLCPCB Access Key`
- `JLCPCB Secret Key` (the `secretKey` from the API key pair, not an RSA private key)
- `JLC / LCSC Supplier` (the supplier whose SKU stores the `C12345` code)
- optionally enable `Allow Missing Part Creation` and choose a `Default Component Category`

The three API values are protected settings stored by InvenTree. Never commit them to this repository.
Protected values intentionally appear as `***` in the admin interface and cannot be read back in the browser.
Use **Test JLCPCB connection** on the importer page to verify the catalogue and private-inventory permissions.

The client uses the official JLCPCB Open API endpoints:

- `/overseas/openapi/component/getComponentDetailByCode`
- `/overseas/openapi/component/getPrivateComponentLibrary`

Public catalogue results are cached for 15 minutes and the private library for 5 minutes to avoid excessive API traffic.

## Matching rules

Automatic matching is deliberately conservative and exact:

1. configured JLC/LCSC `SupplierPart.SKU` = `JLCPCB Part #`;
2. any unique supplier SKU = `JLCPCB Part #`;
3. unique part IPN/name = `JLCPCB Part #`;
4. unique manufacturer MPN = DipTrace `Comment`;
5. unique part IPN/name = DipTrace `Comment`.

Fuzzy matches are never committed automatically. Ambiguous and unmatched rows must be resolved in the preview.

## Safety behavior

- Uploaded normalized rows are held in a signed preview token that expires after one hour.
- Finalization rechecks every selected part inside one database transaction.
- Replace mode requires BOM delete permission and shows an additional confirmation.
- Creating missing parts is disabled by default and requires part/supplier-part add permissions.
- Credentials never leave the server-side plugin code.

## Tests

From this directory:

```text
python -m unittest discover -s tests -v
```
