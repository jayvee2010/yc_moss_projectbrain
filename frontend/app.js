/* ============================================================
   ProjectBrain — application logic    Real data only: /state · /timeline · /ask · /check · /resolve · /ingest*
   Derived metrics are computed from real data and labeled as such.
   ============================================================ */
"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* ---------- tiny utils ---------- */
const AI_AUTHORS = /claude|gpt|gemini|copilot|agent|bot|inferred|openai|anthropic/i;
const isAI = (name) => AI_AUTHORS.test(String(name || ""));
const initials = (n) => String(n || "?").replace(/[^a-zA-Z0-9]/g, "").slice(0, 2).toUpperCase() || "?";
function hue(s) { let h = 0; for (const c of String(s)) h = (h * 31 + c.charCodeAt(0)) % 360; return h; }
function avatar(name, cls = "") {
  const n = String(name || "?");
  const ai = isAI(n);
  return `<span class="av ${ai ? "ai" : ""} ${cls}" data-member="${esc(n)}"
    style="background:hsl(${hue(n)} 30% 22%);color:hsl(${hue(n)} 80% 78%)">${esc(initials(n))}</span>`;
}
function rel(iso) {
  const d = new Date(iso); if (isNaN(d)) return "";
  const s = (Date.now() - d.getTime()) / 1000;
  if (s < 45) return "just now";
  if (s < 3600) return Math.max(1, Math.floor(s / 60)) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}
function hhmm(iso) { const d = new Date(iso); return isNaN(d) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false }); }
function dayLabel(iso) {
  const d = new Date(iso); if (isNaN(d)) return "";
  const today = new Date(); const y = new Date(Date.now() - 864e5);
  const same = (a, b) => a.toDateString() === b.toDateString();
  if (same(d, today)) return "Today";
  if (same(d, y)) return "Yesterday";
  return d.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
}
function toast(msg, kind = "", ms = 4200) {
  const el = document.createElement("div");
  el.className = "toast " + kind; el.textContent = msg;
  $("toasts").appendChild(el); setTimeout(() => el.remove(), ms);
}

/* ---------- api ---------- */
/* Backend base URL:
   - served by FastAPI (http://localhost:8000)  → relative paths, same origin
   - opened as a file (file://…index.html)      → http://127.0.0.1:8000, overridable
     in Settings → Engine URL (saved to localStorage as pb_engine).            */
const FILE_PROTOCOL = location.protocol === "file:";
const API_BASE = FILE_PROTOCOL
  ? (localStorage.getItem("pb_engine") || "http://127.0.0.1:8000").replace(/\/$/, "")
  : "";
let lastEndpoint = "";
async function api(path, body) {
  lastEndpoint = path;
  const res = await fetch(API_BASE + path, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : undefined);
  let data = null; try { data = await res.json(); } catch {}
  if (!res.ok) { const e = new Error((data && data.detail) || `HTTP ${res.status}`); e.status = res.status; e.endpoint = path; throw e; }
  return data;
}

/* ---------- state ---------- */
const store = {
  project: localStorage.getItem("pb_project") || "aura-smart-home",
  state: null, timeline: [], lastError: null, online: false,
  lastAsk: null,            // { ms, sources } — real measured values only
  conflictsFound: 0,        // session-only, from real /check results
  autoTimer: null,
};
const saveProject = (p) => { store.project = p; localStorage.setItem("pb_project", p); };
const projectId = () => store.project;

/* ---------- AI activity pill ---------- */
let pillTimer = null;
function aiPill(name, status, done = false) {
  const p = $("aiPill");
  $("aiPillName").textContent = name;
  $("aiPillStatus").textContent = status;
  p.classList.toggle("done", done);
  p.classList.add("show");
  clearTimeout(pillTimer);
  if (done) pillTimer = setTimeout(() => p.classList.remove("show"), 2600);
}
function hidePill() { clearTimeout(pillTimer); $("aiPill").classList.remove("show"); }

/* ---------- router ---------- */
const VIEWS = ["overview", "ask", "activity", "decisions", "tasks", "conflicts", "moss", "ingest", "settings"];
function show(view) {
  if (!VIEWS.includes(view)) view = "overview";
  document.querySelectorAll(".page").forEach((p) => p.classList.toggle("active", p.id === "page-" + view));
  document.querySelectorAll(".nv[data-view]").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  if (view === "overview") renderOverview();
  if (view === "activity") renderTimelinePage();
  if (view === "decisions") renderDecisions();
  if (view === "tasks") renderTasks();
  if (view === "moss") renderMoss();
  if (view === "ask") setTimeout(() => $("askInput").focus(), 80);
  if (view === "conflicts") setTimeout(() => $("conflictInput").focus(), 80);
}
document.addEventListener("click", (e) => {
  const g = e.target.closest("[data-goto]");
  if (g) { show(g.dataset.goto); if (g.closest(".modalBack")) closeWhatsNew(); }
  const c = e.target.closest("[data-close]");
  if (c) closeWhatsNew();
});

