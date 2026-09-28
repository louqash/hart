// Ember chat: send, stream (SSE with automatic reconnect), tool trace,
// proposed-note cards, stop. Streaming text is inserted as plain text; the
// final answer is replaced by server-rendered, HTML-escaped markdown.

(() => {
  const state = { ...window.CHAT, running: null, es: null };
  const $ = (id) => document.getElementById(id);
  const messages = $("messages"), input = $("input"), send = $("send"), stop = $("stop"), model = $("model");
  const coarse = matchMedia("(pointer: coarse)").matches;

  const el = (tag, cls, text) => {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  };
  const scrollDown = () => requestAnimationFrame(() => { messages.scrollTop = messages.scrollHeight; });

  function addUser(text) {
    const m = el("div", "msg user");
    m.append(el("span", "who", "Athlete"), el("div", "bubble", text));
    messages.append(m);
    scrollDown();
  }

  function addEmber() {
    const m = el("div", "msg ai");
    const trace = el("details", "trace");
    const summary = el("summary", "", "");
    const list = el("ol");
    trace.append(summary, list);
    trace.hidden = true;
    const bubble = el("div", "bubble md streaming");
    const typing = el("span", "typing");
    typing.append(el("i"), el("i"), el("i"));
    bubble.append(typing);
    m.append(el("span", "who", "Ember"), trace, bubble);
    messages.append(m);
    scrollDown();
    return { m, trace, summary, list, bubble, typing, text: "", tools: {} };
  }

  function setRunning(on) {
    state.running = on;
    send.hidden = !!on;
    stop.hidden = !on;
    input.disabled = !!on;
    document.querySelectorAll(".starter").forEach((b) => (b.disabled = !!on));
  }

  function toolSummary(ui) {
    const n = Object.keys(ui.tools).length;
    const pending = Object.values(ui.tools).filter((t) => !t.done);
    ui.summary.textContent = pending.length
      ? `Consulting ${pending[pending.length - 1].name}…`
      : `Checked ${n} source${n === 1 ? "" : "s"}`;
  }

  function noteCard(ui, output) {
    let note;
    try { note = JSON.parse(output); } catch { return; }
    if (!note || !note.id || note.status !== "proposed") return;
    const card = el("div", "note-card");
    card.append(el("div", "eyebrow", "Proposed athlete note"),
      el("p", "", "Ember wants to remember something. Approve it to add it to your notes."));
    const row = el("div", "row");
    const ok = el("button", "small primary", "Approve");
    const no = el("button", "small secondary", "Dismiss");
    const view = el("a", "small", "Open notes →");
    view.href = `/notes#note-${note.id}`;
    ok.onclick = () => act(ok, () => api.post(`/api/notes/${note.id}/approve`), { reload: false })
      .then((r) => { if (r) { card.replaceChildren(el("p", "ok", "Note approved.")); } });
    no.onclick = () => act(no, () => api.post(`/api/notes/${note.id}/archive`), { reload: false })
      .then((r) => { if (r) { card.replaceChildren(el("p", "faint", "Note dismissed.")); } });
    row.append(ok, no, view);
    card.append(row);
    ui.m.append(card);
  }

  function seasonCard(ui, output, kind = "season") {
    let p;
    try { p = JSON.parse(output); } catch { return; }
    if (!p || !p.id || p.status !== "pending") return;
    const card = el("div", "note-card");
    card.append(el("div", "eyebrow", kind === "plan" ? "Suggested plan change" : "Suggested season change"), el("p", "", p.summary));
    const row = el("div", "row");
    const ok = el("button", "small primary", "Apply");
    const no = el("button", "small secondary", "Dismiss");
    const view = el("a", "small", kind === "plan" ? "Open plan →" : "Open season →");
    view.href = kind === "plan" ? "/plan#proposals" : "/season#proposals";
    ok.onclick = () => act(ok, () => api.post(`/api/season/proposals/${p.id}/apply`), { reload: false })
      .then((r) => {
        if (!r) return;
        card.replaceChildren(el("p", "ok", kind === "plan" ? "Applied to your plan." : "Applied to your season."));
        if (r.phases_stale) card.append(el("p", "small muted", "Your next A-race changed — regenerate the phase proposal on the Season page."));
      });
    no.onclick = () => act(no, () => api.post(`/api/season/proposals/${p.id}/dismiss`), { reload: false })
      .then((r) => { if (r) card.replaceChildren(el("p", "faint", "Dismissed.")); });
    row.append(ok, no, view);
    card.append(row);
    ui.m.append(card);
  }

  function healthCard(ui, output) {
    let p;
    try { p = JSON.parse(output); } catch { return; }
    if (!p || !p.id || p.status !== "proposed") return;
    const card = el("div", "note-card");
    card.append(el("div", "eyebrow", "Suggested health check"), el("p", "", p.title));
    const row = el("div", "row");
    const ok = el("button", "small primary", "Approve");
    const no = el("button", "small secondary", "Dismiss");
    const view = el("a", "small", "Open health →");
    view.href = "/health#proposed";
    ok.onclick = () => act(ok, () => api.post(`/api/health/checks/${p.id}/approve`), { reload: false })
      .then((r) => { if (r) card.replaceChildren(el("p", "ok", "Added to your health checks.")); });
    no.onclick = () => act(no, () => api.post(`/api/health/checks/${p.id}/dismiss`), { reload: false })
      .then((r) => { if (r) card.replaceChildren(el("p", "faint", "Dismissed.")); });
    row.append(ok, no, view);
    card.append(row);
    ui.m.append(card);
  }

  function handle(ui, ev) {
    switch (ev.type) {
      case "text":
        if (ui.typing.isConnected) ui.typing.remove();
        ui.text += ev.delta;
        ui.bubble.textContent = ui.text;
        scrollDown();
        break;
      case "tool_start": {
        ui.trace.hidden = false;
        const li = el("li");
        li.append(el("code", "", ev.name));
        if (ev.input && Object.keys(ev.input).length) li.append(el("span", "args", JSON.stringify(ev.input)));
        ui.list.append(li);
        ui.tools[ev.id] = { name: ev.name, li, done: false };
        toolSummary(ui);
        break;
      }
      case "tool_end": {
        const t = ui.tools[ev.id];
        if (!t) break;
        t.done = true;
        if (ev.is_error) t.li.classList.add("err");
        toolSummary(ui);
        if (t.name === "propose_athlete_note" && !ev.is_error) noteCard(ui, ev.output_excerpt);
        if (t.name === "propose_season_change" && !ev.is_error) seasonCard(ui, ev.output_excerpt);
        if (t.name === "propose_plan_change" && !ev.is_error) seasonCard(ui, ev.output_excerpt, "plan");
        if (t.name === "propose_health_check" && !ev.is_error) healthCard(ui, ev.output_excerpt);
        break;
      }
      case "notice":
        ui.m.insertBefore(el("p", "notice", ev.message), ui.bubble);
        break;
      case "done":
        if (ui.typing.isConnected) ui.typing.remove();
        ui.bubble.classList.remove("streaming");
        ui.bubble.innerHTML = ev.content_html; // server-rendered markdown, raw HTML escaped
        if (ev.status === "usage_limited") {
          const when = ev.resets_at ? new Date(ev.resets_at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : null;
          ui.m.append(el("p", "notice warn", when ? `Usage limit reached — resets at ${when}.` : "Usage limit reached."));
        } else if (ev.status === "auth_failed") {
          ui.m.append(el("p", "notice err", "Claude token problem — see the System page."));
        }
        scrollDown();
        break;
      case "error":
        ui.bubble.classList.remove("streaming");
        ui.bubble.textContent = `Something went wrong: ${ev.message}`;
        break;
      case "gone":
        ui.bubble.textContent = "This answer finished while you were away — reloading…";
        setTimeout(() => location.reload(), 800);
        break;
    }
  }

  function stream(runId, ui) {
    setRunning(runId);
    const es = new EventSource(`/api/chat/runs/${runId}/stream`);
    state.es = es;
    es.onmessage = (e) => handle(ui, JSON.parse(e.data));
    es.addEventListener("end", () => {
      es.close();
      setRunning(null);
      input.focus();
    });
    // EventSource reconnects by itself (sending Last-Event-ID) after network blips.
  }

  async function ask(text) {
    text = text.trim();
    if (!text || state.running) return;
    try {
      if (!state.conversationId) {
        const created = await api.post("/api/chat", { model: model.value, context: state.context || null });
        state.conversationId = created.id;
        history.replaceState(null, "", `/chat/${created.id}`);
      }
      document.querySelectorAll(".starters").forEach((s) => s.remove());
      addUser(text);
      input.value = "";
      autosize();
      const ui = addEmber();
      const { run_id } = await api.post(`/api/chat/${state.conversationId}/messages`, { text });
      stream(run_id, ui);
    } catch (e) {
      toast(e.message, 5000);
      setRunning(null);
    }
  }

  function autosize() {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 200) + "px";
  }

  $("composer").addEventListener("submit", (e) => { e.preventDefault(); ask(input.value); });
  input.addEventListener("input", autosize);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !coarse) { e.preventDefault(); ask(input.value); }
  });
  stop.addEventListener("click", () => state.running && api.post(`/api/chat/runs/${state.running}/stop`).catch(() => {}));
  model.addEventListener("change", () => {
    if (state.conversationId) api.request("PATCH", `/api/chat/${state.conversationId}`, { model: model.value }).catch((e) => toast(e.message));
  });
  document.querySelectorAll(".starter").forEach((b) => b.addEventListener("click", () => ask(b.dataset.q)));

  // Re-attach to an answer still being written (e.g. the page was reloaded).
  if (state.activeRun) stream(state.activeRun, addEmber());
  scrollDown();
  if (!coarse) input.focus();
})();
