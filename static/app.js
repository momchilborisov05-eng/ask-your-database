// Ask Your Database frontend: AI questions, results table and chart, and the SQL dashboard.

const $ = (id) => document.getElementById(id);
const COLORS = ["#2563eb", "#f97316", "#16a34a", "#db2777", "#7c3aed", "#ca8a04", "#0891b2", "#64748b"];
const money = new Intl.NumberFormat("en-US", { style: "currency", currency: "EUR", maximumFractionDigits: 0 });
const number = new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 });
let answerChart;

// GSAP is optional: without the CDN (or with reduced motion) the UI simply appears without animation.
const motion = typeof gsap !== "undefined" && !matchMedia("(prefers-reduced-motion: reduce)").matches;
const scrollMotion = motion && typeof ScrollTrigger !== "undefined";
if (scrollMotion) gsap.registerPlugin(ScrollTrigger);

async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "Request failed");
  return data;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text; // textContent: data can never inject HTML
  return node;
}

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
  const table = el("table");
  const head = el("tr");
  columns.forEach((c) => head.append(el("th", "", c)));
  const thead = el("thead");
  thead.append(head);
  table.append(thead);
  const body = el("tbody");
  rows.slice(0, limit).forEach((row) => {
    const tr = el("tr");
    row.forEach((v, i) => tr.append(el("td", typeof v === "number" ? "num" : "", formatCell(v, columns[i]))));
    body.append(tr);
  });
  table.append(body);
  container.replaceChildren(table);
  if (rows.length > limit) container.append(el("p", "muted", `Showing ${limit} of ${rows.length} rows.`));
  if (motion) gsap.from([...body.children].slice(0, 20), { opacity: 0, x: -8, duration: 0.3, stagger: 0.025, ease: "power2.out" });
}

// Counts a number up from zero while showing it in its final format.
function countUp(node, value, format) {
  node.textContent = format(value);
  if (!motion) return;
  const state = { value: 0 };
  gsap.to(state, { value, duration: 1.1, ease: "power2.out", onUpdate: () => { node.textContent = format(state.value); } });
}

function drawChart(canvas, type, labels, values, label) {
  if (typeof Chart === "undefined") return null;
  return new Chart(canvas, {
    type,
    data: {
      labels,
      datasets: [{
        label, data: values,
        backgroundColor: type === "line" ? "rgba(37,99,235,.15)" : COLORS,
        borderColor: type === "line" ? COLORS[0] : undefined,
        fill: type === "line", tension: 0.3, borderRadius: type === "bar" ? 4 : 0,
      }],
    },
    options: {
      maintainAspectRatio: false,
      indexAxis: type === "bar" && labels.length > 6 ? "y" : "x",
      plugins: { legend: { display: type === "pie", position: "right" } },
    },
  });
}

// ---------- Ask ----------

async function ask(question) {
  $("question").value = question;
  $("ask-error").textContent = "";
  $("ask-btn").disabled = true;
  $("ask-btn").textContent = "Thinking…";
  try {
    const data = await api("/api/ask", { method: "POST", body: JSON.stringify({ question }) });
    showAnswer(data);
  } catch (err) {
    $("answer").hidden = true;
    $("ask-error").textContent = err.message;
    if (motion) gsap.fromTo("#ask-error", { x: -6 }, { x: 0, duration: 0.5, ease: "elastic.out(1, 0.3)" });
  } finally {
    $("ask-btn").disabled = false;
    $("ask-btn").textContent = "Ask";
  }
}

function showAnswer(data) {
  $("answer").hidden = false;
  $("answer-text").textContent = data.answer;
  $("sql-explanation").textContent = data.explanation;
  $("sql-editor").value = data.sql;
  const fixed = data.attempts > 1 ? ` · AI fixed its own query ${data.attempts - 1}×` : "";
  $("sql-meta").textContent = `· ${data.rows.length}${data.truncated ? "+" : ""} rows · ${data.elapsed_ms} ms${fixed}`;
  $("sql-error").textContent = "";
  showResult(data.columns, data.rows, data.chart);
  if (motion) {
    gsap.from(["#answer-text", "#answer .answer-grid", "#answer .sql-details"],
      { opacity: 0, y: 14, duration: 0.5, stagger: 0.1, ease: "power3.out" });
  }
}