/* ---------- empty / error states ---------- */
function emptyState(glyph, title, sub, ctaView, ctaLabel) {
  return `<div class="emptyState"><div class="glyph">${glyph}</div><div class="t">${esc(title)}</div>
    <div class="s">${esc(sub)}</div>${ctaView ? `<button class="btn" data-goto="${ctaView}">${esc(ctaLabel || "Add context")}</button>` : ""}</div>`;
}
function errorState(err) {
  return `<div class="errorState">
    <svg width="26" height="26" viewBox="0 0 24 24" fill="none"><path d="M12 3L22 20H2L12 3z" stroke="var(--amber)" stroke-width="1.6" stroke-linejoin="round"/><path d="M12 10v4.5M12 17.2v.8" stroke="var(--amber)" stroke-width="1.6" stroke-linecap="round"/></svg>
    <div class="t">ProjectBrain can't reach the project engine</div>
    <div class="s">The interface is still available, but live project state couldn't be synchronized.</div>
    <button class="btn primary" onclick="loadAll()">Retry connection</button>
    <div class="tech">${esc(err ? (err.endpoint || lastEndpoint) + " · " + err.message : "unknown error")}</div>
  </div>`;
}

/* ---------- data loading ---------- */
async function loadAll(silent = false) {
  try {
    const [st, tl] = await Promise.all([
      api(`/state?project_id=${encodeURIComponent(projectId())}`),
      api(`/timeline?project_id=${encodeURIComponent(projectId())}&limit=80`),
    ]);
    store.state = st; store.timeline = tl.events || []; store.online = true; store.lastError = null;
    renderShell(); renderOverview();
    if (!silent && ["activity", "decisions", "tasks"].includes(currentView())) { show(currentView()); }
  } catch (err) {
    store.online = false; store.lastError = err;
    renderShellOffline(err);
  }
}
const currentView = () => (document.querySelector(".page.active") || {}).id?.replace("page-", "") || "overview";

/* ---------- sidebar & project strip ---------- */
function renderShell() {
  const st = store.state, tl = store.timeline;
  $("sbProjName").textContent = projectId();
  const humans = new Set(), ais = new Set();
  tl.forEach((e) => (isAI(e.author) ? ais : humans).add(e.author));
  $("sbProjCounts").textContent = humans.size ? `· ${humans.size} collaborator${humans.size > 1 ? "s" : ""}${ais.size ? ` · ${ais.size} agent${ais.size > 1 ? "s" : ""}` : ""}` : "";

  // counters
  const byType = (st.stats && st.stats.by_type) || {};
  $("cntActivity").textContent = tl.length || "";
  $("cntDecisions").textContent = byType.decision || "";
  const openTasks = (st.tasks || []).filter((t) => t.status !== "completed").length
    + (byType.task || 0) + (byType.issue || 0);
  $("cntTasks").textContent = openTasks || "";
  $("cntConflicts").textContent = store.conflictsFound || "";

  // status footer
  $("sbStatus").innerHTML = `<span class="dot ok"></span> System operational`;
  const moss = $("sbMoss");
  if (store.lastAsk) { moss.style.display = "flex"; $("sbMossLat").textContent = store.lastAsk.ms + "ms"; }
  else moss.style.display = "none";

  // strip
  const overviewMem = tl.find((e) => e.type === "fact" && /overview/i.test(e.title));
  const desc = overviewMem ? overviewMem.content.split(/(?<=\.)\s/)[0] : "Your entire team. One living project context.";
  $("stripName").textContent = projectId();
  $("stripDesc").textContent = desc;
  const newest = tl[0];
  $("stripActive").outerHTML = newest
    ? `<span class="st ok" id="stripActive">Active · last event ${rel(newest.created_at)}</span>`
    : `<span class="st mut" id="stripActive">Awaiting first event</span>`;
  const stack = [...new Set(tl.slice(0, 30).flatMap((e) => e.entities || []))].slice(0, 4);
  $("stripStack").innerHTML = stack.length ? stack.map((s) => `<span class="tag">${esc(s)}</span>`).join(" ") : "";
  const faces = [...new Set(tl.map((e) => e.author))].slice(0, 5);
  $("stripFaces").innerHTML = faces.map((f) => avatar(f, "sm")).join("");
}

function renderShellOffline(err) {
  $("sbStatus").className = "sbStatus degraded";
  $("sbStatus").innerHTML = `<span class="dot warn"></span> Engine unreachable — retrying`;
  $("sbMoss").style.display = "none";
  ["ovTimeline", "ovDecisions", "ovTeam", "timeline", "decList", "blockerList", "taskList"].forEach((id) => {
    if ($(id)) $(id).innerHTML = errorState(err);
  });
}

/* ---------- derived helpers (all computed from real rows) ---------- */
function healthFromStats(stats, tasks) {
  // honest composite: decision clarity, task progress, unblocked ratio — labeled derived
  const byType = (stats && stats.by_type) || {};
  const dec = byType.decision || 0, rej = byType.rejected || 0;
  const total = stats?.total_memories || 0;
  const doneT = (tasks || []).filter((t) => t.status === "completed").length;
  const allT = Math.max(1, (tasks || []).length);
  const blocked = stats?.blocked_tasks || 0;
  const clarity = Math.min(100, Math.round(((dec + rej) / Math.max(1, total)) * 220));
  const progress = Math.round((doneT / allT) * 100);
  const flow = Math.max(0, 100 - blocked * 18);
  const overall = Math.round(clarity * 0.3 + progress * 0.35 + flow * 0.35);
  return { overall, rows: [["Context clarity", clarity, ""], ["Task progress", progress, "v"], ["Flow (unblocked)", flow, "s"]] };
}

