export function renderDashboardItem(target, data) {
  if (!target) return;

  const url = data?.context?.url || "/plugin/diptrace-bom/";
  const databaseName = "inventree-diptrace-bom";
  const storeName = "dashboard-uploads";
  const uploadKey = "pending";

  const stageFile = (file) => new Promise((resolve, reject) => {
    if (!window.indexedDB) {
      reject(new Error("This browser cannot transfer the selected file to the importer"));
      return;
    }

    const request = window.indexedDB.open(databaseName, 1);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(storeName)) {
        request.result.createObjectStore(storeName);
      }
    };
    request.onerror = () => reject(request.error || new Error("Could not stage the BOM file"));
    request.onsuccess = () => {
      const database = request.result;
      const transaction = database.transaction(storeName, "readwrite");
      transaction.objectStore(storeName).put({
        blob: file,
        name: file.name,
        type: file.type,
        lastModified: file.lastModified,
        savedAt: Date.now(),
      }, uploadKey);
      transaction.oncomplete = () => {
        database.close();
        resolve();
      };
      transaction.onerror = () => {
        database.close();
        reject(transaction.error || new Error("Could not stage the BOM file"));
      };
    };
  });

  target.innerHTML = `
    <div class="diptrace-bom-widget">
      <style>
        .diptrace-bom-widget { display:grid; gap:10px; padding:4px 2px; }
        .diptrace-bom-widget h4 { margin:0; font-size:16px; font-weight:650; }
        .diptrace-bom-widget p { margin:0; color:var(--mantine-color-dimmed,#667085); font-size:13px; }
        .diptrace-bom-widget__row { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:8px; align-items:center; }
        .diptrace-bom-widget input { width:100%; min-width:0; min-height:38px; border:1px solid var(--mantine-color-gray-4,#ced4da);
          border-radius:6px; padding:6px 8px; background:var(--mantine-color-body,#fff); color:var(--mantine-color-text,#1f2937); font:inherit; }
        .diptrace-bom-widget button { min-height:38px; border:0; border-radius:6px; padding:8px 12px;
          color:#fff; background:var(--mantine-primary-color-filled,#228be6); cursor:pointer; font:inherit; white-space:nowrap; }
        .diptrace-bom-widget button:disabled { opacity:.55; cursor:not-allowed; }
        .diptrace-bom-widget__link { width:100%; border:1px solid var(--mantine-color-gray-4,#ced4da) !important;
          background:var(--mantine-color-body,#fff) !important; color:var(--mantine-primary-color-filled,#228be6) !important; }
        .diptrace-bom-widget__status { min-height:16px; color:var(--mantine-color-red-7,#c92a2a); font-size:12px; }
        @media (max-width:640px) {
          .diptrace-bom-widget__row { grid-template-columns:1fr; }
          .diptrace-bom-widget button { width:100%; }
        }
      </style>
      <h4>DipTrace BOM</h4>
      <p>Normalize a PCB BOM, compare local and JLCPCB stock, then finalize it.</p>
      <div class="diptrace-bom-widget__row">
        <input type="file" accept=".csv,.txt,.xlsx,.xlsm" aria-label="DipTrace BOM file">
        <button class="diptrace-bom-widget__open" type="button">Open</button>
      </div>
      <button class="diptrace-bom-widget__link" type="button">Open BOM importer</button>
      <span class="diptrace-bom-widget__status" role="status" aria-live="polite"></span>
    </div>`;

  const input = target.querySelector("input");
  const openButton = target.querySelector(".diptrace-bom-widget__open");
  const linkButton = target.querySelector(".diptrace-bom-widget__link");
  const status = target.querySelector(".diptrace-bom-widget__status");

  openButton?.addEventListener("click", async () => {
    const file = input?.files?.[0];
    if (!file) {
      input?.click();
      return;
    }
    if (file.size > 10 * 1024 * 1024) {
      status.textContent = "Select a BOM file smaller than 10 MB.";
      return;
    }

    openButton.disabled = true;
    status.textContent = "Opening BOM importer…";
    try {
      await stageFile(file);
      window.location.assign(`${url}?dashboard_upload=1`);
    } catch (error) {
      status.textContent = error?.message || "Could not open the selected BOM file.";
      openButton.disabled = false;
    }
  });

  linkButton?.addEventListener("click", () => window.location.assign(url));
}
