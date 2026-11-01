// Ask Your Database frontend: AI questions, results table and chart, and the SQL dashboard.

const $ = (id) => document.getElementById(id);
const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "EUR", maximumFractionDigits: 0 });
const moneyCompact = new Intl.NumberFormat("en-US", { style: "currency", currency: "EUR", notation: "compact", maximumFractionDigits: 1 });
const number = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });

// GSAP is optional: without the CDN (or with reduced motion) the UI simply appears without animation.
const motion = typeof gsap !== "undefined" && !matchMedia("(prefers-reduced-motion: reduce)").matches;
const scrollMotion = motion && typeof ScrollTrigger !== "undefined";
if (scrollMotion) gsap.registerPlugin(ScrollTrigger);

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  if (response.status === 401) {
    // The platform's sign-in page; it comes back here afterwards.
    location.href = "/.auth/login/aad?post_login_redirect_uri=/";
    throw new Error("Sign in required.");
  }
  let data;
  try { data = await response.json(); } catch { data = {}; }
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text; // textContent: data can never inject HTML
  return node;
}

// ---------- Theme ----------

function storedTheme() {
  try { return localStorage.getItem("theme"); } catch { return null; }
}

function setTheme(theme, save) {
  document.documentElement.setAttribute("data-theme", theme);
  if (save) { try { localStorage.setItem("theme", theme); } catch { /* private mode: not saved */ } }
  rerenderCharts();
}

$("theme-toggle").addEventListener("click", () => {
  const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
  setTheme(next, true);
  if (motion) gsap.fromTo("#theme-toggle svg", { rotate: -90, scale: 0.6 }, { rotate: 0, scale: 1, duration: 0.45, ease: "back.out(2)" });
});

// Follow the OS setting until the visitor picks a theme (and unless ?theme= forces one).
if (!new URLSearchParams(location.search).get("theme")) {
  matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", (e) => {
    if (!storedTheme()) setTheme(e.matches ? "dark" : "light", false);
  });
}

// ---------- Formatting and tables ----------

function formatCell(value, column) {
  if (value === null) return "–";
  if (typeof value === "number") {
    if (/eur|revenue|amount|price|value|spent|profit/i.test(column)) return money.format(value);
    if (/pct|percent|rate|share/i.test(column)) return `${number.format(value)}%`;
    return number.format(value);
  }
  return String(value);
}

function renderTable(container, columns, rows, limit = 100) {
  const numeric = columns.map((_, i) => rows.length > 0 && rows.some((r) => typeof r[i] === "number"));
  const isId = columns.map((c) => /(^id$|_id$)/i.test(c));
  const table = el("table");
  const head = el("tr");
  columns.forEach((c, i) => head.append(el("th", numeric[i] ? "num" : "", c.replace(/_/g, " "))));
  const thead = el("thead");
  thead.append(head);
  table.append(thead);
  const body = el("tbody");
  rows.slice(0, limit).forEach((row) => {
    const tr = el("tr");
    row.forEach((v, i) => {
      const cls = [typeof v === "number" ? "num" : "", isId[i] ? "mono" : ""].filter(Boolean).join(" ");
      tr.append(el("td", cls, formatCell(v, columns[i])));
    });
    body.append(tr);
  });
  table.append(body);
  container.replaceChildren(table);
  if (rows.length > limit) container.append(el("p", "muted", `Showing ${limit} of ${rows.length} rows.`));
  if (motion) gsap.from([...body.children].slice(0, 25), { opacity: 0, y: 8, duration: 0.35, stagger: 0.03, ease: "power2.out" });
}

// Counts a number up from its previous value while showing it in its final format.
function countUp(node, value, format) {
  const from = Number(node.dataset.value || 0);
  node.dataset.value = value;
  node.textContent = format(value);
  if (!motion) return;
  const state = { value: from };
  node.textContent = format(from);
  gsap.to(state, { value, duration: 0.9, ease: "power2.out", onUpdate: () => { node.textContent = format(state.value); } });
}

// ---------- Charts (colours come from the CSS theme tokens) ----------

const charts = new Map(); // canvas -> { spec, chart }