function showResult(columns, rows, chart) {
  renderTable($("answer-table"), columns, rows);
  answerChart?.destroy();
  answerChart = null;
  const box = $("answer-chart-box");
  const xi = columns.indexOf(chart?.x), yi = columns.indexOf(chart?.y);
  if (chart && chart.type !== "none" && xi >= 0 && yi >= 0 && rows.length > 1) {
    box.hidden = false;
    const data = chart.type === "line" ? rows : rows.slice(0, chart.type === "pie" ? 8 : 15);
    answerChart = drawChart($("answer-chart"), chart.type, data.map((r) => r[xi]), data.map((r) => r[yi]), chart.y);
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
  if (e.target.classList.contains("chip")) ask(e.target.textContent);
});
$("run-sql").addEventListener("click", async () => {
  $("sql-error").textContent = "";
  try {
    const data = await api("/api/query", { method: "POST", body: JSON.stringify({ sql: $("sql-editor").value }) });
    $("answer-text").textContent = "Result of your edited query:";
    showResult(data.columns, data.rows, null);
  } catch (err) {
    $("sql-error").textContent = err.message;
  }
});

// ---------- Dashboard ----------

async function loadDashboard() {
  const d = await api("/api/dashboard");

  const k = Object.fromEntries(d.kpis.columns.map((c, i) => [c, d.kpis.rows[0][i]]));
  const count = (v) => number.format(Math.round(v));
  const cards = [
    ["Revenue", k.revenue_eur, money.format], ["Orders", k.orders, count],
    ["Avg. order", k.avg_order_eur, money.format], ["Customers", k.customers, count],
    ["Gross profit", k.gross_profit_eur, money.format],
  ];
  $("kpis").replaceChildren(el("div", "kpi-title", `${d.kpis.title}`), ...cards.map(([label, value, format]) => {
    const card = el("div", "kpi");
    const valueEl = el("span", "value");
    card.append(el("span", "label", label), valueEl);
    countUp(valueEl, value, format);
    return card;
  }));
  if (motion) gsap.from("#kpis .kpi", { opacity: 0, y: 16, duration: 0.5, stagger: 0.07, ease: "power3.out" });

  document.querySelectorAll(".grid .panel").forEach((panel) => {
    const q = d[panel.dataset.key];
    panel.querySelector("h3").textContent = q.title;
    panel.querySelector(".sql").textContent = q.sql;
    panel.querySelector(".sql-toggle").addEventListener("click", () => {
      const pre = panel.querySelector(".sql");
      pre.hidden = !pre.hidden;
      if (motion && !pre.hidden) gsap.from(pre, { height: 0, opacity: 0, duration: 0.35, ease: "power2.out", clearProps: "height" });
    });
  });

  const col = (q, name) => q.rows.map((r) => r[q.columns.indexOf(name)]);
  drawChart($("monthly-chart"), "line", col(d.monthly, "month"), col(d.monthly, "revenue_eur"), "Revenue (EUR)");
  drawChart($("categories-chart"), "pie", col(d.categories, "category"), col(d.categories, "revenue_eur"), "Revenue (EUR)");
  drawChart($("cities-chart"), "bar", col(d.cities, "city"), col(d.cities, "revenue_eur"), "Revenue (EUR)");
  renderTable(document.querySelector('[data-key="top_products"] .table-wrap'), d.top_products.columns, d.top_products.rows);
}

function intro() {
  if (!motion) return;
  gsap.timeline({ defaults: { ease: "power3.out" } })
    .from("header h1, .subtitle", { y: 16, opacity: 0, duration: 0.6, stagger: 0.1 })
    .from(".banner, .ask-panel", { y: 24, opacity: 0, duration: 0.6, stagger: 0.08 }, "-=0.3")
    .from(".examples .chip", { y: 8, opacity: 0, duration: 0.35, stagger: 0.05 }, "-=0.3");

  // Dashboard panels sit below the fold, so they rise in as they scroll into view.
  if (scrollMotion) {
    gsap.set(".grid .panel", { opacity: 0, y: 30 });
    ScrollTrigger.batch(".grid .panel", {
      start: "top 90%",
      once: true,
      onEnter: (panels) => gsap.to(panels, { opacity: 1, y: 0, duration: 0.6, stagger: 0.1, ease: "power3.out" }),
    });
  }
}

intro();
loadDashboard();