function liveItems() {
  return store.timeline.slice(0, 3).map((e) => {
    const verb = { decision: e.status === "rejected" ? "rejected" : "decided", change: "shipped", blocker: "hit a blocker", task: "updated", experiment: "ran an experiment", fact: "logged" }[e.type] || "updated";
    return `<span class="liveItem">${avatar(e.author, "sm")} <span><b>${esc(e.author)}</b> ${esc(verb)} — ${esc(e.title.length > 46 ? e.title.slice(0, 46) + "…" : e.title)}</span> <span class="when">${rel(e.created_at)}</span></span>`;
  }).join("");
}

/* ---------- timeline renderer (shared) ---------- */
const TYPE_CLASS = { decision: "decision", change: "change", blocker: "blocker", task: "task", experiment: "experiment" };
function timelineHTML(events, { limit = Infinity, withDays = true } = {}) {
  if (!events.length) return emptyState("◌", "Nothing here yet.", "Once your team makes decisions or adds project context, events will appear here.", "ingest", "Feed context");
  let html = "", lastDay = "";
  events.slice(0, limit).forEach((e, i) => {
    const day = dayLabel(e.created_at);
    if (withDays && day !== lastDay) { html += `<div class="daySep">${esc(day)}</div>`; lastDay = day; }
    const ai = isAI(e.author);
    const node = TYPE_CLASS[e.type] || (ai ? "ai" : "");
    const chips = (e.entities || []).slice(0, 3).map((x) => `<span class="tag">${esc(x)}</span>`).join("");
    html += `
      <div class="tlItem" style="animation-delay:${Math.min(i * 30, 300)}ms">
        <div class="tlTime">${hhmm(e.created_at)}<span class="rel">${rel(e.created_at)}</span></div>
        <div class="tlRail"><div class="tlNode ${node}"></div></div>
        <div class="tlBody">
          <div class="who">${avatar(e.author, "sm")} <b>${esc(e.author)}</b>
            ${ai ? `<span class="tag mint">AI</span>` : ""} <span class="tag ${e.type === "blocker" ? "coral" : e.type === "decision" ? "violet" : e.type === "change" ? "mint" : "slate"}">${esc(e.type)}</span></div>
          <div class="what">${esc(e.title)}</div>
          ${e.content && e.content !== e.title ? `<div class="note">${esc(e.content.length > 180 ? e.content.slice(0, 180) + "…" : e.content)}</div>` : ""}
          ${chips ? `<div class="chips">${chips}</div>` : ""}
        </div>
      </div>`;
  });
  return html;
}

/* ---------- activity page ---------- */
function renderTimelinePage() {
  $("timeline").innerHTML = timelineHTML(store.timeline);
}

/* ---------- overview ---------- */
function renderOverview() {
  const st = store.state; if (!st) return;
  const stats = st.stats || { total_memories: 0, by_type: {}, blocked_tasks: 0 };
  const byType = stats.by_type || {};
  const overviewMem = store.timeline.find((e) => e.type === "fact" && /overview/i.test(e.title));
  $("ovName").textContent = projectId();
  $("ovDesc").textContent = overviewMem ? overviewMem.content.split(/(?<=\.)\s/)[0]
    : (stats.total_memories ? "Live project memory — decisions, changes and blockers for humans and AI agents." : "An empty project. Feed it context or connect a repository to begin.");
  const stack = [...new Set(store.timeline.slice(0, 30).flatMap((e) => e.entities || []))].slice(0, 5);
  $("ovStack").innerHTML = stack.map((s) => `<span class="tag">${esc(s)}</span>`).join("");

  const h = healthFromStats(stats, st.tasks);
  $("ovHealth").innerHTML = `
    <div class="eyebrow" style="margin-bottom:8px">Project health <span title="Derived from decision clarity, task progress and blockers in your real data" style="cursor:help">· derived</span></div>
    <div style="display:flex; align-items:baseline; gap:6px; margin-bottom:8px"><span class="overall">${h.overall}<small>/100</small></span></div>
    ${h.rows.map(([k, v, c]) => `<div class="row"><span class="lbl">${k}</span><span class="meter"><i class="${c}" style="width:${v}%"></i></span><span class="val">${v}%</span></div>`).join("")}`;

  $("liveBar").innerHTML = `<span class="lh"><span class="dot live"></span> LIVE NOW</span>` +
    (store.timeline.length ? liveItems() : `<span class="liveItem t3">Quiet — no events yet. Feed the brain to wake it up.</span>`);

  const openTasks = (st.tasks || []).filter((t) => t.status !== "completed").length;
  $("ovStats").innerHTML = `
    <div class="cell"><div class="v">${stats.total_memories}</div><div class="k">memories</div></div>
    <div class="cell"><div class="v mint">${byType.decision || 0}</div><div class="k">decisions</div></div>
    <div class="cell"><div class="v">${byType.change || 0}</div><div class="k">changes</div></div>
    <div class="cell"><div class="v">${openTasks}</div><div class="k">open tasks</div></div>
    <div class="cell"><div class="v ${stats.blocked_tasks ? "alert" : ""}">${stats.blocked_tasks}</div><div class="k">blockers</div></div>`;

  $("ovTimeline").innerHTML = timelineHTML(store.timeline, { limit: 6, withDays: false });
  $("ovDecisions").innerHTML = decisionsHTML(store.timeline.filter((e) => e.type === "decision"), 5);
  $("ovTeam").innerHTML = teamHTML();
}