function themeColors() {
  const css = getComputedStyle(document.documentElement);
  const v = (name) => css.getPropertyValue(name).trim();
  const accent = v("--accent"), accent2 = v("--accent-2");
  return {
    accent, accent2,
    text: v("--text"), text2: v("--text-2"), text3: v("--text-3"),
    border: v("--border"), borderStrong: v("--border-strong"), surface: v("--surface-strong"),
    font: v("--font") || "system-ui",
    palette: [accent, accent2, "#8b5cf6", "#f59e0b", "#10b981", "#ec4899", "#64748b", "#f43f5e"],
  };
}

function alpha(hex, a) {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex);
  if (!m) return hex;
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
}

function buildChart(canvas, { type, labels, values, label, horizontal }) {
  const c = themeColors();
  const isMoney = /eur|revenue|amount|price|spent|profit/i.test(label || "");
  const isPct = /pct|percent|rate|share/i.test(label || "");
  const tick = (value) => (isMoney ? moneyCompact.format(value) : `${number.format(value)}${isPct ? "%" : ""}`);
  const doughnut = type === "pie";
  const horizontalBars = type === "bar" && (horizontal ?? labels.length > 6);
  const valueAxis = horizontalBars ? "x" : "y";
  const narrow = window.innerWidth < 560;

  Chart.defaults.font.family = c.font;
  Chart.defaults.color = c.text2;

  const dataset = { label, data: values, borderWidth: 0 };
  if (type === "line") {
    Object.assign(dataset, {
      borderColor: c.accent, borderWidth: 2.5, tension: 0.35, fill: true,
      pointRadius: 0, pointHoverRadius: 5, pointBackgroundColor: c.accent, pointBorderColor: c.surface, pointBorderWidth: 2,
      backgroundColor: (ctx) => {
        const area = ctx.chart.chartArea;
        if (!area) return alpha(c.accent, 0.15);
        const g = ctx.chart.ctx.createLinearGradient(0, area.top, 0, area.bottom);
        g.addColorStop(0, alpha(c.accent, 0.32));
        g.addColorStop(1, alpha(c.accent2, 0.0));
        return g;
      },
    });
  } else if (type === "bar") {
    Object.assign(dataset, {
      borderRadius: 6, borderSkipped: false, maxBarThickness: 34,
      backgroundColor: (ctx) => {
        const area = ctx.chart.chartArea;
        if (!area) return c.accent;
        const g = horizontalBars
          ? ctx.chart.ctx.createLinearGradient(area.left, 0, area.right, 0)
          : ctx.chart.ctx.createLinearGradient(0, area.bottom, 0, area.top);
        g.addColorStop(0, c.accent);
        g.addColorStop(1, c.accent2);
        return g;
      },
      hoverBackgroundColor: c.accent2,
    });
  } else {
    Object.assign(dataset, { backgroundColor: c.palette, borderColor: c.surface, borderWidth: 2, hoverOffset: 6 });
  }

  const axis = (isValue) => ({
    grid: { color: isValue ? c.border : "transparent", drawTicks: false },
    border: { display: false },
    ticks: { color: c.text3, padding: 8, font: { size: 11 }, ...(isValue ? { callback: tick, maxTicksLimit: 6 } : { autoSkipPadding: 14, maxRotation: 0 }) },
  });

  return new Chart(canvas, {
    type: doughnut ? "doughnut" : type,
    data: { labels, datasets: [dataset] },
    options: {
      maintainAspectRatio: false,
      animation: { duration: motion ? 700 : 0, easing: "easeOutQuart" },
      indexAxis: horizontalBars ? "y" : "x",
      cutout: doughnut ? "64%" : undefined,
      interaction: { mode: doughnut ? "nearest" : "index", intersect: doughnut },
      layout: { padding: doughnut ? 4 : 0 },
      plugins: {
        legend: {
          display: doughnut,
          position: narrow ? "bottom" : "right",
          labels: { color: c.text2, usePointStyle: true, pointStyle: "circle", boxWidth: 8, boxHeight: 8, padding: 12, font: { size: 12 } },
        },
        tooltip: {
          backgroundColor: c.surface, borderColor: c.borderStrong, borderWidth: 1,
          titleColor: c.text, bodyColor: c.text2, padding: 10, cornerRadius: 10, boxPadding: 4,
          titleFont: { weight: "600" }, usePointStyle: true,
          callbacks: { label: (ctx) => ` ${ctx.dataset.label}: ${formatCell(doughnut ? ctx.parsed : ctx.parsed[valueAxis], label || "")}` },
        },
      },
      scales: doughnut ? {} : { x: axis(valueAxis === "x"), y: axis(valueAxis === "y") },
    },
  });
}

