// ECharts helpers: theme from CSS variables, gap handling, background bands
// for training phases and annotations (injury, no watch, events).

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

const emptyChart = (id, text) => {
  const el = document.getElementById(id);
  if (el) el.outerHTML = `<p class="empty">${text}</p>`;
};

const SPORT_COLORS = () => ({
  swim: css("--swim"), bike: css("--bike"), run: css("--run"), strength: css("--strength"), other: css("--other"),
});

// Forest-dawn palette: phases in moss/river/amber washes, annotations in ember/stone.
const PHASE_COLORS = {
  comeback: "rgba(110,184,134,0.10)", base: "rgba(111,168,201,0.07)", build: "rgba(226,164,95,0.08)",
  peak: "rgba(226,125,96,0.10)", taper: "rgba(110,184,134,0.12)", race: "rgba(226,125,96,0.30)",
  transition: "rgba(139,163,147,0.10)",
};
const ANNOTATION_COLORS = {
  injury: "rgba(226,125,96,0.14)", illness: "rgba(229,96,74,0.16)", no_device: "rgba(91,117,100,0.25)",
  event: "rgba(111,168,201,0.16)", travel: "rgba(111,168,201,0.12)", race: "rgba(226,125,96,0.25)",
  other: "rgba(91,117,100,0.18)",
};

const charts = [];
window.addEventListener("resize", () => charts.forEach((c) => { c.resize(); if (c.__relayout) c.__relayout(); }));

function makeChart(el, option) {
  if (typeof el === "string") el = document.getElementById(el);
  if (!el) return null;
  const existing = echarts.getInstanceByDom(el);
  if (existing) existing.dispose();
  const chart = echarts.init(el, null, { renderer: "canvas" });
  chart.setOption(themed(option));
  charts.push(chart);
  return chart;
}

function themed(option) {
  const muted = css("--muted"), line = css("--line"), soft = css("--line-soft"), text = css("--cream");
  // Merge per key so a chart's own axis options keep the theme colours.
  const axis = (a) => ({
    ...a,
    axisLine: { ...(a.axisLine || {}), lineStyle: { color: line, ...(a.axisLine?.lineStyle || {}) } },
    axisTick: { ...(a.axisTick || {}), lineStyle: { color: line, ...(a.axisTick?.lineStyle || {}) } },
    axisLabel: { color: muted, fontSize: 11, ...(a.axisLabel || {}) },
    splitLine: { ...(a.splitLine || {}), lineStyle: { color: soft, ...(a.splitLine?.lineStyle || {}) } },
    nameTextStyle: { color: muted, fontSize: 11, ...(a.nameTextStyle || {}) },
  });
  const arr = (v) => (Array.isArray(v) ? v : v ? [v] : []);
  return {
    animation: false,
    textStyle: { color: text, fontFamily: css("--sans") },
    grid: { left: 44, right: 44, top: 34, bottom: 30, containLabel: false },
    legend: { top: 0, textStyle: { color: muted, fontSize: 12 }, itemWidth: 14, itemHeight: 8 },
    tooltip: {
      trigger: "axis", backgroundColor: "rgba(17,31,23,0.96)", borderColor: line, borderRadius: 12,
      textStyle: { color: text, fontSize: 12 }, extraCssText: "box-shadow: 0 10px 30px rgba(0,0,0,.5);",
      valueFormatter: (v) => (v == null ? "—" : typeof v === "number" ? +v.toFixed(2) : v),
    },
    ...option,
    // Hide internal helper series (names starting with "_") from every legend.
    legend: option.legend && option.legend.show === false ? option.legend : {
      top: 0, textStyle: { color: muted, fontSize: 12 }, itemWidth: 14, itemHeight: 8,
      data: arr(option.series).filter((x) => x.name && !x.name.startsWith("_")).map((x) => x.name),
      ...(option.legend || {}),
    },
    xAxis: arr(option.xAxis).map(axis),
    yAxis: arr(option.yAxis).map(axis),
  };
}

// Insert nulls where consecutive points are more than maxGapDays apart, so
// missing data (e.g. the watch wasn't worn) shows as a gap, not a line.
function withGaps(points, maxGapDays = 2) {
  const out = [];
  for (let i = 0; i < points.length; i++) {
    if (i > 0) {
      const gap = (new Date(points[i][0]) - new Date(points[i - 1][0])) / 86400000;
      if (gap > maxGapDays) out.push([points[i - 1][0], null]);
    }
    out.push(points[i]);
  }
  return out;
}

function pts(rows, key, dateKey = "date") {
  return rows.filter((r) => r[key] != null).map((r) => [r[dateKey], r[key]]);
}

