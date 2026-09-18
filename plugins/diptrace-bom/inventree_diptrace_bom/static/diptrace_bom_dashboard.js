export function renderDashboardItem(target, data) {
  if (!target) return;
  const url = data?.context?.url || "/plugin/diptrace-bom/";
  target.innerHTML = `
    <div class="diptrace-bom-widget">
      <style>
        .diptrace-bom-widget { display:grid; gap:12px; padding:4px 2px; }
        .diptrace-bom-widget h4 { margin:0; font-size:16px; font-weight:650; }
        .diptrace-bom-widget p { margin:0; color:var(--mantine-color-dimmed,#667085); font-size:13px; }
        .diptrace-bom-widget button { min-height:38px; border:0; border-radius:6px; padding:8px 12px;
          color:#fff; background:var(--mantine-primary-color-filled,#228be6); cursor:pointer; font:inherit; }
      </style>
      <h4>DipTrace BOM</h4>
      <p>Normalize a PCB BOM, compare local and JLCPCB stock, then finalize it.</p>
      <button type="button">Open BOM importer</button>
    </div>`;
  target.querySelector("button")?.addEventListener("click", () => window.location.assign(url));
}