function drawChart(canvas, spec) {
  charts.get(canvas)?.chart?.destroy();
  if (typeof Chart === "undefined") return; // Chart.js CDN unavailable: tables still work
  charts.set(canvas, { spec, chart: buildChart(canvas, spec) });
}

function clearChart(canvas) {
  charts.get(canvas)?.chart?.destroy();
  charts.delete(canvas);
}

function rerenderCharts() {
  for (const [canvas, entry] of charts) {
    entry.chart?.destroy();
    entry.chart = buildChart(canvas, entry.spec);
  }
}

let resizeTimer;
let lastNarrow = window.innerWidth < 560;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => {
    const narrow = window.innerWidth < 560;
    if (narrow !== lastNarrow) { lastNarrow = narrow; rerenderCharts(); } // legend position changes
  }, 200);
});

// ---------- Ask ----------

function setBusy(busy) {
  const btn = $("ask-btn");
  btn.disabled = busy;
  btn.replaceChildren();
  if (busy) btn.append(el("span", "spinner"), document.createTextNode("Thinking…"));
  else btn.textContent = "Ask";
}

async function ask(question) {
  $("question").value = question;
  $("ask-error").textContent = "";
  setBusy(true);
  $("answer").hidden = true;
  $("thinking").hidden = false;
  if (motion) gsap.fromTo("#thinking", { opacity: 0, y: 8 }, { opacity: 1, y: 0, duration: 0.3, ease: "power2.out" });
  try {
    const data = await api("/api/ask", { method: "POST", body: JSON.stringify({ question }) });
    $("thinking").hidden = true;
    showAnswer(data);
  } catch (err) {
    $("thinking").hidden = true;
    $("ask-error").textContent = err.message;
    if (motion) gsap.fromTo("#ask-error", { x: -6 }, { x: 0, duration: 0.6, ease: "elastic.out(1, 0.3)" });
  } finally {
    setBusy(false);
    loadHistory();
    loadMe();
  }
}

function showAnswer(data) {
  $("answer").hidden = false;
  setExport(data.id);
  $("answer-text").textContent = data.answer;
  $("sql-explanation").textContent = data.explanation;
  $("sql-editor").value = data.sql;
  const fixed = data.attempts > 1 ? ` · AI fixed its own query ${data.attempts - 1}×` : "";
  $("sql-meta").textContent = `· ${data.rows.length}${data.truncated ? "+" : ""} rows · ${data.elapsed_ms} ms${fixed}`;
  $("sql-error").textContent = "";
  showResult(data.columns, data.rows, data.chart);
  if (motion) {
    gsap.from(["#answer .answer-head", "#answer .answer-grid", "#answer .sql-details"],
      { opacity: 0, y: 14, duration: 0.5, stagger: 0.1, ease: "power3.out" });
    pulse(".ask-panel");
  }
}

// A short accent glow that says "done".
function pulse(selector) {
  gsap.fromTo(selector,
    { boxShadow: "0 0 0 0px color-mix(in srgb, var(--accent) 45%, transparent)" },
    { boxShadow: "0 0 0 8px color-mix(in srgb, var(--accent) 0%, transparent)", duration: 0.8, ease: "power2.out", clearProps: "boxShadow" });
}

function showResult(columns, rows, chart) {
  renderTable($("answer-table"), columns, rows);
  const canvas = $("answer-chart");
  clearChart(canvas);
  const box = $("answer-chart-box");
  const xi = columns.indexOf(chart?.x), yi = columns.indexOf(chart?.y);
  if (chart && chart.type !== "none" && xi >= 0 && yi >= 0 && rows.length > 1 && typeof Chart !== "undefined") {
    box.hidden = false;
    const data = chart.type === "line" ? rows : rows.slice(0, chart.type === "pie" ? 8 : 15);
    drawChart(canvas, { type: chart.type, labels: data.map((r) => r[xi]), values: data.map((r) => r[yi]), label: chart.y });
  } else {
    box.hidden = true;
  }
}

