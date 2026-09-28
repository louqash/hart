// Health page: check actions, marker trend chart, lab paste → preview → import.

let labPreview = [];
const escHtml = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function markDone(button, id) {
  const when = prompt("Done on (YYYY-MM-DD) — leave empty for today:", "");
  if (when === null) return;
  const body = when.trim() ? { done_on: when.trim() } : {};
  await act(button, () => api.post(`/api/health/checks/${id}/done`, body));
}

async function snooze(select, id) {
  if (!select.value) return;
  await act(select, () => api.post(`/api/health/checks/${id}/snooze`, { weeks: Number(select.value) }));
}

async function addCheck(form) {
  const body = {
    title: form.title.value.trim(), kind: form.kind.value, due_date: form.due_date.value || null,
    interval_days: form.interval_days.value ? Number(form.interval_days.value) : null,
    markers: [...form.markers.selectedOptions].map((o) => o.value), rationale: form.rationale.value.trim() || null,
  };
  await act(form.querySelector("button[type=submit]"), () => api.post("/api/health/checks", body));
}

function drawTrend(key) {
  const s = (window.TREND_SERIES || {})[key];
  const el = document.getElementById("trend");
  if (!s) return;
  const last = s.points[s.points.length - 1];
  const band = [];
  if (last.low != null || last.high != null) {
    band.push([{ yAxis: last.low ?? 0, itemStyle: { color: "rgba(110,184,134,0.08)" } }, { yAxis: last.high ?? last.value * 1.5 }]);
  }
  const annotations = (window.HEALTH_ANNOTATIONS || []).map((a) => [
    { xAxis: a.start_date, name: a.label, itemStyle: { color: a.kind === "illness" ? "rgba(229,96,74,0.10)" : "rgba(226,164,95,0.10)" },
      label: { color: css("--muted"), fontSize: 10, position: "insideTopLeft" } },
    { xAxis: a.end_date || new Date().toISOString().slice(0, 10) },
  ]);
  const color = (p) => (p.status ? css("--amber") : css("--green"));
  makeChart(el, {
    legend: { show: false },
    grid: { left: 48, right: 16, top: 20, bottom: 28 },
    tooltip: { trigger: "item", formatter: (p) => `${p.data.date}<br><b>${escHtml(p.data.text)} ${escHtml(s.unit || "")}</b>` +
      (p.data.low != null || p.data.high != null ? `<br>range ${p.data.low ?? "…"}–${p.data.high ?? "…"}` : "") },
    xAxis: { type: "time" },
    yAxis: { type: "value", scale: true, name: s.unit || "" },
    series: [
      { name: "_range", type: "line", data: [], markArea: { silent: true, data: band } },
      { name: "_events", type: "line", data: [], markArea: { silent: true, data: annotations } },
      { name: s.name, type: "line", color: css("--muted"), lineStyle: { width: 1, type: [4, 4] }, symbolSize: 9,
        data: s.points.map((p) => ({ ...p, value: [p.date, p.value], itemStyle: { color: color(p) } })) },
    ],
  });
}

async function parseLabs(button) {
  const text = document.getElementById("lab-text").value.trim();
  if (!text) return toast("Paste the lab report first");
  const status = document.getElementById("lab-status");
  button.disabled = true;
  status.textContent = "Reading the results… (up to a minute)";
  try {
    const out = await api.post("/api/health/labs/parse", { text });
    labPreview = out.panels;
    renderLabPreview(out);
    status.textContent = `${labPreview.reduce((n, p) => n + p.markers.length, 0)} results found — check them below`;
  } catch (e) {
    status.textContent = "";
    toast(e.message, 6000);
  } finally {
    button.disabled = false;
  }
}

function renderLabPreview(out) {
  const box = document.getElementById("lab-preview");
  box.innerHTML = labPreview.map((p, pi) => `
    <div class="eyebrow" style="margin:14px 0 6px">Test date ${escHtml(p.test_date)}${p.existing ? ` · <span class="warn">${p.existing} results already stored for this date — duplicates are skipped</span>` : ""}</div>
    <div class="table-wrap"><table class="labs">
      <tr><th></th><th>Marker</th><th class="num">Value</th><th>Unit</th><th>Range</th><th>Flag</th></tr>
      ${p.markers.map((m, mi) => `<tr>
        <td><input type="checkbox" data-p="${pi}" data-m="${mi}" checked style="width:auto"></td>
        <td class="wrap">${escHtml(m.name)}${m.marker_key ? "" : ' <span class="faint small">(not in catalogue)</span>'}</td>
        <td class="num">${escHtml(m.value)}</td><td>${escHtml(m.unit)}</td><td>${escHtml(m.reference_range)}</td><td>${escHtml(m.flag)}</td>
      </tr>`).join("")}
    </table></div>`).join("") +
    ((out.warnings || []).length ? `<ul class="hl bad">${out.warnings.map((w) => `<li>${escHtml(w)}</li>`).join("")}</ul>` : "") +
    `<div class="row" style="margin-top:10px"><button class="primary" onclick="importLabs(this)">Save results</button>
      <button class="secondary" onclick="document.getElementById('lab-preview').hidden = true">Discard</button></div>`;
  box.hidden = false;
}

async function importLabs(button) {
  const panels = labPreview.map((p, pi) => ({
    test_date: p.test_date,
    markers: p.markers.filter((_, mi) => document.querySelector(`#lab-preview input[data-p="${pi}"][data-m="${mi}"]`)?.checked)
      .map(({ name, value, unit, reference_range, flag }) => ({ name, value, unit, reference_range, flag })),
  })).filter((p) => p.markers.length);
  if (!panels.length) return toast("Nothing selected");
  const out = await act(button, () => api.post("/api/health/labs/import", { panels, source: "paste" }), { reload: false });
  if (out) {
    const added = out.panels.reduce((n, p) => n + p.added, 0);
    toast(`Saved ${added} result${added === 1 ? "" : "s"}`);
    setTimeout(() => location.reload(), 700);
  }
}

async function addManual(form) {
  let body;
  try { body = formData(form); } catch (e) { return toast(e.message); }
  const { test_date, ...marker } = body;
  await act(form.querySelector("button[type=submit]"),
    () => api.post("/api/health/labs/import", { panels: [{ test_date, markers: [marker] }], source: "manual" }));
}

document.addEventListener("DOMContentLoaded", () => {
  const select = document.getElementById("trend-marker");
  if (select && select.value) drawTrend(select.value);
});