/* ---------- team ---------- */
function teamHTML() {
  const map = new Map();
  store.timeline.forEach((e) => {
    const a = e.author;
    if (!map.has(a)) map.set(a, { author: a, last: e, count: 0 });
    map.get(a).count++;
  });
  const rows = [...map.values()].slice(0, 7);
  if (!rows.length) return emptyState("◌", "No members yet.", "Authors appear here as humans and agents act on the project.");
  return rows.map((r) => `
    <div class="teamRow">${avatar(r.author)}
      <div><div class="nm">${esc(r.author)} ${isAI(r.author) ? `<span class="tag mint">AI agent</span>` : `<span class="tag slate">human</span>`}</div>
      <div class="role">${r.count} event${r.count > 1 ? "s" : ""}</div></div>
      <div class="acts"><b>${esc(r.last.title.length > 34 ? r.last.title.slice(0, 34) + "…" : r.last.title)}</b>${rel(r.last.created_at)}</div>
    </div>`).join("");
}

/* ---------- member hovercard ---------- */
document.addEventListener("mouseover", (e) => {
  const av = e.target.closest("[data-member]");
  const card = $("hovercard");
  if (!av) { card.classList.remove("show"); return; }
  const name = av.dataset.member;
  const last = store.timeline.find((t) => t.author === name);
  card.innerHTML = `<div class="hcHead">${avatar(name)}<div><div class="nm" style="font-weight:600; font-size:13px">${esc(name)}</div>
    <div class="tiny t3">${isAI(name) ? "AI agent" : "Human"}</div></div></div>
    ${last ? `<div class="hcRow"><span>Last action</span><b>${esc(last.title.length > 30 ? last.title.slice(0, 30) + "…" : last.title)}</b></div>
    <div class="hcRow"><span>Active</span><b>${rel(last.created_at)}</b></div>` : `<div class="hcRow">No activity recorded yet</div>`}`;
  const r = av.getBoundingClientRect();
  card.style.left = Math.min(r.left, window.innerWidth - 250) + "px";
  card.style.top = r.bottom + 8 + "px";
  card.classList.add("show");
});
document.addEventListener("scroll", () => $("hovercard").classList.remove("show"), true);

/* ---------- decisions ---------- */
function decisionsHTML(decisions, limit = Infinity) {
  if (!decisions.length) return emptyState("◌", "No decisions yet.", "Approvals and rejections appear here with their reasoning.", "ingest", "Feed context");
  return decisions.slice(0, limit).map((d, i) => {
    const rejected = d.status === "rejected" || /reject/i.test(d.title);
    const area = (d.entities || [])[0] || "";
    return `
    <div class="decRow" data-dec="${esc(d.id)}" style="animation-delay:${Math.min(i * 25, 200)}ms">
      <div class="mark ${rejected ? "no" : "ok"}">${rejected ? "×" : "✓"}</div>
      <div class="what"><b>${esc(d.title)}</b>${area ? `<span class="area">${esc(area)}</span>` : ""}</div>
      <div class="side"><span class="st ${rejected ? "bad" : "ok"}">${esc(d.status || (rejected ? "rejected" : "approved"))}</span>
        <span class="tiny t3">${esc(d.author)} · ${rel(d.created_at)}</span></div>
      <div class="decDetail">
        <div class="grid">
          <div class="cell"><div class="k">Who</div><div class="v">${esc(d.author)}</div></div>
          <div class="cell"><div class="k">When</div><div class="v">${esc(new Date(d.created_at).toLocaleString())}</div></div>
          <div class="cell"><div class="k">Status</div><div class="v">${esc(d.status)}</div></div>
          <div class="cell"><div class="k">Reference</div><div class="v mono">${esc(d.id)}</div></div>
        </div>
        <div class="k eyebrow" style="margin-bottom:4px">Why</div>
        <div class="quote">${esc(d.content)}</div>
        ${(d.entities || []).length ? `<div class="chips" style="margin-top:10px">${d.entities.map((x) => `<span class="tag">${esc(x)}</span>`).join("")}</div>` : ""}
      </div>
    </div>`;
  }).join("");
}
function renderDecisions() {
  const dec = store.timeline.filter((e) => e.type === "decision");
  $("decList").innerHTML = decisionsHTML(dec);
}
document.addEventListener("click", (e) => {
  const row = e.target.closest(".decRow[data-dec]");
  if (row) row.classList.toggle("open");
});