// Background bands for training phases and annotations, as two series:
// the shading sits behind everything (z 0) and a transparent copy carries the
// labels on top (z 20), on small dark pills so bars and lines never hide them.
// Phase names sit in a strip above the plot (leave grid.top ≥ 46).
function bandSeries({ phases = [], annotations = [] }, { labels = true } = {}) {
  const cream = css("--cream");
  const pill = { color: cream, fontSize: 10, backgroundColor: "rgba(10,20,15,0.88)", padding: [2, 6], borderRadius: 4 };
  const shade = [], tags = [];
  for (const p of phases) {
    shade.push([{ xAxis: p.start_date, itemStyle: { color: PHASE_COLORS[p.phase_type] || "rgba(0,0,0,0.04)" } }, { xAxis: p.end_date }]);
    tags.push([{ xAxis: p.start_date, name: p.name, label: { ...pill, position: "top", distance: 3 } }, { xAxis: p.end_date }]);
  }
  annotations.forEach((a, i) => {
    shade.push([{ xAxis: a.start_date, itemStyle: { color: ANNOTATION_COLORS[a.kind] || "rgba(0,0,0,0.06)" } }, { xAxis: a.end_date }]);
    // Bottom-left of the band, on a pill above the bars; phase names use the strip on top.
    // Alternate heights so labels of nearby bands (e.g. injury and no-watch) don't collide.
    tags.push([{ xAxis: a.start_date, name: a.label,
      label: { ...pill, position: "insideBottomLeft", distance: 6 + (i % 2) * 22 } }, { xAxis: a.end_date }]);
  });
  const series = [{ name: "_bands", type: "line", data: [], z: 0, silent: true, tooltip: { show: false },
    markArea: { silent: true, data: shade } }];
  if (labels) {
    series.push({ name: "_band_labels", type: "line", data: [], z: 20, silent: true, tooltip: { show: false },
      markArea: { silent: true, itemStyle: { color: "transparent" }, data: tags } });
  }
  return series;
}

// Legend entries for real series only (hide the band helpers).
const legendFor = (series) => ({ data: series.filter((x) => x.name && !x.name.startsWith("_")).map((x) => x.name) });

const DURATION_LABELS = {
  1: "1s", 5: "5s", 10: "10s", 30: "30s", 60: "1m", 120: "2m", 300: "5m", 600: "10m",
  1200: "20m", 1800: "30m", 3600: "1h", 5400: "1.5h", 7200: "2h",
};

function segmented(el, onChange) {
  el.addEventListener("click", (e) => {
    const b = e.target.closest("button[data-v]");
    if (!b) return;
    el.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
    onChange(b.dataset.v);
  });
}

function fmtMinutes(sec) {
  const m = Math.round(sec / 60);
  return `${Math.floor(m / 60)}:${String(m % 60).padStart(2, "0")}`;
}

// ---------------------------------------------------------------------------
// Band labels above the plot. ECharts can't keep markArea labels clear of bars
// and lines, nor stop narrow bands' labels colliding, so labels for phases,
// annotations and point marks (today, races) are laid out here: in rows above
// the plot, each over its band with a bracket showing the band's extent, a
// row chosen so labels never overlap, shortened with "…" only as a last resort.
// ---------------------------------------------------------------------------

const _measureCtx = document.createElement("canvas").getContext("2d");
function textWidth(text, size = 10) {
  _measureCtx.font = `600 ${size}px ${css("--sans") || "sans-serif"}`;
  return _measureCtx.measureText(text).width;
}

function fitText(text, maxWidth) {
  if (textWidth(text) + 12 <= maxWidth) return text;
  for (let n = text.length - 1; n >= 3; n--) {  // fewer than 3 letters says nothing — leave it to hover
    const t = text.slice(0, n).trimEnd() + "…";
    if (textWidth(t) + 12 <= maxWidth) return t;
  }
  return null;
}

// Greedy row packing: items sorted by position; each goes into the first row
// whose last label ends before it starts. Returns the number of rows used.
function packRows(items, maxRows, left, right, gap = 6) {
  const ends = [];
  for (const it of items.sort((a, b) => a.cx - b.cx)) {
    const place = (w) => Math.min(Math.max(it.cx - w / 2, left), right - w);
    let row = ends.findIndex((end) => end + gap <= place(it.w));
    if (row === -1 && ends.length < maxRows) row = ends.push(-Infinity) - 1;
    if (row === -1) {
      // No free row: shorten the label to the space left in the emptiest row.
      row = ends.indexOf(Math.min(...ends));
      const from = ends[row] + gap;
      const text = fitText(it.text, Math.max(it.x1, it.cx + 20) - from);
      if (!text) { it.row = -1; continue; }
      it.text = text;
      it.w = textWidth(text) + 12;
      it.lx = Math.max(from, place(it.w));
    } else {
      it.lx = place(it.w);
    }
    it.row = row;
    ends[row] = it.lx + it.w;
  }
  return ends.length;
}