$("ask-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const q = $("question").value.trim();
  if (q) ask(q);
});
$("examples").addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (chip && !$("ask-btn").disabled) ask(chip.textContent);
});
$("run-sql").addEventListener("click", async () => {
  $("sql-error").textContent = "";
  const btn = $("run-sql");
  btn.disabled = true;
  try {
    const data = await api("/api/query", { method: "POST", body: JSON.stringify({ sql: $("sql-editor").value }) });
    $("answer-text").textContent = "Result of your edited query:";
    setExport(data.id);
    $("sql-meta").textContent = `· ${data.rows.length}${data.truncated ? "+" : ""} rows · ${data.elapsed_ms} ms · edited`;
    showResult(data.columns, data.rows, null);
    if (motion) pulse(".ask-panel");
  } catch (err) {
    $("sql-error").textContent = err.message;
    if (motion) gsap.fromTo(".editor", { x: -6 }, { x: 0, duration: 0.6, ease: "elastic.out(1, 0.3)" });
  } finally {
    btn.disabled = false;
    loadHistory();
  }
});

function setExport(id) {
  const link = $("export-csv");
  link.hidden = !id;
  if (id) link.href = `/api/questions/${id}/export.csv`;
}

// ---------- Account and history ----------

let me = null;
let showAll = false;

async function loadMe() {
  try {
    me = await api("/api/me");
  } catch {
    return;
  }
  const user = $("user");
  user.hidden = false;
  user.textContent = `${me.auth === "none" ? "local" : me.name} · $${me.spent_today_usd.toFixed(2)} / $${me.daily_budget_usd.toFixed(2)} today`;
  user.title = me.database;
  $("audit-toggle").hidden = !me.admin || me.auth === "none";
  if (!me.database.startsWith("SQLite")) $("footnote").textContent = `Connected to ${me.database}.`;
}

const STATUS = { ok: "✓", error: "error", blocked: "blocked" };

async function loadHistory() {
  let rows;
  try {
    rows = await api(`/api/history${showAll ? "?all=1" : ""}`);
  } catch (err) {
    $("history-table").replaceChildren(el("p", "error", err.message));
    return;
  }
  $("history-title").textContent = showAll ? "Audit log: everyone's questions" : "Your recent questions";
  if (!rows.length) {
    $("history-table").replaceChildren(el("p", "muted", "No questions yet."));
    return;
  }
  const table = el("table");
  const head = el("tr");
  const columns = ["When", ...(showAll ? ["Who"] : []), "Question", "Result", "Rows", "Cost", ""];
  columns.forEach((c) => head.append(el("th", c === "Rows" || c === "Cost" ? "num" : "", c)));
  const thead = el("thead");
  thead.append(head);
  const body = el("tbody");
  for (const r of rows) {
    const tr = el("tr");
    tr.append(el("td", "mono", new Date(`${r.created_at.replace(" ", "T")}Z`).toLocaleString()));
    if (showAll) tr.append(el("td", "", r.owner_name));
    const q = el("td", "", r.kind === "sql" ? "(edited SQL)" : r.question);
    q.title = r.sql_text || r.error || "";
    tr.append(q);
    const status = el("td", r.status === "ok" ? "" : "error", STATUS[r.status] || r.status);
    if (r.error) status.title = r.error;
    tr.append(status);
    tr.append(el("td", "num", r.row_count ?? "–"));
    tr.append(el("td", "num", r.cost_usd == null ? "–" : `$${Number(r.cost_usd).toFixed(4)}`));
    const actions = el("td");
    if (r.status === "ok" && r.sql_text) {
      const link = el("a", "sql-toggle", "CSV");
      link.href = `/api/questions/${r.id}/export.csv`;
      link.setAttribute("download", "");
      actions.append(link);
    }
    tr.append(actions);
    body.append(tr);
  }
  table.append(thead, body);
  $("history-table").replaceChildren(table);
}

$("audit-toggle").addEventListener("click", () => {
  showAll = !showAll;
  $("audit-toggle").textContent = showAll ? "Show only mine" : "Show everyone's (audit log)";
  loadHistory();
});

// ---------- Dashboard ----------

