export function renderDashboardItem(target, data) {
  if (!target) {
    return;
  }

  const url = data?.context?.url || "/plugin/supplier-scan/";
  const openScan = (barcode = "") => {
    const value = String(barcode || "").trim();
    const destination = value
      ? `${url}?barcode=${encodeURIComponent(value)}`
      : url;
    window.location.assign(destination);
  };

  target.innerHTML = `
    <div class="supplier-scan-widget">
      <style>
        .supplier-scan-widget {
          display: grid;
          gap: 10px;
          padding: 4px 2px;
        }

        .supplier-scan-widget__title {
          margin: 0;
          font-size: 16px;
          font-weight: 650;
          color: var(--mantine-color-text, #1f2937);
        }

        .supplier-scan-widget__meta {
          margin: 0;
          font-size: 13px;
          color: var(--mantine-color-dimmed, #667085);
        }

        .supplier-scan-widget__row {
          display: grid;
          grid-template-columns: minmax(0, 1fr) auto;
          gap: 8px;
          align-items: center;
        }

        .supplier-scan-widget input {
          width: 100%;
          min-height: 36px;
          border: 1px solid var(--mantine-color-gray-4, #ced4da);
          border-radius: 6px;
          padding: 7px 9px;
          font: inherit;
          letter-spacing: 0;
        }

        .supplier-scan-widget button {
          min-height: 36px;
          border: 1px solid var(--mantine-primary-color-filled, #228be6);
          border-radius: 6px;
          background: var(--mantine-primary-color-filled, #228be6);
          color: var(--mantine-color-white, #fff);
          padding: 7px 12px;
          font: inherit;
          cursor: pointer;
          white-space: nowrap;
        }

        .supplier-scan-widget__link {
          border-color: var(--mantine-color-gray-4, #ced4da) !important;
          background: var(--mantine-color-body, #fff) !important;
          color: var(--mantine-primary-color-filled, #228be6) !important;
        }

        @media (max-width: 640px) {
          .supplier-scan-widget__row {
            grid-template-columns: 1fr;
          }

          .supplier-scan-widget button {
            width: 100%;
          }
        }
      </style>
      <h4 class="supplier-scan-widget__title">Supplier Scan</h4>
      <p class="supplier-scan-widget__meta">LCSC, JLC and DigiKey receiving</p>
      <div class="supplier-scan-widget__row">
        <input type="text" autocomplete="off" placeholder="Barcode">
        <button type="button">Open</button>
      </div>
      <button class="supplier-scan-widget__link" type="button">Open scanner</button>
    </div>
  `;

  const input = target.querySelector("input");
  const [openButton, linkButton] = target.querySelectorAll("button");

  input?.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      openScan(input.value);
    }
  });

  openButton?.addEventListener("click", () => openScan(input?.value));
  linkButton?.addEventListener("click", () => openScan());
}
