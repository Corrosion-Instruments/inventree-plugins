export function renderDashboardItem(target, data) {
  if (!target) return;

  const importerUrl = data?.context?.importer_url || "/plugin/diptrace-bom/";
  const plannerUrl = data?.context?.planner_url || "/plugin/diptrace-bom/planner/";
  const catalogueUrl = data?.context?.catalogue_url || "";
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
        .diptrace-bom-widget__links { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; }
        .diptrace-bom-widget__link { min-height:38px; display:flex; align-items:center; justify-content:center;
          border:1px solid var(--mantine-color-gray-4,#ced4da); border-radius:6px; padding:8px 10px;
          background:var(--mantine-color-body,#fff); color:var(--mantine-primary-color-filled,#228be6);
          font:inherit; font-weight:600; text-decoration:none; text-align:center; }
        .diptrace-bom-widget__link:hover { background:var(--mantine-color-gray-0,#f8f9fa); }
        .diptrace-bom-widget__status { min-height:16px; color:var(--mantine-color-red-7,#c92a2a); font-size:12px; }
        @media (max-width:640px) {
          .diptrace-bom-widget__row { grid-template-columns:1fr; }
          .diptrace-bom-widget__links { grid-template-columns:1fr; }
          .diptrace-bom-widget button { width:100%; }
        }
      </style>
      <h4>DipTrace BOM</h4>
      <p>Normalize a PCB BOM, compare local and JLCPCB stock, then finalize it.</p>
      <div class="diptrace-bom-widget__row">
        <input type="file" accept=".csv,.txt,.xlsx,.xlsm" aria-label="DipTrace BOM file">
        <button class="diptrace-bom-widget__open" type="button">Open</button>
      </div>
      <nav class="diptrace-bom-widget__links" aria-label="DipTrace BOM tools">
        <a class="diptrace-bom-widget__link" data-tool="importer">BOM Importer</a>
        <a class="diptrace-bom-widget__link" data-tool="planner">Build Planner</a>
        ${catalogueUrl ? '<a class="diptrace-bom-widget__link" data-tool="catalogue">JLC Part Catalogue</a>' : ''}
      </nav>
      <span class="diptrace-bom-widget__status" role="status" aria-live="polite"></span>
    </div>`;

  const input = target.querySelector("input");
  const openButton = target.querySelector(".diptrace-bom-widget__open");
  const status = target.querySelector(".diptrace-bom-widget__status");
  target.querySelector('[data-tool="importer"]')?.setAttribute("href", importerUrl);
  target.querySelector('[data-tool="planner"]')?.setAttribute("href", plannerUrl);
  target.querySelector('[data-tool="catalogue"]')?.setAttribute("href", catalogueUrl);

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
      window.location.assign(`${importerUrl}?dashboard_upload=1`);
    } catch (error) {
      status.textContent = error?.message || "Could not open the selected BOM file.";
      openButton.disabled = false;
    }
  });
}
