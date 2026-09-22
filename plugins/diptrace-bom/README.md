# DipTrace BOM for InvenTree

This plugin provides a guarded workflow for turning a DipTrace CSV/XLSX export into an InvenTree bill of materials:

1. Select an existing InvenTree assembly / finished-product part.
2. Upload a DipTrace BOM containing `Designator`, `Footprint`, `Comment`, `Quantity`, and `JLCPCB Part #`.
3. Preview normalized, grouped lines and exact identifier matches.
4. Compare local available stock with JLCPCB public stock and the configured account's private component-library buckets.
5. Manually resolve ambiguous/unmatched rows (or explicitly create them when enabled).
6. Merge into or replace the assembly BOM in one database transaction.

## Separate JLC part catalogue importer

The plugin also provides **JLC Part Catalogue**, a separate page at
`/plugin/diptrace-bom/catalogue/`. The original BOM importer page is unchanged.
Upload the same CSV/XLSX format to preview manufacturer and supplier records.
The `Footprint` column is treated as the expected manufacturer part number for
this workflow, not as a PCB land-pattern value.

The catalogue importer reads the public JLCPCB part-detail pages. Exact matches
between each page's `MFR.Part #` and the CSV `Footprint` value (ignoring only
case and incidental whitespace) are ready automatically. Mismatches require
the operator to select or create the manufacturer for the spreadsheet MPN and
explicitly confirm that it is fully interchangeable with the JLCPCB MPN.
The importer then adds both Manufacturer Part records to one internal Part;
the JLCPCB C-code Supplier Part links to the JLCPCB Manufacturer Part. It does
not infer the spreadsheet manufacturer's name from JLCPCB. Missing pages,
duplicate identifiers, and conflicting existing InvenTree links remain blocked.
The preview is read-only. Applying re-fetches and rechecks the source and local
records inside a database transaction, skips blocked or unapproved rows, and reuses existing
Company, Part, Manufacturer Part, Supplier Part and Package-parameter records.
The JLCPCB C-code is stored as the Supplier Part SKU; the manufacturer number
is stored as the Manufacturer Part MPN. The part-page description is used for
new records and fills an empty existing Part description without replacing a
non-empty one. The page's package value becomes a Part `Package` parameter.

Each new Part requires a manually selected non-structural category or a newly
entered category name, with an optional parent. New categories are created
only when the user applies the reviewed import. The apply action requires an
InvenTree superuser; preview requires staff access. No BOM lines or stock are
changed by this catalogue workflow.

If the configured JLCPCB Open API is available and provides manufacturer-specific
description or website fields, the importer adds them to a new manufacturer or
fills blank fields on an existing manufacturer. It never uses a component's
description as a company description and never overwrites populated company
fields. These fields remain blank when JLCPCB does not provide them.

## JLCPCB stock synchronization

The plugin can mirror the three JLCPCB-owned inventory buckets into InvenTree
external stock locations every 30 minutes:

- `consignedParts` → Consigned Parts
- `jlcpcbParts` → JLCPCB Private Parts
- `globalSourcingParts` → Global Sourcing Reserved

Only existing `SupplierPart` records belonging to the configured JLC/LCSC
supplier are eligible, and the supplier SKU must exactly match the JLCPCB C-code.
The sync creates a managed `StockItem` when a matched quantity first becomes
positive, then updates it only when the quantity changes. It does not create
master Parts or Supplier Parts and never modifies ordinary local stock.

After a complete successful API snapshot, a managed quantity which disappeared
or became zero is set to zero. If the API request fails or the complete snapshot
cannot be confirmed, the database is left unchanged.

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
- `JLCPCB Consigned Parts Location`
- `JLCPCB Private Parts Location`
- `JLCPCB Global Sourcing Location`
- `Enable JLCPCB Stock Sync`
- optionally enable `Allow Missing Part Creation` and choose a `Default Component Category`

Create the three locations as non-structural children under a `JLCPCB` parent and
mark each child location as **External**. Each bucket must use a different
location. External stock participates in InvenTree's normal available-stock and
can-build calculations, while remaining visibly separate from company storage.

Also enable **Admin Center → Plugin Settings → Enable schedule integration** and
run an InvenTree background worker. Restart both the web server and worker after
installing or updating the plugin. Use **Sync JLCPCB stock now** for an immediate
authorized run; scheduled runs use the same reconciliation logic.

The three API values are protected settings stored by InvenTree. Never commit them to this repository.
Protected values intentionally appear as `***` in the admin interface and cannot be read back in the browser.
Use **Test JLCPCB connection** on the importer page to verify the catalogue and private-inventory permissions.

The client uses the official JLCPCB Open API endpoints:

- `/overseas/openapi/component/getComponentDetailByCode`
- `/overseas/openapi/component/getPrivateComponentLibrary`

Public catalogue results are cached for 15 minutes and importer previews cache
the private library for 5 minutes. Stock synchronization always requests a fresh,
complete private-library snapshot using JLCPCB's maximum page size of 100.

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
- Stock synchronization only owns rows whose batch marker starts with `DIPTRACE-JLC:`.
- Duplicate supplier SKUs are treated as ambiguous and are not changed.
- Only a complete successful private-library response can zero managed stock.
- Credentials never leave the server-side plugin code.

## Tests

From this directory:

```text
python -m unittest discover -s tests -v
```
