/* Fold dashboard.
 * Realtime data comes straight from Firestore (Firebase JS SDK, config served by Firebase Hosting).
 * Actions call the Cloud Run API through the Hosting rewrite at /api and /v1.
 * If Firebase isn't available (e.g. local dev), it falls back to polling /api/state.
 */
const API = "";
const $ = (id) => document.getElementById(id);
const fmtUsd = (v) => {
  v = v || 0;
  if (v === 0) return "$0";
  if (v < 0.01) return "$" + v.toFixed(5);
  if (v < 1) return "$" + v.toFixed(4);
  return "$" + v.toLocaleString(undefined, { maximumFractionDigits: 2 });
};
const fmtN = (v) => (v || 0).toLocaleString();
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ORDER = ["invoice_extract", "material_normalize", "complaint_reply"];
const STATUS = {
  llm: ["llm", "On Gemini"],
  compiled: ["compiled", "Folded into code"],
  not_compilable: ["not_compilable", "Kept on Gemini: creative"],
};
const ROUTE = { compiled: ["compiled", "folded code"], llm: ["llm", "Gemini"], fallback: ["fallback", "fallback → Gemini"] };

let db = null;
const state = { global: {}, tasks: {}, logs: [] };
const jobWatchers = {};
let seenLogs = new Set();
let drawerTask = null;   // which task the open drawer belongs to