// Solid lane colours (the in-plot shading stays faint).
const LANE_PHASE = {
  comeback: "#3f7a55", base: "#3d6a82", build: "#8a6a3a", peak: "#9a5a44", taper: "#4f8a62", race: "#b0503c",
  transition: "#56695c",
};
const LANE_NOTE = {
  injury: "#9a5a44", illness: "#a3453a", no_device: "#4d5f53", event: "#3d6a82", travel: "#3d6a82", race: "#b0503c",
  other: "#4d5f53",
};

let _lanePop = null;
function lanePop(text, x, y) {
  if (!_lanePop) {
    _lanePop = document.createElement("div");
    _lanePop.className = "tip-pop";
    _lanePop.style.whiteSpace = "pre-line";
    document.body.appendChild(_lanePop);
  }
  if (!text) { _lanePop.style.display = "none"; return; }
  _lanePop.textContent = text;
  _lanePop.style.display = "block";
  const w = _lanePop.offsetWidth;
  _lanePop.style.left = `${Math.min(Math.max(8, x - w / 2), window.innerWidth - w - 8)}px`;
  _lanePop.style.top = `${y - _lanePop.offsetHeight - 8}px`;
}
window.addEventListener("scroll", () => lanePop(null), { passive: true });

// Timeline lanes above the plot: phases as solid bars in one lane, annotations
// in a second lane (a sub-row only where two overlap in time), named inside
// when the name fits (hover shows it otherwise); "today" and races as small
// markers on top whose dashed lines run down into the plot.
function bandChart(el, option, bands = {}, { marks = [] } = {}) {
  const phases = bands.phases || [], annotations = bands.annotations || [];
  // Start at the height the lanes needed last time, so the page doesn't shift once they're drawn.
  const dom = typeof el === "string" ? document.getElementById(el) : el;
  if (!dom) return null;
  const heightKey = `chartExtra:${location.pathname}:${dom.id}`;
  if (dom.__baseHeight === undefined) {
    dom.__baseHeight = dom.clientHeight;
    let extra = 0;
    try { extra = Number(localStorage.getItem(heightKey)) || 0; } catch {}
    if (extra) dom.style.height = `${dom.__baseHeight + extra}px`;
  }
  const chart = makeChart(dom, { ...option, series: [...bandSeries(bands, { labels: false }), ...option.series] });
  if (!chart) return null;
  const legendH = option.legend && option.legend.show === false ? 2 : 26;
  const laneH = 16, gap = 3, markH = 15;
  const cream = css("--cream"), muted = css("--muted"), font = `600 10px ${css("--sans")}`;
  const fmt = (d) => new Date(d).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });

  const relayout = () => {
    const grid = chart.getModel().getComponent("grid");
    if (!grid || !grid.coordinateSystem) return;
    const rect = grid.coordinateSystem.getRect();
    const left = rect.x, right = rect.x + rect.width;
    const px = (d) => chart.convertToPixel({ xAxisIndex: 0 }, new Date(d).getTime());
    const today = new Date().toISOString().slice(0, 10);
    const span = (start, end) => {
      const x0 = Math.max(px(start), left), x1 = Math.min(px(end || today) + 1, right);
      return x1 > left && x0 < right && x1 > x0 ? [x0, x1] : null;
    };

    // Narrow charts: consecutive blocks of one phase type share a bar ("Base ×6").
    let shown = phases.map((p) => ({ ...p, count: 1 }));
    if (rect.width < 600) {
      shown = [];
      for (const p of phases) {
        const last = shown[shown.length - 1];
        if (last && last.phase_type === p.phase_type) {
          last.end_date = p.end_date;
          last.count += 1;
          last.name = `${p.phase_type[0].toUpperCase()}${p.phase_type.slice(1)} ×${last.count}`;
        } else shown.push({ ...p, count: 1 });
      }
    }

    // Annotation sub-rows: only where annotations overlap in time.
    const notes = annotations.map((a) => ({ ...a, x: span(a.start_date, a.end_date) })).filter((a) => a.x)
      .sort((a, b) => a.x[0] - b.x[0]);
    const rowEnds = [];
    for (const a of notes) {
      let row = rowEnds.findIndex((end) => end + 2 <= a.x[0]);
      if (row === -1) row = rowEnds.push(0) - 1;
      a.row = row;
      rowEnds[row] = a.x[1];
    }
    const noteRows = rowEnds.length;
    const hasMarks = marks.some((m) => span(m.xAxis, m.xAxis));
    const yMarks = legendH, yPhase = legendH + (hasMarks ? markH : 0);
    const yNotes = yPhase + (shown.length ? laneH + gap : 0);
    const lanesBottom = yNotes + noteRows * (laneH + gap);

    const children = [];
    // Label placement: full name inside the bar, else beside it (right, then left) where the lane is
    // free, else shortened inside; annotations only spill (phases sit edge to edge).
    const bar = (x0, x1, y, color, name, tooltip, free = null) => {
      const w = x1 - x0;
      const hover = {
        onmouseover: () => { const r = chart.getDom().getBoundingClientRect(); lanePop(tooltip, r.left + (x0 + x1) / 2, r.top + y); },
        onmouseout: () => lanePop(null),
      };
      children.push({ type: "rect", shape: { x: x0 + 0.5, y, width: Math.max(w - 1, 1.5), height: laneH, r: 3 },
        style: { fill: color }, cursor: "default", ...hover });
      const full = textWidth(name) + 8;
      const put = (x, text, fill) => children.push({ type: "text", silent: true, x, y: y + 3, style: { text, fill, font } });
      if (full + 4 <= w) return put(x0 + 6, name, cream);
      if (free && free.right - x1 - 4 >= full) return put(x1 + 4, name, muted);
      if (free && x0 - free.left - 4 >= full) return put(x0 - full - 2, name, muted);
      const inside = fitText(name, w - 2);
      if (inside) return put(x0 + 6, inside, cream);
      if (free) {
        const outside = fitText(name, free.right - x1 + 2);
        if (outside) put(x1 + 4, outside, muted);
      }
    };
    for (const p of shown) {
      const x = span(p.start_date, p.end_date);
      if (x) bar(x[0], x[1], yPhase, LANE_PHASE[p.phase_type] || "#4d5f53", p.name,
        `${p.name} — ${fmt(p.start_date)} → ${fmt(p.end_date)}${p.goal ? `\n${p.goal}` : ""}`);
    }
    // Text placed beside a bar claims lane space, so later labels don't run into it.
    const claimed = [];
    for (const a of notes) {
      const same = notes.filter((b) => b !== a && b.row === a.row);
      const walls = [...same.map((b) => b.x), ...claimed.filter((c) => c.row === a.row).map((c) => c.x)];
      const free = {
        right: Math.min(right, ...walls.filter((x) => x[0] >= a.x[1]).map((x) => x[0])),
        left: Math.max(left, ...walls.filter((x) => x[1] <= a.x[0]).map((x) => x[1])),
      };
      const before = children.length;
      bar(a.x[0], a.x[1], yNotes + a.row * (laneH + gap), LANE_NOTE[a.kind] || "#4d5f53", a.label,
        `${a.label} — ${fmt(a.start_date)} → ${a.end_date ? fmt(a.end_date) : "ongoing"}`, free);
      const label = children.length > before + 1 ? children[children.length - 1] : null;
      if (label && (label.x < a.x[0] || label.x > a.x[1])) {
        claimed.push({ row: a.row, x: [label.x, label.x + textWidth(label.style.text) + 4] });
      }
    }
    // Markers: a label on top and a dashed line through the lanes (the plot has its own markLine).
    const markItems = marks.map((m) => ({ ...m, x: span(m.xAxis, m.xAxis) })).filter((m) => m.x)
      .map((m) => ({ text: m.name, cx: m.x[0], x0: m.x[0], x1: m.x[0], w: textWidth(m.name) + 12 }));
    if (markItems.length) packRows(markItems, 1, left, right);
    for (const m of markItems) {
      children.push({ type: "line", silent: true, shape: { x1: m.cx, y1: yMarks + 12, x2: m.cx, y2: rect.y },
        style: { stroke: "rgba(242,232,213,0.45)", lineWidth: 1, lineDash: [3, 3] } });
      if (m.row >= 0) {
        children.push({ type: "text", silent: true, x: m.lx + 6, y: yMarks,
          style: { text: m.text, fill: muted, font } });
      }
    }

    const top = lanesBottom + 20;  // + room for the y-axis names above the plot
    chart.setOption({ grid: { top }, graphic: [{ type: "group", id: "band-labels", children }] }, { replaceMerge: ["graphic"] });
    const extra = Math.max(0, top - 50);
    try { localStorage.setItem(heightKey, String(extra)); } catch {}
    const wanted = dom.__baseHeight + extra;
    if (Math.abs(dom.clientHeight - wanted) > 1) {
      dom.style.height = `${wanted}px`;
      chart.resize();
    }
  };
  relayout();
  chart.__relayout = relayout;
  return chart;
}