/* ---------- tasks & blockers ---------- */
function renderTasks() {
  const st = store.state;
  if (!st) return;
  const taskMems = store.timeline.filter((e) => e.type === "task" || e.type === "issue");
  const blockers = st.blockers || [];
  const blockerMems = store.timeline.filter((e) => e.type === "blocker");

  $("blockerList").innerHTML = blockers.length || blockerMems.length
    ? [...blockers.map((b) => ({ ...b, _kind: "task" })), ...blockerMems.map((b) => ({ ...b, _kind: "mem" }))].map((b) => `
      <div class="blocker">
        <div class="head"><span class="ico">⚠</span> ${esc(b.title)}</div>
        <div class="body">${esc(b.description || b.content || "")}</div>
        <div class="foot">
          <span>Detected ${rel(b.created_at || b.updated_at)}</span>
          ${b.priority ? `<span>· priority ${esc(b.priority)}</span>` : ""}
          <button class="btn ghost" data-goto="ask" data-prefill="What is blocking ${esc((b.title || "this task").replace(/^Implement /, ""))}?">View context</button>
        </div>
      </div>`).join("")
    : emptyState("✓", "No blockers.", "Nothing is standing in the way right now.");

  const all = [...(st.tasks || []).map((t) => ({ ...t, _kind: "task" })), ...taskMems.map((m) => ({ ...m, _kind: "mem" }))];
  $("taskHint").textContent = all.length ? `${all.filter((t) => t._kind === "task" && t.status === "completed").length} of ${(st.tasks || []).length} tracked tasks completed` : "";
  $("taskList").innerHTML = all.length ? all.map((t) => {
    const done = t.status === "completed";
    const g = done ? "ok" : t.status === "blocked" ? "bad" : t.status === "in_progress" ? "run" : "";
    const glyph = done ? "✓" : t.status === "blocked" ? "⚠" : t.status === "in_progress" ? "◐" : "○";
    return `
      <div class="taskRow">
        <span class="glyph ${g}">${glyph}</span>
        <span class="t ${done ? "done" : ""}">${esc(t.title)} ${t._kind === "mem" ? `<span class="tag slate">memory</span>` : ""}</span>
        <span class="st ${done ? "ok" : t.status === "blocked" ? "bad" : "sky"}">${esc(t.status || "active")}</span>
        <span class="p">${esc(t.priority || "")}</span>
      </div>`;
  }).join("") : emptyState("◌", "No tasks yet.", "Open issues from connected repositories and task memories appear here.", "ingest", "Connect a repo");
}

/* ---------- ask ---------- */
const SUGGESTED = [
  "Why did we reject Firebase?",
  "What is currently blocking authentication?",
  "What did we try before Supabase?",
  "Why did we choose the ESP32?",
  "What changed in the last 24 hours?",
];
function renderQPills() {
  // pills are generic; suggestions from real blockers get priority placement
  const b = (store.state?.blockers || [])[0];
  const pills = [...SUGGESTED];
  if (b) pills.unshift(`What is blocking ${b.title.replace(/^Implement /, "")}?`);
  $("qPills").innerHTML = pills.slice(0, 6).map((q) => `<button class="qPill">${esc(q)}</button>`).join("");
  $("qPills").querySelectorAll(".qPill").forEach((el, i) => (el.onclick = () => ask(pills[i])));
}

let asking = false;
async function ask(q) {
  q = (q || $("askInput").value).trim();
  if (!q || asking) return;
  asking = true;
  $("askInput").value = q;
  $("answerZone").style.display = "block";
  $("retrBtn").style.display = "none";
  $("retrPanel").classList.remove("open");
  const at = $("answerText");
  at.className = "answerText thinking";
  at.innerHTML = `<span class="spin">✦</span> retrieving relevant project context…`;
  $("srcList").innerHTML = "";
  aiPill("ProjectBrain", "retrieving context…");
  try {
    const data = await api("/ask", { project_id: projectId(), question: q });
    const t = data.timings || {};
    const sources = data.sources || [];
    store.lastAsk = { ms: t.moss_ms ?? null, sources };
    // type out the answer
    at.className = "answerText";
    const answer = data.answer || "(empty answer)";
    let i = 0;
    at.innerHTML = `<span class="cursor"></span>`;
    const tick = () => {
      i = Math.min(answer.length, i + 3);
      at.textContent = answer.slice(0, i);
      if (i < answer.length) requestAnimationFrame(tick);
      else at.textContent = answer;
    };
    tick();
    // retrieval chip — REAL measured values only
    if (t.moss_ms != null) {
      $("retrBtn").style.display = "inline-flex";
      $("retrLabel").innerHTML = `Retrieved <span class="n">&nbsp;${sources.length}&nbsp;</span> relevant memories · <span class="ms">&nbsp;${t.moss_ms}ms</span>`;
      const kinds = {};
      sources.forEach((s) => (kinds[s.type] = (kinds[s.type] || 0) + 1));
      $("retrPanel").innerHTML = `
        <div class="rrow"><span>Moss retrieval</span><b>${t.moss_ms} ms</b></div>
        <div class="rrow"><span>Answer generation</span><b>${t.llm_ms ?? "—"} ms</b></div>
        <div class="rrow"><span>End to end</span><b>${t.total_ms ?? "—"} ms</b></div>
        <div class="rrow" style="border-top:1px solid var(--line); margin-top:6px; padding-top:8px"><span>Memory types used</span><b>${Object.entries(kinds).map(([k, v]) => `${v} ${k}`).join(" · ") || "—"}</b></div>`;
      $("sbMoss").style.display = "flex";
      $("sbMossLat").textContent = t.moss_ms + "ms";
    }
    // sources
    $("srcList").innerHTML = sources.length
      ? sources.map((s, i) => `
        <div class="srcItem">
          <span class="idx">[${i + 1}]</span>
          <span class="tag ${s.type === "decision" ? "violet" : s.type === "blocker" ? "coral" : "mint"}">${esc(s.type)}</span>
          <span><span class="tt">${esc(s.title)}</span><div class="meta">${esc(s.author)} · ${esc(new Date(s.created_at).toLocaleDateString([], { month: "short", day: "numeric" }))} ${hhmm(s.created_at)}</div></span>
          <span class="score">${esc(String((s.score ?? "")).slice(0, 5))}</span>
        </div>`).join("")
      : `<div class="tiny t3" style="padding:10px 4px">No sources returned for this question.</div>`;
    aiPill("ProjectBrain", `context synchronized · ${t.moss_ms ?? "?"}ms`, true);
  } catch (err) {
    at.className = "answerText";
    at.textContent = "Couldn't complete that: " + err.message;
    toast("Ask failed — " + err.message, "bad");
    hidePill();
  } finally {
    asking = false;
  }
}
$("retrBtn").onclick = () => $("retrPanel").classList.toggle("open");