async function api(path, body) {
  const r = await fetch(API + path, {
    method: body === undefined ? "GET" : "POST",
    headers: { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!r.ok) throw new Error((await r.text()) || r.statusText);
  return r.json();
}

/* ---------------- data sources ---------------- */
async function initFirebase() {
  try {
    const r = await fetch("/__/firebase/init.json");
    if (!r.ok) throw new Error("no firebase config");
    firebase.initializeApp(await r.json());
    db = firebase.firestore();
    db.doc("meta/global").onSnapshot((s) => { state.global = s.data() || {}; renderKpis(); });
    db.collection("tasks").onSnapshot((qs) => {
      qs.forEach((d) => (state.tasks[d.id] = d.data()));
      renderTasks(); renderKpis();
    });
    db.collection("logs").orderBy("ts", "desc").limit(14).onSnapshot((qs) => {
      state.logs = qs.docs.map((d) => ({ id: d.id, ...d.data() }));
      renderLogs();
    });
    setLive(true, "live · Firestore");
    return true;
  } catch (e) {
    return false;
  }
}

async function poll() {
  try {
    const s = await api("/api/state");
    state.global = s.global || {};
    (s.tasks || []).forEach((t) => (state.tasks[t.id] = t));
    state.logs = (s.logs || []).map((l, i) => ({ id: l.ts + ":" + i, ...l }));
    renderKpis(); renderTasks(); renderLogs();
    setLive(true, "live · polling");
  } catch (e) {
    setLive(false, "API unreachable");
  }
}

function watchJob(jobId, cb) {
  if (db) {
    const unsub = db.doc("jobs/" + jobId).onSnapshot((s) => {
      const j = s.data();
      if (!j) return;
      cb(j);
      if (j.state === "done" || j.state === "error") unsub();
    });
  } else {
    const t = setInterval(async () => {
      const j = await api("/api/jobs/" + jobId).catch(() => null);
      if (!j) return;
      cb(j);
      if (j.state === "done" || j.state === "error") clearInterval(t);
    }, 800);
  }
}

function setLive(on, txt) {
  $("liveDot").classList.toggle("on", on);
  $("liveTxt").textContent = txt;
}

/* ---------------- render ---------------- */
function renderKpis() {
  const g = state.global;
  $("kCalls").textContent = fmtN(g.calls);
  $("kShare").textContent = g.calls ? Math.round((100 * (g.compiled_calls || 0)) / g.calls) + "%" : "0%";
  $("kSpend").textContent = fmtUsd(g.cost_usd);
  $("kSaved").textContent = fmtUsd(g.saved_usd);
  $("kEnergy").textContent = (g.energy_wh_saved || 0).toFixed(1) + " Wh";
  $("kShadow").textContent = g.shadow_checks ? `${g.shadow_agree}/${g.shadow_checks}` : "–";
  const avg = g.llm_calls ? g.cost_usd / g.llm_calls : 0;
  const share = g.calls ? (g.compiled_calls || 0) / g.calls : 0;
  $("kProj").textContent = fmtUsd(avg * share * 1e6) + " / month";
}

function renderTasks() {
  const el = $("tasks");
  const ids = ORDER.filter((id) => state.tasks[id]);
  el.innerHTML = ids.map((id) => taskCard(state.tasks[id])).join("");
  ids.forEach((id) => {
    const q = (sel) => el.querySelector(`[data-task="${id}"] ${sel}`);
    q(".sim")?.addEventListener("click", () => simulate(id));
    q(".fold")?.addEventListener("click", () => compileTask(id));
    q(".view")?.addEventListener("click", () => showCompiled(id));
    q(".revert")?.addEventListener("click", () => revert(id));
  });
  if (!$("pTask").options.length) {
    $("pTask").innerHTML = ids.map((id) => `<option value="${id}">${esc(state.tasks[id].name)}</option>`).join("");
    loadSample();
  }
}

function taskCard(t) {
  const s = t.stats || {};
  const calls = s.calls || 0;
  const pc = calls ? (100 * (s.compiled_calls || 0)) / calls : 0;
  const pf = calls ? (100 * (s.fallback_calls || 0)) / calls : 0;
  const pl = calls ? 100 - pc - pf : 0;
  const [cls, label] = STATUS[t.status] || STATUS.llm;
  const busy = jobWatchers[t.id] || "";
  const llmLogged = s.llm_calls || 0;
  let note = "";
  if (t.status === "compiled")
    note = `<div class="note">Verified on unseen calls: <b>${((t.precision || 0) * 100).toFixed(1)}%</b> agreement, handles <b>${Math.round((t.coverage || 0) * 100)}%</b> of traffic. The rest still go to Gemini.</div>`;
  else if (t.status === "not_compilable")
    note = `<div class="note">Gemini judged this creative work, so Fold correctly left it on the LLM.</div>`;
  else if (t.reverted_reason) note = `<div class="note">${esc(t.reverted_reason)}</div>`;
  else note = `<div class="note">${llmLogged < 30 ? `Needs ${30 - llmLogged} more Gemini calls before it can be folded.` : "Enough traffic logged. Ready to fold."}</div>`;
  return `<article class="card task" data-task="${t.id}">
    <span class="chip ${cls}">${label}</span>
    <h3>${esc(t.name)}</h3>
    <p>${esc(t.description)}</p>
    <div class="mini">
      <div><span>Calls</span><span>${fmtN(calls)}</span></div>
      <div><span>By code</span><span>${Math.round(pc)}%</span></div>
      <div><span>Spend</span><span>${fmtUsd(s.cost_usd)}</span></div>
      <div><span>Saved</span><span>${fmtUsd(s.saved_usd)}</span></div>
    </div>
    <div class="bar" aria-label="Routing split"><i class="b-c" style="width:${pc}%"></i><i class="b-f" style="width:${pf}%"></i><i class="b-l" style="width:${pl}%"></i></div>
    <div class="legend"><span><i style="background:var(--code)"></i>code</span><span><i style="background:var(--warn)"></i>fallback</span><span><i style="background:var(--llm)"></i>Gemini</span></div>
    ${note}
    <div class="progress">${esc(busy)}</div>
    <div class="actions">
      <button class="ghost sim" ${busy ? "disabled" : ""}>Send 60 production calls</button>
      ${t.status === "compiled"
        ? `<button class="primary view">View folded code</button><button class="ghost revert">Revert</button>`
        : `<button class="accent fold" ${busy || llmLogged < 30 ? "disabled" : ""}>Fold it</button>`}
      ${t.status === "not_compilable" ? `<button class="ghost view">See analysis</button>` : ""}
    </div>
  </article>`;
}

function renderLogs() {
  const body = $("logBody");
  if (!state.logs.length) { body.innerHTML = `<tr><td colspan="6" class="muted">No requests yet.</td></tr>`; return; }
  const fresh = new Set();
  body.innerHTML = state.logs.map((l) => {
    const [cls, label] = ROUTE[l.route] || ROUTE.llm;
    const isNew = !seenLogs.has(l.id);
    if (isNew) fresh.add(l.id);
    const name = state.tasks[l.task_id]?.name || l.task_id;
    return `<tr class="${isNew && seenLogs.size ? "flash" : ""}">
      <td>${new Date(l.ts * 1000).toLocaleTimeString()}</td><td>${esc(name)}</td>
      <td><span class="chip ${cls}">${label}</span></td>
      <td class="num">${fmtN(l.latency_ms)} ms</td><td class="num">${fmtUsd(l.cost_usd)}</td>
      <td class="inp" title="${esc(l.input)}">${esc(l.input)}</td></tr>`;
  }).join("");
  state.logs.forEach((l) => seenLogs.add(l.id));
}

/* ---------------- actions ---------------- */
async function simulate(id) {
  jobWatchers[id] = "Sending traffic…";
  renderTasks();
  try {
    const { job_id } = await api(`/api/tasks/${id}/simulate`, { n: 60 });
    watchJob(job_id, (j) => {
      const r = j.routes || {};
      jobWatchers[id] = j.state === "done" ? "" : `${j.done || 0}/${j.total} sent · code ${r.compiled || 0} · Gemini ${(r.llm || 0) + (r.fallback || 0)}`;
      if (!db) poll();
      renderTasks();
    });
  } catch (e) {
    jobWatchers[id] = "Error: " + e.message;
    renderTasks();
    setTimeout(() => { delete jobWatchers[id]; renderTasks(); }, 4000);
  }
}

async function compileTask(id) {
  jobWatchers[id] = "Folding…";
  renderTasks();
  openDrawer(`Folding: ${state.tasks[id].name}`, `<p class="muted">Starting…</p>`, id);
  const { job_id } = await api(`/api/tasks/${id}/compile`, {});
  watchJob(job_id, (j) => {
    if (drawerTask === id && $("drawer").classList.contains("open")) $("dBody").innerHTML = compileView(j);
    if (j.state === "done" || j.state === "error") { delete jobWatchers[id]; if (!db) poll(); renderTasks(); }
  });
}

function compileView(j) {
  const steps = (j.steps || []).map((s) => `<li><span class="ic ${s.status}"></span><div><b>${esc(s.name)}</b><span>${esc(s.detail || "")}</span></div><em>${s.t}s</em></li>`).join("");
  let verdict = "";
  if (j.state === "error") verdict = `<div class="verdict keep"><b>Couldn't fold yet</b>${esc(j.error)}</div>`;
  else if (j.verdict === "compiled") verdict = `<div class="verdict ok"><b>Folded. This task now runs as free code.</b>
      Folding it cost ${fmtUsd(j.compile_cost_usd)} in Gemini calls, once.</div>
      <div class="stats3"><div><span>Agreement on unseen calls</span><strong>${(j.test.precision * 100).toFixed(1)}%</strong></div>
      <div><span>Traffic it handles</span><strong>${Math.round(j.test.coverage * 100)}%</strong></div>
      <div><span>Saving per 1M calls</span><strong>${fmtUsd(j.projected)}</strong></div></div>`;
  else if (j.verdict === "kept_on_llm") verdict = `<div class="verdict keep"><b>Kept on Gemini</b>This task needs real generation. Fold won't fake it with code.</div>`;
  else if (j.verdict === "rejected") verdict = `<div class="verdict keep"><b>Didn't pass the quality gate</b>The task stays on Gemini. Send more traffic and try again.</div>`;
  const an = j.analysis ? `<h4>Gemini's analysis</h4><pre class="json">${esc(JSON.stringify(j.analysis, null, 2))}</pre>` : "";
  const code = j.code ? `<h4>Generated function</h4><pre class="code"><code>${esc(j.code)}</code></pre>` : "";
  return `<ul class="timeline">${steps}</ul>${verdict}${an}${code}`;
}

async function showCompiled(id) {
  const d = await api(`/api/compiled/${id}`);
  const t = state.tasks[id];
  if (!d || !d.analysis) { openDrawer(t.name, `<p class="muted">Nothing folded yet.</p>`, id); return; }
  const test = d.test || {};
  const body = (d.verdict === "kept_on_llm"
      ? `<div class="verdict keep"><b>Kept on Gemini</b>${esc(d.analysis.strategy || "")}</div>`
      : `<div class="stats3"><div><span>Agreement on unseen calls</span><strong>${((test.precision || 0) * 100).toFixed(1)}%</strong></div>
         <div><span>Traffic it handles</span><strong>${Math.round((test.coverage || 0) * 100)}%</strong></div>
         <div><span>Saving per 1M calls</span><strong>${fmtUsd(d.projected_monthly_saving_usd_per_1m_calls)}</strong></div></div>`) +
    `<h4>Gemini's analysis</h4><pre class="json">${esc(JSON.stringify(d.analysis, null, 2))}</pre>` +
    (d.code ? `<h4>Folded function (v${d.version})</h4><pre class="code"><code>${esc(d.code)}</code></pre>` : "");
  openDrawer(t.name, body, id);
}

async function revert(id) {
  await api(`/api/tasks/${id}/revert`, {});
  if (!db) poll();
}

async function loadSample() {
  const id = $("pTask").value;
  if (!id) return;
  const s = await api(`/api/tasks/${id}/sample`).catch(() => ({ input: "" }));
  $("pInput").value = s.input;
}

async function runPlayground() {
  const btn = $("pRun");
  btn.disabled = true;
  $("pResult").innerHTML = `<p class="muted">Routing…</p>`;
  try {
    const r = await api("/v1/run", { task: $("pTask").value, input: $("pInput").value });
    const [cls, label] = ROUTE[r.route] || ROUTE.llm;
    $("pResult").innerHTML = `<div class="meta"><span class="chip ${cls}">${label}</span>
      <span>${fmtN(r.latency_ms)} ms</span><span>${fmtUsd(r.cost_usd)}</span>${r.model ? `<span class="muted">${esc(r.model)}</span>` : ""}</div>
      <pre class="json">${esc(JSON.stringify(r.output, null, 2))}</pre>`;
    if (!db) poll();
  } catch (e) {
    $("pResult").innerHTML = `<p class="muted">Error: ${esc(e.message)}</p>`;
  } finally {
    btn.disabled = false;
  }
}

function openDrawer(title, html, taskId = null) {
  drawerTask = taskId;
  $("dTitle").textContent = title;
  $("dBody").innerHTML = html;
  $("drawer").classList.add("open");
  $("drawer").setAttribute("aria-hidden", "false");
}
function closeDrawer() {
  $("drawer").classList.remove("open");
  $("drawer").setAttribute("aria-hidden", "true");
}

/* ---------------- boot ---------------- */
$("pSample").addEventListener("click", loadSample);
$("pTask").addEventListener("change", loadSample);
$("pRun").addEventListener("click", runPlayground);
$("dClose").addEventListener("click", closeDrawer);
$("drawer").addEventListener("click", (e) => { if (e.target.id === "drawer") closeDrawer(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
$("apiBase").textContent = location.origin;

(async () => {
  const ok = await initFirebase();
  if (!ok) { poll(); setInterval(poll, 2000); }
})();
