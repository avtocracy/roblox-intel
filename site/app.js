/* Roblox Intel dashboard. Plain JS + inline SVG, no dependencies.
   Every string from data/ is inserted as text (textContent), never parsed as HTML. */
"use strict";
(() => {
  const SVGNS = "http://www.w3.org/2000/svg";
  const SLOTS = 6; // categorical slots used for plotted series (validated palette, both modes)
  const WINDOW_DAYS = { "24h": [1, 0], "7d": [7, 1], "30d": [30, 3] }; // [days back, tolerance]
  const data = {};
  const tableViews = new Set();

  // ---------- small utilities ----------
  const store = {
    get(key, fallback) {
      try { const v = localStorage.getItem("ri:" + key); return v === null ? fallback : JSON.parse(v); }
      catch { return fallback; }
    },
    set(key, value) { try { localStorage.setItem("ri:" + key, JSON.stringify(value)); } catch { /* private mode */ } },
  };
  const $ = (sel) => document.querySelector(sel);
  const safeUrl = (u) => (typeof u === "string" && /^https:\/\//i.test(u) ? u : null);

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k === "href") { const u = safeUrl(v); if (u) { node.href = u; node.rel = "noopener"; node.target = "_blank"; } }
      else if (k === "src") { const u = safeUrl(v); if (u) node.src = u; }
      else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
      else node.setAttribute(k, v === true ? "" : String(v));
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }
  function svg(tag, attrs) {
    const node = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined) continue;
      if (k === "text") node.textContent = v;
      else if (k === "style") node.setAttribute("style", v);
      else node.setAttribute(k, String(v));
    }
    return node;
  }
  function fmtNum(n) {
    if (n === null || n === undefined || Number.isNaN(n)) return "–";
    const sign = n < 0 ? "-" : "";
    let a = Math.abs(n);
    for (const [lim, suf] of [[1e9, "B"], [1e6, "M"], [1e3, "k"]]) {
      if (a >= lim * 0.9995) {
        const v = a / lim;
        let t = v < 100 ? v.toFixed(1) : v.toFixed(0);
        if (t.includes(".")) t = t.replace(/\.?0+$/, "");
        return sign + t + suf;
      }
    }
    return sign + Math.round(a).toLocaleString("en-US");
  }
  function fmtPct(p, signed = true) {
    if (p === null || p === undefined || Number.isNaN(p)) return "–";
    const a = Math.abs(p);
    const t = a >= 1000 ? Math.round(p).toLocaleString("en-US") : a >= 10 ? p.toFixed(0) : p.toFixed(1);
    return (signed && p > 0 ? "+" : "") + t + "%";
  }
  const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  function fmtDate(iso, withYear = false) {
    if (!iso) return "";
    const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
    return `${MONTHS[m - 1]} ${d}` + (withYear ? `, ${y}` : "");
  }
  const dayNum = (iso) => Date.UTC(+iso.slice(0, 4), +iso.slice(5, 7) - 1, +iso.slice(8, 10)) / 86400000;
  const pctChange = (now, then) => (now === null || then === null || then === undefined || now === undefined || then <= 0 ? null : (now - then) / then * 100);

  /** Inline markdown subset used by the report: **bold**, [text](https://...), \-escapes. */
  function renderInline(text) {
    const frag = document.createDocumentFragment();
    const re = /\[((?:\\.|[^\]])+)\]\((https:\/\/[^\s)]+)\)|\*\*((?:\\.|[^*])+)\*\*/g;
    const unescape = (s) => s.replace(/\\(.)/g, "$1");
    let last = 0, m;
    while ((m = re.exec(text))) {
      if (m.index > last) frag.append(unescape(text.slice(last, m.index)));
      if (m[1] !== undefined) frag.append(el("a", { href: m[2] }, renderInline(m[1])));
      else frag.append(el("strong", { text: unescape(m[3]) }));
      last = re.lastIndex;
    }
    if (last < text.length) frag.append(unescape(text.slice(last)));
    return frag;
  }

  // ---------- tooltip ----------
  const tip = $("#tooltip");
  function showTip(x, y, title, rows) {
    tip.replaceChildren(el("div", { class: "tt-title", text: title }));
    for (const r of rows) {
      tip.append(el("div", { class: "tt-row" },
        r.color ? el("span", { class: "key-line", style: `background:${r.color}` }) : null,
        el("strong", { text: r.value }), el("span", { text: r.label })));
    }
    tip.hidden = false;
    const w = tip.offsetWidth, h = tip.offsetHeight;
    const left = Math.min(window.innerWidth - w - 8, x + 14);
    const top = Math.min(window.innerHeight - h - 8, Math.max(8, y - h / 2));
    tip.style.left = `${Math.max(8, left)}px`;
    tip.style.top = `${top}px`;
  }
  const hideTip = () => { tip.hidden = true; };

  // ---------- selections: color follows the entity, never its rank ----------
  function selection(key, max = SLOTS) {
    let items = store.get(key, null); // [{id, slot}]
    return {
      init(validIds, defaults) {
        // keep the viewer's picks that still exist (and their colors); otherwise start from the defaults
        items = Array.isArray(items) ? items.filter((i) => validIds.has(String(i.id))).slice(0, max) : [];
        if (!items.length) items = defaults.slice(0, max).map((id, i) => ({ id: String(id), slot: i + 1 }));
      },
      has: (id) => items.some((i) => i.id === String(id)),
      slot: (id) => (items.find((i) => i.id === String(id)) || {}).slot,
      ids: () => items.map((i) => i.id),
      toggle(id) {
        id = String(id);
        if (this.has(id)) items = items.filter((i) => i.id !== id);
        else {
          if (items.length >= max) return false;
          const used = new Set(items.map((i) => i.slot));
          let slot = 1; while (used.has(slot)) slot++;
          items.push({ id, slot });
        }
        store.set(key, items);
        return true;
      },
    };
  }
  const colorOf = (slot) => `var(--series-${slot})`;

  // ---------- range / window helpers ----------
  function rangeIndex(dates) {
    if (!dates.length) return 0;
    const lastDay = dayNum(dates[dates.length - 1]);
    const idx = dates.findIndex((d) => lastDay - dayNum(d) <= state.range - 1);
    return idx < 0 ? 0 : idx;
  }
  function baseIndex(dates, win) {
    const [back, tol] = WINDOW_DAYS[win];
    if (!dates.length) return -1;
    const target = dayNum(dates[dates.length - 1]) - back;
    for (let i = dates.length - 1; i >= 0; i--) {
      const d = dayNum(dates[i]);
      if (d <= target && d >= target - tol) return i;
      if (d < target - tol) break;
    }
    return -1;
  }

  // ---------- charts ----------
  function niceStep(span, count = 4) {
    const raw = span / count;
    const mag = 10 ** Math.floor(Math.log10(raw || 1));
    const norm = raw / mag;
    return (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag;
  }

  function sparkline(values, w = 96, h = 24) {
    const pts = values.map((v, i) => [i, v]).filter((p) => p[1] !== null && p[1] !== undefined);
    const root = svg("svg", { class: "spark", width: w, height: h, viewBox: `0 0 ${w} ${h}`, "aria-hidden": "true" });
    if (!pts.length) return root;
    const vals = pts.map((p) => p[1]);
    const min = Math.min(...vals), max = Math.max(...vals);
    const n = Math.max(1, values.length - 1);
    const x = (i) => 3 + (i / n) * (w - 6);
    const y = (v) => (max === min ? h / 2 : 3 + (1 - (v - min) / (max - min)) * (h - 6));
    if (pts.length > 1) {
      root.append(svg("polyline", { class: "spark-line", points: pts.map((p) => `${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`).join(" ") }));
    }
    const lastPt = pts[pts.length - 1];
    root.append(svg("circle", { class: "spark-dot", cx: x(lastPt[0]), cy: y(lastPt[1]), r: 3 }));
    return root;
  }

  /** Multi-series line chart with crosshair tooltip, legend, selective end labels, table view. */
  function lineChart(container, { dates, series, mode = "abs", valueLabel = "players", empty }) {
    container.replaceChildren();
    if (!dates.length || !series.length) {
      container.append(el("div", { class: "empty", text: empty || "No data yet." }));
      return;
    }
    // indexed mode: each series relative to its first value in range (=100)
    const rows = series.map((s) => {
      if (mode !== "index") return { ...s, plot: s.values };
      const first = s.values.find((v) => v !== null && v !== undefined && v > 0);
      return { ...s, plot: s.values.map((v) => (v === null || v === undefined || !first ? null : (v / first) * 100)) };
    });
    const fmt = (v) => (v === null || v === undefined ? "–" : mode === "index" ? v.toFixed(0) : fmtNum(v));
    if (series.length > 1) {
      container.append(el("div", { class: "legend" }, rows.map((s) =>
        el("span", { class: "legend-item" }, el("span", { class: "key-line", style: `background:${s.color}` }), s.name))));
    }
    if (tableViews.has(container.id)) {
      container.append(chartTable(dates, rows, fmt));
      return;
    }
    const width = Math.max(280, container.clientWidth || 640);
    const plotH = width < 520 ? 200 : 260;
    const all = rows.flatMap((s) => s.plot).filter((v) => v !== null && v !== undefined);
    let lo = mode === "index" ? Math.min(100, ...all) : 0;
    let hi = Math.max(mode === "index" ? 100 : 1, ...all);
    if (mode === "index") { const pad = (hi - lo) * 0.08 || 5; lo -= pad; hi += pad; }
    const step = niceStep(hi - lo);
    lo = Math.floor(lo / step) * step;
    hi = Math.ceil(hi / step) * step;
    const ticks = [];
    for (let t = lo; t <= hi + step / 2; t += step) ticks.push(t);
    const tickText = (t) => (mode === "index" ? String(Math.round(t)) : fmtNum(t));
    const left = Math.max(30, 8 + 6.5 * Math.max(...ticks.map((t) => tickText(t).length)));
    const endLabels = rows.length <= 4 && width >= 520;
    const right = endLabels ? 118 : 14;
    const top = 8, axisBand = 24;
    const plotW = width - left - right;
    const n = dates.length;
    const d0 = dayNum(dates[0]), span = Math.max(1, dayNum(dates[n - 1]) - d0);
    const xDay = (day) => (n === 1 ? left + plotW / 2 : left + ((day - d0) / span) * plotW);
    const x = (i) => xDay(dayNum(dates[i]));  // by calendar day, so a missed run shows as a gap in time
    const y = (v) => top + (1 - (v - lo) / (hi - lo)) * plotH;

    const root = svg("svg", { width, height: top + plotH + axisBand, viewBox: `0 0 ${width} ${top + plotH + axisBand}`,
      role: "img", "aria-label": `${valueLabel} over time for ${rows.map((r) => r.name).join(", ")}` });
    for (const t of ticks) {
      root.append(svg("line", { class: t === lo && mode !== "index" ? "baseline" : "gridline", x1: left, x2: left + plotW, y1: y(t), y2: y(t) }));
      root.append(svg("text", { class: "tick", x: left - 6, y: y(t) + 4, "text-anchor": "end", text: tickText(t) }));
    }
    if (mode === "index") root.append(svg("line", { class: "baseline", x1: left, x2: left + plotW, y1: y(100), y2: y(100) }));
    // evenly spaced day ticks, at least ~64px apart
    const maxTicks = Math.max(2, Math.floor(plotW / 64));
    const stepDays = n === 1 ? 1 : [1, 2, 3, 7, 14, 30, 60, 90].find((s) => span / s <= maxTicks - 1) || 180;
    const tickDays = [];
    for (let d = d0; d <= d0 + span; d += stepDays) tickDays.push(d);
    for (const day of tickDays) {
      const iso = new Date(day * 86400000).toISOString().slice(0, 10);
      const anchor = n === 1 ? "middle" : day === d0 ? "start" : xDay(day) > left + plotW - 24 ? "end" : "middle";
      root.append(svg("text", { class: "tick", x: xDay(day), y: top + plotH + 17, "text-anchor": anchor, text: fmtDate(iso) }));
    }
    const ends = [];
    for (const s of rows) {
      let d = "", pen = false;
      s.plot.forEach((v, i) => {
        if (v === null || v === undefined) { pen = false; return; }
        d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
        pen = true;
      });
      if (d.includes("L")) root.append(svg("path", { class: "series-line", d, style: `stroke:${s.color}` }));
      const lastI = s.plot.map((v) => v !== null && v !== undefined).lastIndexOf(true);
      if (lastI >= 0) {
        root.append(svg("circle", { cx: x(lastI), cy: y(s.plot[lastI]), r: 4, style: `fill:${s.color};stroke:var(--surface-1);stroke-width:2` }));
        ends.push({ y: y(s.plot[lastI]), x: x(lastI), name: s.name, value: fmt(s.plot[lastI]) });
      }
    }
    if (endLabels && ends.length) {
      ends.sort((a, b) => a.y - b.y);
      const collide = ends.some((e, i) => i > 0 && e.y - ends[i - 1].y < 14);
      if (!collide) {
        for (const e of ends) {
          const label = e.name.length > 16 ? e.name.slice(0, 15) + "…" : e.name;
          root.append(svg("text", { class: "end-label", x: e.x + 9, y: e.y + 4, text: `${label} ${e.value}` }));
        }
      }
    }
    // hover / keyboard layer
    const cross = svg("line", { class: "crosshair", y1: top, y2: top + plotH, visibility: "hidden" });
    root.append(cross);
    const hit = svg("rect", { x: left - 6, y: top, width: plotW + 12, height: plotH, fill: "transparent" });
    root.append(hit);
    let active = -1;
    const show = (i, clientX, clientY) => {
      active = Math.max(0, Math.min(n - 1, i));
      cross.setAttribute("x1", x(active)); cross.setAttribute("x2", x(active));
      cross.setAttribute("visibility", "visible");
      const list = rows.map((s) => ({ color: s.color, label: s.name, raw: s.plot[active], value: fmt(s.plot[active]) }))
        .sort((a, b) => (b.raw ?? -Infinity) - (a.raw ?? -Infinity));
      const box = root.getBoundingClientRect();
      showTip(clientX ?? box.left + x(active), clientY ?? box.top + top + plotH / 3, fmtDate(dates[active], true)
        + (mode === "index" ? " · index (first day = 100)" : ""), list);
    };
    const hide = () => { cross.setAttribute("visibility", "hidden"); hideTip(); };
    hit.addEventListener("pointermove", (ev) => {
      const box = root.getBoundingClientRect();
      const px = (ev.clientX - box.left) * (width / box.width);
      let best = 0;  // snap to the nearest snapshot date
      for (let i = 1; i < n; i++) if (Math.abs(x(i) - px) < Math.abs(x(best) - px)) best = i;
      show(best, ev.clientX, ev.clientY);
    });
    hit.addEventListener("pointerleave", hide);
    container.tabIndex = 0;
    container.onkeydown = (ev) => {
      if (ev.key === "ArrowRight" || ev.key === "ArrowLeft") {
        ev.preventDefault();
        show(active < 0 ? n - 1 : active + (ev.key === "ArrowRight" ? 1 : -1));
      } else if (ev.key === "Escape") hide();
    };
    container.onfocus = () => show(active < 0 ? n - 1 : active);
    container.onblur = hide;
    container.append(root);
    if (n === 1) container.append(el("p", { class: "muted small", text: "One snapshot so far: lines appear after the next daily run." }));
  }

  function chartTable(dates, rows, fmt) {
    const table = el("table", { class: "data chart-table" });
    table.append(el("thead", {}, el("tr", {}, el("th", { text: "Date" }), rows.map((r) => el("th", { class: "num", text: r.name })))));
    const body = el("tbody");
    for (let i = dates.length - 1; i >= 0; i--) {
      body.append(el("tr", {}, el("td", { text: fmtDate(dates[i], true) }), rows.map((r) => el("td", { class: "num", text: fmt(r.plot[i]) }))));
    }
    table.append(body);
    return el("div", { class: "table-wrap" }, table);
  }

  /** Horizontal bars from a zero baseline; negative values extend left (diverging poles). */
  function barChart(container, { rows, format, diverging = true, tableId, label }) {
    container.replaceChildren();
    if (!rows.length) { container.append(el("div", { class: "empty", text: "No data yet." })); return; }
    if (tableViews.has(tableId)) {
      const table = el("table", { class: "data chart-table" },
        el("thead", {}, el("tr", {}, el("th", { text: "Genre" }), el("th", { class: "num", text: label }), el("th", { class: "num", text: "Players" }))),
        el("tbody", {}, rows.map((r) => el("tr", {}, el("td", { text: r.label }), el("td", { class: "num", text: format(r.value) }), el("td", { class: "num", text: r.detail || "" })))));
      container.append(el("div", { class: "table-wrap" }, table));
      return;
    }
    const width = Math.max(280, container.clientWidth || 420);
    const labelW = Math.min(150, Math.max(70, 6.6 * Math.max(...rows.map((r) => r.label.length))));
    const valueRoom = 52, barH = 16, rowH = 26, top = 4;
    const minV = Math.min(0, ...rows.map((r) => r.value)), maxV = Math.max(0, ...rows.map((r) => r.value));
    const plotL = labelW + 8, plotR = width - 4;
    const span = (maxV - minV) || 1;
    const usable = plotR - plotL - valueRoom * (minV < 0 ? 2 : 1);
    const zeroX = plotL + (minV < 0 ? valueRoom : 0) + ((0 - minV) / span) * usable;
    const scale = usable / span;
    const height = top + rows.length * rowH + 4;
    const root = svg("svg", { width, height, viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": label });
    root.append(svg("line", { class: "baseline", x1: zeroX, x2: zeroX, y1: 0, y2: height }));
    rows.forEach((r, i) => {
      const cy = top + i * rowH + rowH / 2;
      const len = Math.max(1, Math.abs(r.value) * scale);
      const pos = r.value >= 0;
      const x0 = pos ? zeroX : zeroX - len;
      const rad = Math.min(4, len / 2);
      // rounded data-end, square at the baseline
      const d = pos
        ? `M${x0},${cy - barH / 2}h${len - rad}a${rad},${rad} 0 0 1 ${rad},${rad}v${barH - 2 * rad}a${rad},${rad} 0 0 1 -${rad},${rad}h-${len - rad}z`
        : `M${zeroX},${cy - barH / 2}h-${len - rad}a${rad},${rad} 0 0 0 -${rad},${rad}v${barH - 2 * rad}a${rad},${rad} 0 0 0 ${rad},${rad}h${len - rad}z`;
      const g = svg("g", { tabindex: "0", role: "listitem", "aria-label": `${r.label}: ${format(r.value)}` });
      g.append(svg("rect", { x: 0, y: cy - rowH / 2, width, height: rowH, fill: "transparent" }));
      g.append(svg("path", { d, style: `fill:${diverging ? (pos ? "var(--div-pos)" : "var(--div-neg)") : "var(--series-1)"}` }));
      g.append(svg("text", { class: "cat-label", x: labelW, y: cy + 4, "text-anchor": "end", text: r.label.length > 22 ? r.label.slice(0, 21) + "…" : r.label }));
      g.append(svg("text", { class: "bar-label", x: pos ? zeroX + len + 6 : zeroX - len - 6, y: cy + 4, "text-anchor": pos ? "start" : "end", text: format(r.value) }));
      const showRow = (cx, cyy) => showTip(cx, cyy, r.label, [{ value: format(r.value), label: label }, ...(r.detail ? [{ value: r.detail, label: "players" }] : [])]);
      g.addEventListener("pointermove", (ev) => showRow(ev.clientX, ev.clientY));
      g.addEventListener("pointerleave", hideTip);
      g.addEventListener("focus", () => { const b = g.getBoundingClientRect(); showRow(b.left + b.width / 2, b.top); });
      g.addEventListener("blur", hideTip);
      root.append(g);
    });
    container.append(root);
  }

  // ---------- state & filters ----------
  const state = {
    window: store.get("window", "7d"),
    range: store.get("range", 30),
    playersMode: store.get("playersMode", "abs"),
    genresMode: store.get("genresMode", "abs"),
    moversKind: "gainers",
    importance: new Set(store.get("importance", ["high", "medium"])),
    source: "all",
    eventGroup: "all",
  };
  const playerSel = selection("players");
  const genreSel = selection("genres");

  function segmented(rootSel, current, onChange) {
    const root = $(rootSel);
    const buttons = [...root.querySelectorAll("button[data-value]")];
    const paint = (val) => buttons.forEach((b) => b.setAttribute("aria-checked", String(b.dataset.value === String(val))));
    paint(current);
    buttons.forEach((b) => b.addEventListener("click", () => { paint(b.dataset.value); onChange(b.dataset.value); }));
  }

  // ---------- sections ----------
  function renderHeader() {
    const s = data.summary;
    const when = s.generated_at ? new Date(s.generated_at) : null;
    $("#updated").textContent = `Data for ${fmtDate(s.day, true)}` +
      (when ? ` · updated ${when.toUTCString().slice(17, 22)} UTC` : "") +
      ` · ${s.history_days} day${s.history_days === 1 ? "" : "s"} of history`;
    const notes = [];
    if ((s.history_days || 0) < 2) notes.push("This is the first snapshot. Growth comparisons appear after the next daily run (24h), then after 7 and 30 days.");
    const bad = (s.health || []).filter((h) => h.status === "fail");
    if (bad.length) notes.push(`${bad.length} data source${bad.length > 1 ? "s" : ""} failed on the last run: ${bad.map((b) => b.label).join(", ")}. See Data sources below.`);
    const box = $("#notice");
    box.hidden = !notes.length;
    box.replaceChildren(...notes.map((t) => el("div", { text: t })));
  }

  function tile(label, value, { delta, deltaLabel, spark, sub } = {}) {
    const foot = el("div", { class: "tile-foot" });
    if (delta !== undefined && delta !== null) {
      const cls = Math.abs(delta) < 0.05 ? "flat" : delta > 0 ? "up" : "down";
      const arrow = cls === "up" ? "▲ " : cls === "down" ? "▼ " : "";
      foot.append(el("span", { class: `delta ${cls}`, text: `${arrow}${fmtPct(delta)} ${deltaLabel || ""}`.trim() }));
    } else if (sub) foot.append(el("span", { class: "muted small", text: sub }));
    if (spark) foot.append(sparkline(spark, 80, 22));
    return el("div", { class: "tile" }, el("div", { class: "tile-label", text: label }), el("div", { class: "tile-value", text: value }), foot);
  }

  function renderKpis() {
    const k = data.summary.kpis || {};
    const delta = pctChange(k.total_ccu_now_common, k.total_ccu_prev);
    $("#kpis").replaceChildren(
      tile("Players in tracked games", fmtNum(k.total_ccu), { delta, deltaLabel: "vs yesterday", spark: k.total_ccu_series, sub: "first snapshot" }),
      tile("Games tracked", fmtNum(k.tracked_games), { sub: "Charts + your studios" }),
      tile("New breakouts", fmtNum(k.breakouts), { sub: "under 60 days old" }),
      tile("Developer moves", fmtNum(k.dev_moves_7d), { sub: `last 7 days · ${k.developers || 0} studios` }),
      tile("Platform changes", fmtNum(k.platform_7d), { sub: `last 7 days · ${k.platform_high_7d || 0} high` }),
    );
  }

  function renderBrief() {
    const r = data.summary.report || {};
    $("#brief-date").textContent = r.date ? fmtDate(r.date, true) : "";
    const fill = (sel, items, empty) => {
      const list = $(sel);
      list.replaceChildren(...(items.length ? items : [el("li", { class: "muted", text: empty })]));
    };
    fill("#brief-trends", (r.highlights || []).map((h) => el("li", {}, renderInline(h.text))), "No trend data in the last run.");
    $("#brief-note").textContent = r.history_note || "";
    fill("#brief-devs", (r.developer_moves || []).map((m) => el("li", { text: m.text })), "No notable moves by tracked studios.");
    fill("#brief-platform", ((r.platform || {}).featured || []).map((p) => el("li", {},
      el("span", { class: "badge" }, el("span", { class: `dot ${p.importance}` }), cap(p.importance)), " ",
      el("a", { href: p.url, text: p.title }), el("div", { class: "muted small", text: p.summary }))), "No high-importance changes.");
  }
  const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : "");

  function windowChange(values, dates) {
    const bi = baseIndex(dates, state.window);
    const now = values[values.length - 1];
    return bi < 0 ? null : pctChange(now, values[bi]);
  }

  function gameCell(g) {
    const img = safeUrl(g.icon)
      ? el("img", { src: g.icon, alt: "", loading: "lazy", width: 28, height: 28,
        onerror: (ev) => ev.target.replaceWith(el("span", { class: "icon-fallback" })) })
      : el("span", { class: "icon-fallback" });
    return el("div", { class: "game-cell" }, img,
      el("div", {}, g.url ? el("a", { href: g.url, text: g.name }) : el("span", { text: g.name }),
        el("span", { class: "sub", text: [g.creator, g.subgenre].filter(Boolean).join(" · ") })));
  }

  function renderPlayers() {
    const { dates, games } = data.games;
    const start = rangeIndex(dates);
    const slice = dates.slice(start);
    const byId = new Map(games.map((g) => [String(g.id), g]));
    const series = playerSel.ids().filter((id) => byId.has(id)).map((id) => {
      const g = byId.get(id);
      return { name: g.name, color: colorOf(playerSel.slot(id)), values: g.ccu.slice(start) };
    });
    lineChart($("#players-chart"), { dates: slice, series, mode: state.playersMode, valueLabel: "Players",
      empty: series.length ? "No snapshots yet." : "Tick a game in the table below to plot it." });
    renderGamesTable();
  }

  function renderGamesTable() {
    const { dates, games } = data.games;
    const start = rangeIndex(dates);
    const top = games.filter((g) => g.top).sort((a, b) => (b.ccu[b.ccu.length - 1] || 0) - (a.ccu[a.ccu.length - 1] || 0));
    const bi = baseIndex(dates, state.window);
    const table = $("#games-table");
    table.replaceChildren(el("thead", {}, el("tr", {},
      el("th", { text: "Plot" }), el("th", { text: "Game" }), el("th", { text: "Genre" }),
      el("th", { class: "num", text: "Players" }), el("th", { class: "num", text: `Change ${state.window}` }),
      el("th", { class: "num", text: `Visits/day ${state.window}` }), el("th", { class: "num", text: "Rating" }), el("th", { text: "Trend" }))));
    const body = el("tbody");
    for (const g of top) {
      const id = String(g.id);
      const now = g.ccu[g.ccu.length - 1];
      const change = windowChange(g.ccu, dates);
      let vpd = null;
      if (bi >= 0 && g.visits[bi] !== null && g.visits[g.visits.length - 1] !== null) {
        vpd = (g.visits[g.visits.length - 1] - g.visits[bi]) / Math.max(1, dayNum(dates[dates.length - 1]) - dayNum(dates[bi]));
      }
      const box = el("input", { type: "checkbox", "aria-label": `Plot ${g.name}` });
      box.checked = playerSel.has(id);
      box.addEventListener("change", () => {
        if (!playerSel.toggle(id)) { box.checked = false; flash(`Up to ${SLOTS} games at once: untick one first.`); return; }
        renderPlayers();
      });
      const swatch = playerSel.has(id) ? el("span", { class: "swatch", style: `background:${colorOf(playerSel.slot(id))}` }) : null;
      body.append(el("tr", {},
        el("td", {}, el("label", { class: "plot-toggle" }, box, swatch)),
        el("td", {}, gameCell(g)), el("td", { text: g.genre }),
        el("td", { class: "num", text: fmtNum(now) }), deltaCell(change),
        el("td", { class: "num", text: vpd === null ? "–" : fmtNum(vpd) }),
        el("td", { class: "num", text: g.rating === null || g.rating === undefined ? "–" : `${g.rating}%` }),
        el("td", {}, sparkline(g.ccu.slice(start)))));
    }
    table.append(body);
  }

  function deltaCell(p) {
    if (p === null || p === undefined) return el("td", { class: "num muted", text: "–" });
    const cls = Math.abs(p) < 0.05 ? "flat" : p > 0 ? "up" : "down";
    return el("td", { class: `num delta ${cls}`, text: (cls === "up" ? "▲ " : cls === "down" ? "▼ " : "") + fmtPct(p) });
  }

  let flashTimer;
  function flash(text) {
    const box = $("#notice");
    box.hidden = false;
    box.replaceChildren(el("div", { text }));
    clearTimeout(flashTimer);
    flashTimer = setTimeout(renderHeader, 3500);
  }

  function renderMovers() {
    const m = data.movers || {};
    const win = (m.windows || {})[state.window] || {};
    const table = $("#movers-table");
    const sub = $("#movers-sub");
    if (!m.available || !win.available) {
      const need = WINDOW_DAYS[state.window][0];
      sub.textContent = `Needs a snapshot from ${need} day${need > 1 ? "s" : ""} ago; history so far: ${data.summary.history_days || 0} day(s).`;
      table.replaceChildren();
      return;
    }
    sub.textContent = `${fmtDate(win.base_day)} → ${fmtDate(m.date)} · ${win.compared_games} games compared · min. 1k players for % rankings`;
    const rows = win[state.moversKind] || [];
    table.replaceChildren(el("thead", {}, el("tr", {}, el("th", { text: "#" }), el("th", { text: "Game" }), el("th", { text: "Genre" }),
      el("th", { class: "num", text: "Players" }), el("th", { class: "num", text: "Change" }), el("th", { class: "num", text: "Visits/day" }))));
    const body = el("tbody");
    rows.forEach((r, i) => {
      const g = { name: r.name, url: r.place_id ? `https://www.roblox.com/games/${r.place_id}` : "", creator: r.creator,
        icon: (data.games.games.find((x) => x.id === r.universe_id) || {}).icon };
      const change = state.moversKind === "abs_gainers" ? null : r.ccu_pct;
      body.append(el("tr", {}, el("td", { class: "num", text: String(i + 1) }), el("td", {}, gameCell(g)), el("td", { text: r.genre }),
        el("td", { class: "num", text: `${fmtNum(r.ccu_then)} → ${fmtNum(r.ccu_now)}` }),
        state.moversKind === "abs_gainers" ? el("td", { class: "num", text: (r.ccu_delta > 0 ? "+" : "") + fmtNum(r.ccu_delta) }) : deltaCell(change),
        el("td", { class: "num", text: fmtNum(r.visits_per_day) })));
    });
    if (!rows.length) body.append(el("tr", {}, el("td", { colspan: 6, class: "muted", text: "Nothing qualifies in this window." })));
    table.append(body);
  }

  function renderBreakouts() {
    const rows = (data.movers || {}).breakouts || [];
    const table = $("#breakouts-table");
    table.replaceChildren(el("thead", {}, el("tr", {}, el("th", { text: "Game" }), el("th", { text: "Genre" }),
      el("th", { class: "num", text: "Age" }), el("th", { class: "num", text: "Players" }), el("th", { class: "num", text: "Visits/day" }), el("th", { text: "Charts" }))));
    const body = el("tbody");
    for (const r of rows) {
      const g = { name: r.name, url: r.place_id ? `https://www.roblox.com/games/${r.place_id}` : "", creator: r.creator,
        icon: (data.games.games.find((x) => x.id === r.universe_id) || {}).icon };
      body.append(el("tr", {}, el("td", {}, gameCell(g)), el("td", { text: r.genre }), el("td", { class: "num", text: `${r.age_days}d` }),
        el("td", { class: "num", text: fmtNum(r.ccu_now) }), el("td", { class: "num", text: fmtNum(r.visits_per_day) }),
        el("td", { class: "small", text: (r.charts || []).join(", ") || "–" })));
    }
    if (!rows.length) body.append(el("tr", {}, el("td", { colspan: 6, class: "muted", text: "No new games above the breakout threshold today." })));
    table.append(body);
  }

  function renderGenres() {
    const { dates, genres } = data.genres;
    const start = rangeIndex(dates);
    const m = data.movers || {};
    const changes = ((m.genres || {})[state.window]) || [];
    if (changes.length) {
      $("#momentum-title").textContent = `Momentum · ${state.window}`;
      $("#momentum-sub").textContent = "Player change per genre, same games on both days.";
      barChart($("#momentum-chart"), { rows: changes.slice().sort((a, b) => b.pct - a.pct).slice(0, 14).map((r) => ({ label: r.genre, value: r.pct, detail: `${fmtNum(r.ccu_then)} → ${fmtNum(r.ccu_now)}` })),
        format: (v) => fmtPct(v), diverging: true, tableId: "genres-chart", label: `Change ${state.window}` });
    } else {
      $("#momentum-title").textContent = "Share of tracked players";
      $("#momentum-sub").textContent = `Genre momentum (${state.window}) appears once there is enough history; until then, today's share.`;
      const now = (m.genre_now || []).slice(0, 12);
      barChart($("#momentum-chart"), { rows: now.map((r) => ({ label: r.genre, value: r.share, detail: fmtNum(r.ccu) })),
        format: (v) => `${v.toFixed(1)}%`, diverging: false, tableId: "genres-chart", label: "Share of players" });
    }
    const byName = new Map(genres.map((g) => [g.genre, g]));
    const series = genreSel.ids().filter((id) => byName.has(id)).map((id) => ({
      name: id, color: colorOf(genreSel.slot(id)), values: byName.get(id).ccu.slice(start) }));
    lineChart($("#genres-chart"), { dates: dates.slice(start), series, mode: state.genresMode, valueLabel: "Players by genre",
      empty: "Tick a genre in the table below to plot it." });

    const table = $("#genres-table");
    table.replaceChildren(el("thead", {}, el("tr", {}, el("th", { text: "Plot" }), el("th", { text: "Genre" }),
      el("th", { class: "num", text: "Players" }), el("th", { class: "num", text: "Share" }), el("th", { class: "num", text: `Change ${state.window}` }),
      el("th", { class: "num", text: "Games" }), el("th", { text: "Trend" }))));
    const body = el("tbody");
    const total = genres.reduce((a, g) => a + (g.ccu[g.ccu.length - 1] || 0), 0) || 1;
    const changeByGenre = new Map(changes.map((c) => [c.genre, c.pct]));
    for (const g of genres) {
      const now = g.ccu[g.ccu.length - 1] || 0;
      const box = el("input", { type: "checkbox", "aria-label": `Plot ${g.genre}` });
      box.checked = genreSel.has(g.genre);
      box.addEventListener("change", () => {
        if (!genreSel.toggle(g.genre)) { box.checked = false; flash(`Up to ${SLOTS} genres at once: untick one first.`); return; }
        renderGenres();
      });
      body.append(el("tr", {},
        el("td", {}, el("label", { class: "plot-toggle" }, box, genreSel.has(g.genre) ? el("span", { class: "swatch", style: `background:${colorOf(genreSel.slot(g.genre))}` }) : null)),
        el("td", { text: g.genre }), el("td", { class: "num", text: fmtNum(now) }),
        el("td", { class: "num", text: `${((now / total) * 100).toFixed(1)}%` }),
        deltaCell(changeByGenre.has(g.genre) ? changeByGenre.get(g.genre) : null),
        el("td", { class: "num", text: String(g.games[g.games.length - 1] || 0) }),
        el("td", {}, sparkline(g.ccu.slice(start)))));
    }
    table.append(body);
  }

  const EVENT_GROUPS = {
    all: { label: "All", types: null },
    releases: { label: "Releases", types: ["new_game", "game_listed"] },
    updates: { label: "Updates", types: ["game_updated", "update_impact", "renamed"] },
    players: { label: "Player swings", types: ["player_surge", "player_drop"] },
    owners: { label: "Ownership", types: ["ownership_change"] },
  };
  const EVENT_LABEL = { new_game: "New game", game_listed: "Made public", game_updated: "Update", update_impact: "Update impact",
    renamed: "Renamed", player_surge: "Players up", player_drop: "Players down", ownership_change: "Owner change" };

  function renderDevelopers() {
    const d = data.developers;
    const start = rangeIndex(data.games.dates);
    const table = $("#devs-table");
    table.replaceChildren(el("thead", {}, el("tr", {}, el("th", { text: "Developer" }), el("th", { class: "num", text: "Games" }),
      el("th", { class: "num", text: "Players" }), el("th", { class: "num", text: "Change 7d" }), el("th", { text: "Trend" }), el("th", { text: "Top game" }))));
    const body = el("tbody");
    for (const r of d.developers) {
      body.append(el("tr", {},
        el("td", {}, el("a", { href: r.url, text: r.name }), el("span", { class: "sub muted small", text: ` ${r.kind === "group" ? "group" : "user"}` })),
        el("td", { class: "num", text: String(r.games) }), el("td", { class: "num", text: fmtNum(r.ccu) }), deltaCell(r.ccu_7d_pct),
        el("td", {}, sparkline((r.series || []).slice(start))),
        el("td", {}, r.top_game ? el("a", { href: r.top_game.url, text: `${r.top_game.name}` }) : "–",
          r.top_game ? el("span", { class: "muted small", text: ` ${fmtNum(r.top_game.ccu)}` }) : null)));
    }
    if (!d.developers.length) body.append(el("tr", {}, el("td", { colspan: 6, class: "muted", text: "No developers resolved yet. Check config/developers.yml and the Data sources panel." })));
    table.append(body);

    const chips = $("#event-filter");
    chips.replaceChildren(...Object.entries(EVENT_GROUPS).map(([key, g]) => el("button", {
      type: "button", class: "chip", "aria-pressed": String(state.eventGroup === key), text: g.label,
      onclick: () => { state.eventGroup = key; renderDevelopers(); } })));
    renderEvents();
  }

  function renderEvents() {
    const group = EVENT_GROUPS[state.eventGroup];
    const cutoff = data.games.dates.length ? dayNum(data.games.dates[data.games.dates.length - 1]) - state.range : -Infinity;
    const events = data.developers.events.filter((e) => (!group.types || group.types.includes(e.type)) && dayNum(e.date) > cutoff);
    const log = $("#events-log");
    const shown = events.slice(0, 80);
    log.replaceChildren(...shown.map((e) => el("li", {}, el("span", { class: "when", text: fmtDate(e.date, true) }),
      el("div", { class: "what" }, el("div", { class: "meta" }, el("span", { class: "badge", text: EVENT_LABEL[e.type] || e.type })),
        el("div", { text: e.text })))));
    if (!events.length) log.append(el("li", { class: "muted" }, el("span", { class: "when" }), el("span", { text: "No developer moves in this range yet." })));
    if (events.length > shown.length) log.append(el("li", { class: "muted small" }, el("span", { class: "when" }), el("span", { text: `${events.length - shown.length} older moves not shown; narrow the range or filter.` })));
  }

  function renderPlatform() {
    const items = data.platform.items || [];
    const chips = $("#importance-filter");
    chips.replaceChildren(...["high", "medium", "low"].map((lvl) => el("button", {
      type: "button", class: "chip", "aria-pressed": String(state.importance.has(lvl)),
      onclick: () => { state.importance.has(lvl) ? state.importance.delete(lvl) : state.importance.add(lvl); store.set("importance", [...state.importance]); renderPlatform(); } },
      el("span", { class: "badge" }, el("span", { class: `dot ${lvl}` }), cap(lvl)), ` ${items.filter((i) => i.importance === lvl).length}`)));
    const select = $("#source-filter");
    if (!select.options.length) {
      const sources = [...new Map(items.map((i) => [i.source, i.source_label])).entries()];
      select.append(el("option", { value: "all", text: "All sources" }), ...sources.map(([id, label]) => el("option", { value: id, text: label })));
      select.addEventListener("change", () => { state.source = select.value; renderPlatform(); });
    }
    const cutoff = data.games.dates.length ? dayNum(data.games.dates[data.games.dates.length - 1]) - state.range : -Infinity;
    const shown = items.filter((i) => state.importance.has(i.importance) && (state.source === "all" || i.source === state.source)
      && dayNum((i.published || i.found || "1970-01-01").slice(0, 10)) > cutoff);
    const log = $("#platform-log");
    log.replaceChildren(...shown.slice(0, 120).map((i) => el("li", {},
      el("span", { class: "when", text: fmtDate(i.published || i.found, true) }),
      el("div", { class: "what" },
        el("div", { class: "meta" }, el("span", { class: "badge" }, el("span", { class: `dot ${i.importance}` }), cap(i.importance)),
          el("span", { text: i.source_label }), ...(i.tags || []).map((t) => el("span", { class: "tag", text: t }))),
        el("a", { class: "title", href: i.url, text: i.title }),
        el("p", { class: "summary", text: i.summary }),
        el("div", { class: "muted small", text: [i.why ? `Why: ${i.why}` : "", i.method && i.method.startsWith("github-models") ? "AI summary" : "keyword summary"].filter(Boolean).join(" · ") })))));
    if (!shown.length) log.append(el("li", { class: "muted" }, el("span", { class: "when" }), el("span", { text: "Nothing matches these filters in this range." })));
  }

  function renderHealth() {
    const list = $("#health-list");
    const rows = data.summary.health || [];
    $("#health-date").textContent = data.summary.day ? `last run ${fmtDate(data.summary.day, true)}` : "";
    const label = { ok: "OK", warn: "Warning", fail: "Failed" };
    list.replaceChildren(...rows.map((h) => el("li", {},
      el("span", { class: "badge" }, el("span", { class: `dot ${h.status}` }), label[h.status] || h.status),
      el("div", {}, el("strong", { text: h.label }), el("div", { class: "detail", text: h.detail + (h.fallback ? ` · fallback: ${h.fallback}` : "") })))));
    if (!rows.length) list.append(el("li", { class: "muted", text: "No run recorded yet." }));
  }

  function renderSuggestions() {
    const rows = data.developers.suggestions || [];
    const yaml = rows.map((r) => `  - ${r.kind}: ${r.id}\n    name: ${JSON.stringify(r.name)}  # top game: ${r.top_game} (${fmtNum(r.top_ccu)} playing)`).join("\n");
    $("#suggest-yaml").textContent = yaml || "Every owner of today's top games is already tracked.";
    $("#copy-suggest").onclick = async () => {
      try { await navigator.clipboard.writeText(yaml); flash("Copied. Paste under `developers:` in config/developers.yml."); }
      catch { flash("Copy failed: select the text and copy it manually."); }
    };
  }

  function renderAll() {
    renderHeader(); renderKpis(); renderBrief(); renderPlayers(); renderMovers(); renderBreakouts();
    renderGenres(); renderDevelopers(); renderPlatform(); renderHealth(); renderSuggestions();
  }

  // ---------- theme ----------
  function setupTheme() {
    const btn = $("#theme-toggle");
    const order = ["auto", "light", "dark"];
    let theme = store.get("theme", "auto");
    const apply = () => {
      if (theme === "auto") delete document.documentElement.dataset.theme;
      else document.documentElement.dataset.theme = theme;
      btn.textContent = { auto: "◐ Auto", light: "☀ Light", dark: "☾ Dark" }[theme];
      btn.setAttribute("aria-label", `Color theme: ${theme}`);
    };
    apply();
    btn.addEventListener("click", () => { theme = order[(order.indexOf(theme) + 1) % order.length]; store.set("theme", theme); apply(); renderAll(); });
  }

  // ---------- boot ----------
  async function load(name) {
    const resp = await fetch(`data/${name}.json`, { cache: "no-cache" });
    if (!resp.ok) throw new Error(`${name}.json: HTTP ${resp.status}`);
    return resp.json();
  }

  async function boot() {
    setupTheme();
    try {
      [data.summary, data.games, data.genres, data.movers, data.developers, data.platform] = await Promise.all(
        ["summary", "games", "genres", "movers", "developers", "platform"].map(load));
    } catch (err) {
      $("#updated").textContent = "No data yet";
      const box = $("#notice");
      box.hidden = false;
      box.replaceChildren(el("div", { text: `Could not load dashboard data (${err.message}). It appears after the first successful daily run.` }));
      return;
    }
    const latest = (g) => g.ccu[g.ccu.length - 1] || 0;
    playerSel.init(new Set(data.games.games.map((g) => String(g.id))),
      data.games.games.filter((g) => g.top).sort((a, b) => latest(b) - latest(a)).slice(0, 5).map((g) => g.id));
    genreSel.init(new Set(data.genres.genres.map((g) => g.genre)), data.genres.genres.slice(0, 5).map((g) => g.genre));

    segmented("#window-filter", state.window, (v) => { state.window = v; store.set("window", v); renderAll(); });
    segmented("#range-filter", state.range, (v) => { state.range = Number(v); store.set("range", state.range); renderAll(); });
    segmented("#players-mode", state.playersMode, (v) => { state.playersMode = v; store.set("playersMode", v); renderPlayers(); });
    segmented("#genres-mode", state.genresMode, (v) => { state.genresMode = v; store.set("genresMode", v); renderGenres(); });
    segmented("#movers-kind", state.moversKind, (v) => { state.moversKind = v; renderMovers(); });
    document.querySelectorAll("[data-table-toggle]").forEach((btn) => btn.addEventListener("click", () => {
      const id = btn.dataset.tableToggle;
      tableViews.has(id) ? tableViews.delete(id) : tableViews.add(id);
      btn.textContent = tableViews.has(id) ? "Chart view" : "Table view";
      if (id === "players-chart") renderPlayers(); else renderGenres();
    }));
    let resizeTimer;
    window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { renderPlayers(); renderGenres(); }, 150); });
    renderAll();
  }
  boot();
})();
