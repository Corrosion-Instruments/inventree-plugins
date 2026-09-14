# Supplier Scan

Corrosion Instruments' custom InvenTree plugin for scanning LCSC/JLC and DigiKey supplier labels into stock.

The Python package exposes `SupplierScanPlugin` through InvenTree's `inventree_plugins` entry-point group and includes its dashboard JavaScript and page template.

The plugin currently runs from a local Docker volume in production. This packaged copy is a source backup and installation candidate; it has not replaced the running production or staging copy. Test the package on staging before using it in production. Plugin activation, integration toggles, and supplier settings are stored in the InvenTree database and are not included here.