/* ---------- conflicts ---------- */
const CONFLICT_PILLS = [
  "Let's switch to Firebase for authentication",
  "Use MongoDB for the database",
  "Add an Arduino Uno for door sensors",
  "Replace Vite with Create React App",
];
function renderConflictPills() {
  $("conflictPills").innerHTML = CONFLICT_PILLS.map((q) => `<button class="qPill">${esc(q)}</button>`).join("");
  $("conflictPills").querySelectorAll(".qPill").forEach((el, i) => (el.onclick = () => checkConflict(CONFLICT_PILLS[i])));
}

let lastConflictAction = "";
let lastConflictMemoryId = null;   // current decision the proposal conflicts with (from /check)
async function checkConflict(action) {
  action = (action || $("conflictInput").value).trim();
  if (!action) return;
  lastConflictAction = action;
  $("conflictInput").value = action;
  const zone = $("conflictZone");
  zone.style.display = "block";
  $("impactBox").innerHTML = `<div class="skeleton" style="max-width:420px"></div><div class="skeleton" style="max-width:300px"></div>`;
  $("verdictBox").innerHTML = "";
  $("vsProposal").textContent = action;
  $("vsProposalWho").innerHTML = `${avatar("you", "sm")} proposed just now`;
  try {
    const data = await api("/check", { project_id: projectId(), action });
    const t = data.timings || {};
    const mem = data.conflicting_memory;
    lastConflictMemoryId = mem?.id || null;
    // current side
    if (mem) {
      $("vsCurrent").textContent = mem.title;
      $("vsCurrentWho").innerHTML = `${avatar(mem.author, "sm")} ${esc(mem.author)} · ${esc(new Date(mem.created_at).toLocaleDateString([], { month: "short", day: "numeric" }))} · ${esc(mem.status)}`;
    } else {
      $("vsCurrent").textContent = "No directly conflicting decision found";
      $("vsCurrentWho").textContent = "Checked against all project memories";
    }
    // impact — honest: computed from real linked entities only
    const ents = (mem?.entities || []).map((x) => x.toLowerCase());
    const linked = store.timeline.filter((e) => (e.entities || []).some((x) => ents.includes(String(x).toLowerCase())) && e.id !== mem?.id);
    $("impactBox").innerHTML = linked.length ? `
      <div class="impact">
        <div class="eyebrow" style="margin-bottom:8px">Connected context · ${linked.length} related event${linked.length > 1 ? "s" : ""}</div>
        ${linked.slice(0, 4).map((e) => `<div class="row"><span class="tag ${e.type === "decision" ? "violet" : "mint"}">${esc(e.type)}</span> <span>${esc(e.title.length > 64 ? e.title.slice(0, 64) + "…" : e.title)}</span><span class="tiny t3" style="margin-left:auto">${rel(e.created_at)}</span></div>`).join("")}
      </div>` : "";
    // verdict
    if (data.conflict) {
      store.conflictsFound++;
      $("cntConflicts").textContent = store.conflictsFound;
      $("verdictBox").innerHTML = `
        <div class="verdict bad">
          <div class="vh">△ ${esc(data.level || "CONFLICT")}</div>
          <p class="expl">${esc(data.reason || "")}</p>
          <p class="tiny t3">Checked against ${data.timings?.moss_ms ?? "?"}ms of Moss retrieval · ${t.total_ms ?? "?"}ms total</p>
          <div class="vactions">
            <button class="btn primary" id="cAccept">Create new decision</button>
            <button class="btn" id="cKeep">Keep current decision</button>
          </div>
        </div>`;
      $("cKeep").onclick = () => postResolution("keep");
      $("cAccept").onclick = () => postResolution("supersede");
    } else {
      $("verdictBox").innerHTML = `
        <div class="verdict good">
          <div class="vh">✓ No conflict</div>
          <p class="expl">${esc(data.reason || "This action aligns with current project decisions.")}</p>
          <p class="tiny t3">Checked in ${t.moss_ms ?? "?"}ms (Moss) · ${t.total_ms ?? "?"}ms total</p>
        </div>`;
    }
  } catch (err) {
    $("impactBox").innerHTML = "";
    $("verdictBox").innerHTML = `<div class="verdict bad"><div class="vh">△ Check failed</div><p class="expl">${esc(err.message)}</p></div>`;
  }
}
/* Both Conflicts-page actions hit POST /resolve:
   supersede → old decision marked 'superseded', proposal becomes the active decision
   keep      → proposal recorded as a rejected_approach so future identical
               proposals hit deterministic conflict detection (the system learns) */
async function postResolution(resolution) {
  const btn = resolution === "supersede" ? $("cAccept") : $("cKeep");
  if (!btn) return;
  const original = btn.textContent;
  btn.disabled = true; btn.textContent = "Recording…";
  try {
    const who = $("ingestAuthor").value.trim() || "you";
    const res = await api("/resolve", {
      project_id: projectId(),
      action: lastConflictAction,
      resolution,
      conflicting_memory_id: lastConflictMemoryId,
      author: who,
    });
    const ms = res.timings?.total_ms;
    if (resolution === "supersede") {
      toast(`Recorded as the new active decision${res.superseded_memory_id ? " · old decision superseded" : ""}${ms ? ` · ${ms}ms` : ""}`, "");
    } else {
      toast(`Kept the current decision · proposal recorded as rejected${ms ? ` · ${ms}ms` : ""}`, "");
    }
    if (res.moss_warning) toast("Moss sync deferred — resolution saved in SQLite: " + res.moss_warning, "warn");
    $("verdictBox").innerHTML = "";
    loadAll(true);
  } catch (e) {
    toast("Could not record: " + e.message, "bad");
    btn.disabled = false; btn.textContent = original;
  }
}