async function loadDashboard() {
  let d;
  try {
    d = await api("/api/dashboard");
  } catch (err) {
    $("kpis").replaceChildren(el("p", "error", `Could not load the dashboard: ${err.message}`));
    return;
  }
  if (d.available === false) {
    // Not the sample shop: the hand-written dashboard doesn't apply to this database.
    for (const node of [$("dashboard"), $("kpis"), document.querySelector(".grid")]) node.hidden = true;
    document.querySelector('a[href="#dashboard"]').hidden = true;
    return;
  }

  const k = Object.fromEntries(d.kpis.columns.map((c, i) => [c, d.kpis.rows[0][i]]));
  const count = (v) => number.format(Math.round(v));
  const margin = k.revenue_eur ? (100 * k.gross_profit_eur) / k.revenue_eur : 0;
  const period = d.kpis.title.toLowerCase();
  const cards = [
    ["Revenue", k.revenue_eur, money.format, `${d.kpis.title} · excl. returns`],
    ["Orders", k.orders, count, `Valid orders, ${period}`],
    ["Avg. order", k.avg_order_eur, money.format, "Revenue ÷ orders"],
    ["Customers", k.customers, count, "Unique buyers"],
    ["Gross profit", k.gross_profit_eur, money.format, `${number.format(Math.round(margin * 10) / 10)}% margin`],
  ];
  $("kpis").replaceChildren(...cards.map(([label, value, format, sub]) => {
    const card = el("div", "kpi");
    const valueEl = el("span", "value");
    card.append(el("span", "label", label), valueEl, el("span", "sub", sub));
    countUp(valueEl, value, format);
    return card;
  }));
  if (motion) gsap.from("#kpis .kpi", { opacity: 0, y: 16, duration: 0.5, stagger: 0.06, ease: "power3.out" });

  document.querySelectorAll(".grid .panel").forEach((panel) => {
    const q = d[panel.dataset.key];
    panel.querySelector("h3").textContent = q.title;
    const pre = panel.querySelector(".sql");
    const toggle = panel.querySelector(".sql-toggle");
    pre.textContent = q.sql;
    toggle.setAttribute("aria-expanded", "false");
    toggle.addEventListener("click", () => {
      const open = pre.hidden;
      toggle.setAttribute("aria-expanded", String(open));
      toggle.textContent = open ? "Hide SQL" : "SQL";
      if (!motion) { pre.hidden = !open; return; }
      if (open) {
        pre.hidden = false;
        gsap.fromTo(pre, { height: 0, opacity: 0 }, { height: "auto", opacity: 1, duration: 0.3, ease: "power2.out", clearProps: "height" });
      } else {
        gsap.to(pre, { height: 0, opacity: 0, duration: 0.18, ease: "power2.in", onComplete: () => { pre.hidden = true; gsap.set(pre, { clearProps: "all" }); } });
      }
    });
  });

  const col = (q, name) => q.rows.map((r) => r[q.columns.indexOf(name)]);
  drawChart($("monthly-chart"), { type: "line", labels: col(d.monthly, "month"), values: col(d.monthly, "revenue_eur"), label: "Revenue (EUR)" });
  drawChart($("categories-chart"), { type: "pie", labels: col(d.categories, "category"), values: col(d.categories, "revenue_eur"), label: "Revenue (EUR)" });
  drawChart($("cities-chart"), { type: "bar", labels: col(d.cities, "city"), values: col(d.cities, "revenue_eur"), label: "Revenue (EUR)", horizontal: false });
  renderTable(document.querySelector('[data-key="top_products"] .table-wrap'), d.top_products.columns, d.top_products.rows);
}

// ---------- Intro ----------

function intro() {
  if (!motion) return;
  gsap.timeline({ defaults: { ease: "power3.out" } })
    .from(".topbar", { y: -14, opacity: 0, duration: 0.5 })
    .from(".hero > *", { y: 16, opacity: 0, duration: 0.5, stagger: 0.06 }, "-=0.3")
    .from(".banner, .ask-panel", { y: 16, opacity: 0, duration: 0.5, stagger: 0.06 }, "-=0.35")
    .from(".examples .chip", { y: 8, opacity: 0, duration: 0.3, stagger: 0.03 }, "-=0.3");

  // Slow background drift (the only looping motion on the page).
  gsap.to(".glow-a", { x: 120, y: 40, duration: 26, ease: "sine.inOut", repeat: -1, yoyo: true });
  gsap.to(".glow-b", { x: -100, y: 60, duration: 22, ease: "sine.inOut", repeat: -1, yoyo: true });

  // Dashboard panels sit below the fold, so they rise in as they scroll into view.
  if (scrollMotion) {
    gsap.set(".grid .panel", { opacity: 0, y: 24 });
    ScrollTrigger.batch(".grid .panel", {
      start: "top 92%",
      once: true,
      onEnter: (panels) => gsap.to(panels, { opacity: 1, y: 0, duration: 0.6, stagger: 0.08, ease: "power3.out", clearProps: "transform" }),
    });
  }
}

intro();
loadMe();
loadHistory();
loadDashboard();
