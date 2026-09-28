// Plan page: paste → preview → import, add/edit/delete sessions, link
// activities, Garmin workout text.

let preview = [];

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function parsePaste(button) {
  const text = document.getElementById("paste-text").value.trim();
  if (!text) return toast("Paste the coach's week first");
  const status = document.getElementById("parse-status");
  button.disabled = true;
  status.textContent = "Reading the plan… (up to a minute)";
  try {
    const out = await api.post("/api/plan/parse", { text, default_date: document.getElementById("paste-date").value || null });
    preview = out.sessions;
    renderPreview(out);
    status.textContent = `${preview.length} session${preview.length === 1 ? "" : "s"} found — check them below`;
  } catch (e) {
    status.textContent = "";
    toast(e.message, 6000);
  } finally {
    button.disabled = false;
  }
}

function renderPreview(out) {
  const body = document.getElementById("preview-rows");
  body.innerHTML = preview.map((s, i) => `
    <tr>
      <td><input type="checkbox" data-i="${i}" checked style="width:auto"></td>
      <td><input type="date" class="small-date" data-date="${i}" value="${esc(s.date)}"></td>
      <td><span class="sport ${esc(s.sport_type)}"></span>${esc(s.sport_type)}</td>
      <td class="wrap">${esc(s.title)}</td>
      <td class="num">${s.duration_min ?? "—"}</td>
      <td>${esc(s.intensity ?? "—")}</td>
      <td class="wrap"><div class="coach-text small">${esc(s.description)}</div></td>
    </tr>`).join("") || `<tr><td colspan="7" class="empty">No sessions found in the text.</td></tr>`;
  document.getElementById("preview-warnings").innerHTML = (out.warnings || []).map((w) => `<li>${esc(w)}</li>`).join("");
  const existing = out.dates_with_existing_import || [];
  document.getElementById("replace-wrap").hidden = !existing.length;
  document.getElementById("replace-dates").textContent = existing.join(", ");
  document.getElementById("preview").hidden = false;
}

async function importPreview(button) {
  const picked = [...document.querySelectorAll("#preview-rows input[type=checkbox]")]
    .filter((c) => c.checked).map((c) => {
      const i = Number(c.dataset.i);
      return { ...preview[i], date: document.querySelector(`#preview-rows input[data-date="${i}"]`).value || preview[i].date };
    });
  if (!picked.length) return toast("Nothing selected");
  const replace = document.getElementById("replace-existing").checked;
  const out = await act(button, () => api.post("/api/plan/import", { sessions: picked, replace_existing: replace }), { reload: false });
  if (out) {
    toast(`Saved ${out.created} session${out.created === 1 ? "" : "s"}${out.replaced ? `, replaced ${out.replaced}` : ""}`);
    setTimeout(() => location.reload(), 700);
  }
}

function openEditor(row) {
  const dialog = document.getElementById("editor");
  const form = document.getElementById("editor-form");
  form.reset();
  for (const el of form.elements) {
    if (el.name && row[el.name] !== undefined && row[el.name] !== null) el.value = row[el.name];
  }
  document.getElementById("editor-title").textContent = row.id ? "Edit planned session" : "Add planned session";
  document.getElementById("editor-delete").hidden = !row.id;
  dialog.showModal();
}

async function saveEditor(form) {
  let body;
  try { body = formData(form); } catch (e) { return toast(e.message); }
  const id = body.id;
  delete body.id;
  await act(form.querySelector("button[type=submit]"), () => (id ? api.put(`/api/plan/${id}`, body) : api.post("/api/plan", body)));
}

async function deleteRow(button) {
  const id = document.getElementById("editor-form").elements.id.value;
  await act(button, () => api.del(`/api/plan/${id}`), { confirmText: "Delete this planned session?" });
}

async function linkActivity(select, id) {
  if (!select.value) return;
  const activity_id = select.value === "__none" ? null : select.value;
  await act(select, () => api.post(`/api/plan/${id}/link`, { activity_id }));
}

async function garminText(button, id) {
  const pre = document.getElementById(`garmin-${id}`);
  if (!pre.hidden && pre.textContent.trim()) return copyText(button, pre.textContent);
  button.disabled = true;
  const label = button.textContent;
  button.textContent = "Converting…";
  try {
    const out = await api.post(`/api/plan/${id}/garmin`);
    pre.textContent = out.garmin_text;
    pre.hidden = false;
    button.textContent = "Copy Garmin text";
  } catch (e) {
    button.textContent = label;
    toast(e.message, 6000);
  } finally {
    button.disabled = false;
  }
}