/* ---------- ingest ---------- */
const PIPELINE = ["Reading", "Understanding", "Extracting decisions", "Updating project state", "Indexed"];
function pipelineStart() {
  const p = $("pipeline");
  p.style.display = "flex";
  p.innerHTML = PIPELINE.map((s, i) => `<div class="pStep" data-i="${i}"><span class="pn">${i + 1}</span> ${s} <span class="ms"></span></div>`).join("");
}
function pipelineStep(i, ms) {
  document.querySelectorAll(".pStep").forEach((el) => {
    const n = +el.dataset.i;
    el.classList.toggle("done", n < i);
    el.classList.toggle("active", n === i);
    if (n === i - 1) el.querySelector(".ms").textContent = ms;
  });
}
function pipelineDone() {
  document.querySelectorAll(".pStep").forEach((el) => { el.classList.remove("active"); el.classList.add("done"); });
}

async function ingestText(text, authorOverride) {
  text = (text ?? $("ingestText").value).trim();
  if (!text) { toast("Paste some context first", "warn"); return; }
  const body = { project_id: projectId(), text, author: authorOverride ?? ($("ingestAuthor").value.trim() || "unknown") };
  pipelineStart();
  pipelineStep(1);
  const t0 = performance.now();
  const mark = setInterval(() => pipelineStep(2, Math.round(performance.now() - t0) + "ms"), 350);
  try {
    const r = await api("/ingest", body);
    clearInterval(mark);
    pipelineStep(3, Math.round(r.timings?.llm_ms || 0) + "ms");
    pipelineStep(4, Math.round(r.timings?.total_ms || 0) + "ms");
    setTimeout(pipelineDone, 300);
    toast(`Extracted ${r.memories.length} memories in ${(r.timings?.total_ms / 1000).toFixed(1)}s`, "");
    $("ingestText").value = "";
    loadAll(true);
  } catch (e) {
    clearInterval(mark);
    pipelineStep(3);
    toast("Ingest failed: " + e.message, "bad", 7000);
  }
}

async function connectGitHub() {
  const repo = $("repoInput").value.trim();
  if (!repo) return;
  pipelineStart();
  pipelineStep(1);
  try {
    const s = await api("/ingest/github", { project_id: projectId(), repo, use_llm: $("useLlm").checked });
    pipelineStep(3, s.commits + " commits");
    pipelineStep(4, s.issues + " issues");
    setTimeout(pipelineDone, 300);
    toast(`Connected ${s.repo}: ${s.commits} commits · ${s.issues} issues · ${s.insights} insights`, "", 7000);
    if (s.moss_error) toast("Moss indexing failed — /ask needs real Moss keys", "warn", 7000);
    loadAll(true);
  } catch (e) {
    pipelineStep(3);
    toast("GitHub connect failed: " + e.message, "bad", 7000);
  }
}

const DEMO_STORY = `Team meeting for the AURA Smart Home security prototype. We decided to use Supabase for authentication and the database because we want PostgreSQL compatibility and full control over the backend. We rejected Firebase earlier because of vendor lock-in and no direct PostgreSQL support. The frontend will be React with Vite; we rejected Next.js because server-side rendering is unnecessary for this prototype. ESP32-C6 is our hardware controller; Arduino Uno was rejected because it has no native WiFi or Bluetooth. Claude experimented with self-hosted JWT fallback authentication and it worked, but we are keeping Supabase OAuth as the primary approach. Currently blocked: the Supabase OAuth callback returns an incorrect redirect URL (localhost instead of production), so the login flow cannot be tested end to end.`;

/* ---------- moss page ---------- */
function renderMoss() {
  const last = store.lastAsk;
  const total = store.state?.stats?.total_memories || 0;
  $("mossPanel").innerHTML = `
    <div class="mossRow"><span class="k">Indexable memories in this project</span><span class="v">${total}</span></div>
    <div class="mossRow"><span class="k">Last measured retrieval</span><span class="v ${last ? "mint" : ""}">${last ? last.ms + " ms" : "— ask a question to measure"}</span></div>
    <div class="mossRow"><span class="k">Sources returned</span><span class="v">${last ? last.sources.length : "—"}</span></div>
    <div class="mossRow"><span class="k">Retrieval model</span><span class="v">semantic + keyword (Moss)</span></div>`;
}

