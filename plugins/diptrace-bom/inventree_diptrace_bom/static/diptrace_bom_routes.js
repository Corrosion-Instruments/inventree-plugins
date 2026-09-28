function redirectPage(target, label) {
  window.setTimeout(() => {
    window.location.replace(target);
  }, 0);

  return React.createElement(
    "main",
    {
      style: {
        display: "grid",
        justifyItems: "center",
        gap: 12,
        padding: 32,
      },
      "aria-live": "polite",
    },
    React.createElement("p", null, `Opening ${label}…`),
    React.createElement(
      "a",
      { href: target },
      `Open ${label}`,
    ),
  );
}

export function redirectBomImporter() {
  return redirectPage("/plugin/diptrace-bom/", "BOM Importer");
}

export function redirectPartCatalogue() {
  return redirectPage("/plugin/diptrace-bom/catalogue/", "Part Catalogue");
}

export function redirectBuildPlanner() {
  return redirectPage("/plugin/diptrace-bom/planner/", "Build Planner");
}
