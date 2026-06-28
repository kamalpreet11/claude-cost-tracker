"use strict";
const D = window.TRACKER_DATA || {totals: {}, projects: []};
const SWATCH = ["#c98a3a", "#5b8def", "#3fb389", "#c678dd", "#d9a441", "#7ec8e3"];

// ---- formatters ----
const money = n => "$" + (Number(n) || 0).toFixed(2);
function dur(ms) {
  ms = Number(ms) || 0;
  const s = Math.round(ms / 1000), h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  if (h > 0) return h + "h " + String(m).padStart(2, "0") + "m";
  if (m > 0) return m + "m " + (s % 60) + "s";
  return s + "s";
}
function toks(n) {
  n = Number(n) || 0;
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "k";
  return String(n);
}
function when(iso) {
  if (!iso) return "-";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString([], {month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit"});
}
function sessionLabel(s) {
  const a = s.start ? new Date(s.start) : null, b = s.end ? new Date(s.end) : null;
  if (!a || isNaN(a)) return "-";
  const day = a.toLocaleDateString([], {month: "short", day: "numeric"});
  const t = d => d.toLocaleTimeString([], {hour: "numeric", minute: "2-digit"});
  return day + " - " + t(a) + (b && !isNaN(b) ? "->" + t(b) : "");
}
function modelClass(id) {
  id = (id || "").toLowerCase();
  if (id.includes("opus")) return "opus";
  if (id.includes("haiku")) return "haiku";
  if (id.includes("sonnet")) return "sonnet";
  return "other";
}
function shortModel(id) {
  return (id || "").replace(/^claude-/, "").replace(/ \(1M context\)/i, "");
}
const esc = s => String(s == null ? "" : s).replace(/[&<>"]/g,
  c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]));

// Token breakdown helpers. "In/out" = real new tokens you sent + Claude wrote.
// "Cache" = the conversation history re-read each turn (read) plus what's written
// to the cache (write) — this is what makes the raw total look huge.
function ioTokens(tk) { return (tk && (tk.input || 0) + (tk.output || 0)) || 0; }
function cacheTokens(tk) { return (tk && (tk.cache_read || 0) + (tk.cache_write || 0)) || 0; }
function totalTokens(t) {
  if (t && t.tokens) return t.tokens;
  const acc = {input: 0, output: 0, cache_read: 0, cache_write: 0};
  (D.projects || []).forEach(p => {
    const x = (p.totals && p.totals.tokens) || {};
    for (const k in acc) acc[k] += x[k] || 0;
  });
  return acc;
}

// ---- render ----
function kpis(t) {
  const tk = totalTokens(t);
  const cards = [
    ["Total cost", money(t.cost)],
    ["API time", dur(t.api_ms)],
    ["Wall time", dur(t.wall_ms)],
    ["Tokens", `${toks(ioTokens(tk))}<span class="unit">input + output</span>`],
    ["Tokens", `${toks(cacheTokens(tk))}<span class="unit">cache read + write</span>`],
    ["Sessions", String(t.sessions || 0)],
  ];
  return '<div class="kpis">' + cards.map(([l, v]) =>
    `<div class="kpi"><div class="lbl">${l}</div><div class="val">${v}</div></div>`).join("") + "</div>";
}

function modelTags(models) {
  return (models || []).map(m =>
    `<span class="tag ${modelClass(m)}">${esc(shortModel(m))}</span>`).join("");
}

function sessionsTable(p) {
  if (!p.sessions.length) return '<div style="padding:14px 18px;color:var(--mut)">No sessions recorded yet.</div>';
  const rows = p.sessions.slice().reverse().map(s => `
    <tr>
      <td>${esc(sessionLabel(s))}</td>
      <td>${dur(s.wall_ms)}</td>
      <td>${dur(s.api_ms)}</td>
      <td>${money(s.cost)}</td>
      <td>${modelTags(s.models)}</td>
      <td>${toks(ioTokens(s.tokens))}</td>
      <td>${toks(cacheTokens(s.tokens))}</td>
      <td class="lines"><span class="add">+${s.lines_added}</span> / <span class="rem">-${s.lines_removed}</span></td>
    </tr>`).join("");
  return `<div class="sessions"><table>
    <thead><tr><th>Session</th><th>Wall</th><th>API</th><th>Cost</th><th>Models</th><th>In/Out</th><th>Cache</th><th>Lines</th></tr></thead>
    <tbody>${rows}</tbody></table></div>`;
}

function projectCard(p, i) {
  const t = p.totals, color = SWATCH[i % SWATCH.length];
  return `<div class="proj" data-name="${esc(p.name)}">
    <div class="proj-head">
      <div class="pname"><span class="swatch" style="background:${color}"></span>${esc(p.name)}
        ${p.repo ? `<span class="repo">${esc(p.repo)}</span>` : ""}</div>
      <div class="cell"><div class="c-lbl">Cost</div><div class="c-val">${money(t.cost)}</div></div>
      <div class="cell"><div class="c-lbl">In/Out tok</div><div class="c-val">${toks(ioTokens(t.tokens))}</div></div>
      <div class="cell"><div class="c-lbl">Cache tok</div><div class="c-val">${toks(cacheTokens(t.tokens))}</div></div>
      <div class="cell"><div class="c-lbl">API time</div><div class="c-val">${dur(t.api_ms)}</div></div>
      <div class="chev">&rsaquo;</div>
    </div>
    ${sessionsTable(p)}
  </div>`;
}

function modelSpend(spend) {
  const entries = Object.entries(spend || {}).sort((a, b) => b[1].est_cost - a[1].est_cost);
  if (!entries.length) return "";
  const max = Math.max(...entries.map(([, v]) => v.est_cost), 1e-9);
  const colorFor = id => ({opus: "var(--acc)", haiku: "var(--acc2)",
    sonnet: "var(--good)", other: "var(--pink)"}[modelClass(id)]);
  const header = `<div class="modelrow mhead">
      <div class="mname"></div><div class="mbar"></div>
      <div class="mtok">In/Out</div><div class="mtok">Cache</div><div class="mcost">Cost</div>
    </div>`;
  const rows = entries.map(([id, v]) => `
    <div class="modelrow">
      <div class="mname"><span class="tag ${modelClass(id)}">${esc(shortModel(id))}</span></div>
      <div class="mbar"><div class="bar"><i style="width:${(v.est_cost / max * 100).toFixed(1)}%;background:${colorFor(id)}"></i></div></div>
      <div class="mtok">${toks(ioTokens(v.tokens))}</div>
      <div class="mtok">${toks(cacheTokens(v.tokens))}</div>
      <div class="mcost">${money(v.est_cost)}</div>
    </div>`).join("");
  return `<div class="section-title" style="margin-top:28px">Spend by model (all projects)</div>
    <div class="proj" style="cursor:default">${header}${rows}</div>
    <div class="note">* per-model cost is estimated from token counts at list prices; the
    headline totals above use the exact figures Claude Code records.</div>`;
}

function render() {
  const t = D.totals || {};
  const head = `<header><h1>Claude Project Tracker<span class="dot">.</span></h1>
    <button class="btn" id="refresh">&#8635; Refresh</button></header>
    <div class="sub">${(t.projects || 0)} project(s) tracked &middot; last refreshed ${when(D.generated_at)}</div>`;

  let body;
  if (!D.projects || !D.projects.length) {
    body = `<div class="empty">No tracked projects yet.<br><br>
      Run <code>./install.sh</code> inside a project on Day 0, use Claude there, then
      hit Refresh.</div>`;
  } else {
    body = kpis(t)
      + '<div class="section-title">Projects</div>'
      + D.projects.slice().sort((a, b) => b.totals.cost - a.totals.cost)
          .map(projectCard).join("")
      + modelSpend(t.model_spend);
  }
  document.getElementById("app").innerHTML = head + body;

  // Restore which projects were expanded, and persist toggles so a Refresh
  // (which does a full page reload) doesn't collapse the sessions table.
  const readOpen = () => {
    try { return new Set(JSON.parse(localStorage.getItem("openProjects") || "[]")); }
    catch (e) { return new Set(); }
  };
  const open = readOpen();
  document.querySelectorAll(".proj[data-name]").forEach(proj => {
    const name = proj.getAttribute("data-name");
    if (open.has(name)) proj.classList.add("open");
    const headEl = proj.querySelector(".proj-head");
    if (!headEl) return;
    headEl.addEventListener("click", () => {
      proj.classList.toggle("open");
      const s = readOpen();
      proj.classList.contains("open") ? s.add(name) : s.delete(name);
      try { localStorage.setItem("openProjects", JSON.stringify([...s])); } catch (e) {}
    });
  });

  const btn = document.getElementById("refresh");
  btn.addEventListener("click", () => {
    btn.disabled = true; btn.textContent = "Refreshing...";
    fetch("/refresh").then(r => r.json()).then(() => location.reload())
      .catch(() => { btn.disabled = false; btn.textContent = "Refresh failed - retry"; });
  });
}

render();