/* ---------- what's new ---------- */
let lastSeen = localStorage.getItem("pb_lastseen_" + projectId());
function openWhatsNew() {
  const cutoff = lastSeen ? new Date(lastSeen) : new Date(Date.now() - 864e5);
  const fresh = store.timeline.filter((e) => new Date(e.created_at) > cutoff);
  const counts = { decision: 0, task: 0, blocker: 0, other: 0 };
  fresh.forEach((e) => { if (counts[e.type] != null) counts[e.type]++; else counts.other++; });
  $("wnRange").textContent = `${cutoff === lastSeen ? "Since " + cutoff.toLocaleString() : "Last 24 hours"} · project “${projectId()}”`;
  $("wnBody").innerHTML = fresh.length ? `
    <div class="sumLine violet"><span class="v">${counts.decision}</span> decisions made</div>
    <div class="sumLine mint"><span class="v">${counts.other}</span> project events</div>
    <div class="sumLine coral"><span class="v">${counts.blocker}</span> new blockers</div>
    <hr class="divider" style="margin:12px 0">
    ${fresh.slice(0, 8).map((e) => timelineHTML([e], { withDays: false })).join("")}`
    : emptyState("✓", "You're all caught up.", "No project events since you last checked.");
  $("whatsNewModal").classList.add("open");
}
function closeWhatsNew() {
  $("whatsNewModal").classList.remove("open");
  lastSeen = new Date().toISOString();
  localStorage.setItem("pb_lastseen_" + projectId(), lastSeen);
}
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeWhatsNew(); });

/* ---------- settings ---------- */
$("setAuto").onchange = (e) => {
  localStorage.setItem("pb_auto", e.target.checked ? "1" : "0");
  setAutoRefresh();
};
$("setKbd").onchange = (e) => {
  localStorage.setItem("pb_kbd", e.target.checked ? "1" : "0");
  $("kbdBar").style.display = e.target.checked ? "flex" : "none";
};
$("setProjectBtn").onclick = () => switchProject($("setProject").value.trim());
$("setEngineBtn").onclick = () => {
  const v = $("setEngine").value.trim().replace(/\/$/, "");
  if (v && !/^https?:\/\//.test(v)) { toast("Engine URL must start with http:// or https://", "bad"); return; }
  localStorage.setItem("pb_engine", v || "http://127.0.0.1:8000");
  toast("Engine URL saved — reloading", "", 2200);
  setTimeout(() => location.reload(), 900);
};
if (FILE_PROTOCOL) { const e = $("setEngine"); if (e) e.value = localStorage.getItem("pb_engine") || "http://127.0.0.1:8000"; }
$("reseedBtn").onclick = () => toast("Run `.venv/bin/python -m backend.seed` from the project root to re-seed", "", 6000);

function setAutoRefresh() {
  clearInterval(store.autoTimer);
  if (localStorage.getItem("pb_auto") !== "0") store.autoTimer = setInterval(() => loadAll(true), 60000);
}

/* ---------- project switching ---------- */
function switchProject(p) {
  p = (p || "").trim();
  if (!p) return;
  saveProject(p);
  toast(`Switched to “${p}”`, "", 2200);
  lastSeen = localStorage.getItem("pb_lastseen_" + p);
  loadAll();
}
$("projCard").onclick = () => {
  const v = prompt("Project namespace", projectId());
  if (v && v.trim()) switchProject(v.trim());
};

/* ---------- keyboard ---------- */
const KEYMAP = { 1: "overview", 2: "ask", 3: "activity", 4: "decisions", 5: "tasks", 6: "conflicts", 7: "moss", 8: "ingest", 9: "settings" };
document.addEventListener("keydown", (e) => {
  const typing = /INPUT|TEXTAREA/.test(document.activeElement.tagName);
  if (e.key === "/" && !typing) { e.preventDefault(); show("ask"); }
  if (e.key.toLowerCase() === "n" && !typing) { e.preventDefault(); openWhatsNew(); }
  if (KEYMAP[e.key] && !typing) show(KEYMAP[e.key]);
  if (e.key === "Enter" && document.activeElement === $("askInput")) ask();
  if (e.key === "Enter" && document.activeElement === $("conflictInput")) checkConflict();
});

/* ---------- static wiring ---------- */
$("sbNav").addEventListener("click", (e) => { const b = e.target.closest(".nv[data-view]"); if (b) show(b.dataset.view); });
document.querySelectorAll(".nv[data-view]").forEach((b) => { if (!b.closest("#sbNav")) b.onclick = () => show(b.dataset.view); });
document.querySelectorAll(".modeRow .tab").forEach((t) => {
  t.onclick = () => {
    document.querySelectorAll(".modeRow .tab").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    $("mode-text").style.display = t.dataset.mode === "text" ? "block" : "none";
    $("mode-github").style.display = t.dataset.mode === "github" ? "block" : "none";
  };
});
$("demoFillBtn").onclick = () => { $("ingestText").value = DEMO_STORY; $("ingestAuthor").value = "Jayvee"; };
$("ingestBtn").onclick = () => ingestText();
$("connectBtn").onclick = connectGitHub;
$("repoInput").addEventListener("keydown", (e) => { if (e.key === "Enter") connectGitHub(); });
$("tlRefresh").onclick = () => loadAll(true);
$("whatsNewBtn").onclick = openWhatsNew;
$("setProject") && ($("setProject").value = projectId());

/* prefill ask from blocker CTAs */
document.addEventListener("click", (e) => {
  const p = e.target.closest("[data-prefill]");
  if (p) { $("askInput").value = p.dataset.prefill; ask(p.dataset.prefill); }
});

/* ---------- boot ---------- */
$("setAuto").checked = localStorage.getItem("pb_auto") !== "0";
$("setKbd").checked = localStorage.getItem("pb_kbd") !== "0";
$("kbdBar").style.display = $("setKbd").checked ? "flex" : "none";
renderQPills();
renderConflictPills();
setAutoRefresh();
loadAll();
