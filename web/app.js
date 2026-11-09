"use strict";

const $ = (id) => document.getElementById(id);
let currentJob = null;
let source = null;
let promptRevision = null;
let promptVersions = [];
let currentPromptVersion = null;
let selectedPromptVersion = null;
let promptBaseline = { system: "", run: "" };
let resultsMode = "live"; // "live" | "history"
let topbarObserver = null;
const cycles = new Map(); // cycle -> {el, reasoning, text, tools}

// --- boot ------------------------------------------------------------------
async function boot() {
  // Wire the UI first: a failed API call must never leave the Save button dead
  // or the run list blank.
  wireControls();
  wireAccordions();
  await Promise.allSettled([loadHealth(), loadModels()]);
  await Promise.allSettled([loadConfig()]);
  await Promise.allSettled([loadRuns(), loadExamples()]);
  await Promise.allSettled([reattachRun()]);
}

function wireControls() {
  $("save").onclick = savePrompts;
  $("reload").onclick = reloadPrompts;
  $("start").onclick = startRun;
  $("stop").onclick = stopRun;
  $("model").onchange = updateVariants;
  $("refresh-runs").onclick = loadRuns;
  $("prompt-version").onchange = loadSelectedPromptVersion;
  $("toggle-prompts").onclick = togglePrompts;
  $("close-prompts").onclick = closePrompts;
  $("drawer-backdrop").onclick = closePrompts;
  $("back-to-live").onclick = backToLive;
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closePrompts();
  });
  wireTabs();
  syncTopbarHeight();
  window.addEventListener("resize", syncTopbarHeight);
  if (typeof ResizeObserver !== "undefined") {
    topbarObserver = new ResizeObserver(syncTopbarHeight);
    topbarObserver.observe(document.querySelector(".topbar"));
  }
}

// Keep the sticky tab bar exactly below the two-row top bar, whose height
// changes when the controls wrap on narrow windows.
function syncTopbarHeight() {
  const bar = document.querySelector(".topbar");
  if (!bar) return;
  document.documentElement.style.setProperty(
    "--topbar-h",
    `${Math.ceil(bar.getBoundingClientRect().height)}px`
  );
}

// --- tabs + drawer ---------------------------------------------------------
function savedTab() {
  try {
    return localStorage.getItem("mb_tab");
  } catch (e) {
    return null;
  }
}

function rememberTab(name) {
  try {
    localStorage.setItem("mb_tab", name);
  } catch (e) {
    // localStorage unavailable; ignore.
  }
}

function wireTabs() {
  const bar = document.querySelector(".tabbar");
  if (!bar) return;
  bar.querySelectorAll(".tab").forEach((btn) => {
    btn.onclick = () => activateTab(btn.dataset.tab);
  });
  activateTab(savedTab() || "live", true);
}

function activateTab(name, silent = false) {
  const bar = document.querySelector(".tabbar");
  if (!bar) return;
  const btn = bar.querySelector(`.tab[data-tab="${name}"]`);
  if (!btn) return;
  bar.querySelectorAll(".tab").forEach((b) => b.classList.toggle("active", b === btn));
  document.querySelectorAll(".tabpane").forEach((p) => {
    p.classList.toggle("active", p.dataset.pane === name);
  });
  if (!silent) rememberTab(name);
}

// --- live vs historical results -------------------------------------------
function showHistoryView(name) {
  resultsMode = "history";
  $("history-name").textContent = `output/runs/${name}`;
  $("history-banner").hidden = false;
  $("history-results").hidden = false;
  $("results").hidden = true;
  $("results-history-badge").hidden = false;
  $("back-to-live").textContent = currentJob ? "Back to live results" : "Close history";
  activateTab("results");
}

function showLiveResults() {
  resultsMode = "live";
  $("history-banner").hidden = true;
  $("history-results").hidden = true;
  $("history-results").innerHTML = "";
  $("results").hidden = false;
  $("results-history-badge").hidden = true;
  const badge = $("results-badge");
  if (badge) badge.hidden = true;
}

function backToLive() {
  showLiveResults();
  activateTab("results");
}

function togglePrompts() {
  const drawer = $("prompts-drawer");
  if (drawer.classList.contains("open")) closePrompts();
  else openPrompts();
}

function openPrompts() {
  $("prompts-drawer").classList.add("open");
  $("prompts-drawer").setAttribute("aria-hidden", "false");
  $("drawer-backdrop").classList.add("show");
}

function closePrompts() {
  $("prompts-drawer").classList.remove("open");
  $("prompts-drawer").setAttribute("aria-hidden", "true");
  $("drawer-backdrop").classList.remove("show");
}

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) {
    let message = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      if (body && body.detail) message = body.detail;
    } catch (e) {
      // Non-JSON error body; keep the status-line message.
    }
    const err = new Error(message);
    err.status = res.status;
    throw err;
  }
  return res.json();
}

function pill(el, ok, label) {
  el.className = "pill " + (ok ? "ok" : "bad");
  el.textContent = label;
}

async function loadHealth() {
  try {
    const h = await api("/api/health");
    pill($("pill-opencode"), h.ok, `opencode: ${h.opencode?.version || "?"}`);
  } catch (e) {
    pill($("pill-opencode"), false, "opencode: down");
  }
  try {
    const b = await api("/api/barygraph");
    const srv = (b.servers || []).find((s) => s.name === "barygraph");
    const status = srv?.status?.status || srv?.status?.state || "unknown";
    pill($("pill-barygraph"), status === "connected", `barygraph: ${status}`);
  } catch (e) {
    pill($("pill-barygraph"), false, "barygraph: ?");
  }
}

async function loadModels() {
  const sel = $("model");
  try {
    const { models } = await api("/api/models");
    sel.innerHTML = "";
    const groups = {};
    for (const m of models) (groups[m.providerID] ??= []).push(m);
    for (const [prov, list] of Object.entries(groups)) {
      const og = document.createElement("optgroup");
      og.label = prov;
      for (const m of list) {
        const opt = document.createElement("option");
        opt.value = `${m.providerID}\u0000${m.id}`;
        opt.textContent = `${m.name} (${m.id})`;
        opt.dataset.variants = JSON.stringify(m.variants || []);
        og.appendChild(opt);
      }
      sel.appendChild(og);
    }
  } catch (e) {
    sel.innerHTML = "";
    const opt = document.createElement("option");
    opt.textContent = "Model list unavailable — check OpenCode";
    sel.appendChild(opt);
    console.error("Could not load models:", e);
  }
  updateVariants();
}

function updateVariants() {
  const opt = $("model").selectedOptions[0];
  const variants = opt ? JSON.parse(opt.dataset.variants || "[]") : [];
  const sel = $("variant");
  sel.innerHTML = "";
  const none = document.createElement("option");
  none.value = "";
  none.textContent = "(default)";
  sel.appendChild(none);
  for (const v of variants) {
    const o = document.createElement("option");
    o.value = v;
    o.textContent = v;
    sel.appendChild(o);
  }
}

async function loadConfig({ announce = false } = {}) {
  try {
    const c = await api("/api/config");
    promptRevision = c.prompt_revision || null;
    promptVersions = Array.isArray(c.prompt_versions) ? c.prompt_versions : [];
    currentPromptVersion =
      Number(c.prompt_version) ||
      promptVersions.find((v) => v.current)?.version ||
      null;
    selectedPromptVersion = currentPromptVersion;
    const systemPrompt = c.system_prompt || "";
    const runPrompt = c.run_prompt || "";
    $("system-prompt").value = systemPrompt;
    $("run-prompt").value = runPrompt;
    promptBaseline = { system: systemPrompt, run: runPrompt };
    $("prompt-comment").value = "";
    renderPromptVersions();
    applyModelSelection(c.last_model);
    if (announce) notify("Reloaded prompts from server", "ok");
    return true;
  } catch (e) {
    notify("Could not load prompts: " + e.message, "bad");
    console.error("Could not load config:", e);
    return false;
  }
}

// --- prompt version history ------------------------------------------------
function formatVersionDate(value) {
  if (!value) return "unknown time";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString([], {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function isPromptDirty() {
  return (
    $("system-prompt").value !== promptBaseline.system ||
    $("run-prompt").value !== promptBaseline.run
  );
}

function renderVersionMeta() {
  const meta = $("version-meta");
  if (!meta) return;
  const selected = promptVersions.find(
    (v) => v.version === selectedPromptVersion
  );
  if (!selected) {
    meta.textContent = "";
    return;
  }
  const when = formatVersionDate(selected.created);
  const comment = selected.comment || "no comment";
  if (selectedPromptVersion === currentPromptVersion) {
    meta.textContent = `Current: v${selected.version} · ${when} · ${comment}`;
  } else {
    meta.textContent = `Viewing v${selected.version} · ${when} · ${comment} — save with a comment to make it current.`;
  }
}

function renderPromptVersions() {
  const sel = $("prompt-version");
  if (!sel) return;
  sel.innerHTML = "";
  const versions = [...promptVersions].sort((a, b) => b.version - a.version);
  if (!versions.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "No versions yet";
    sel.appendChild(opt);
    sel.disabled = true;
    renderVersionMeta();
    return;
  }
  sel.disabled = false;
  for (const v of versions) {
    const opt = document.createElement("option");
    opt.value = String(v.version);
    const when = formatVersionDate(v.created);
    const marker = v.current ? "● " : "";
    opt.textContent = `${marker}v${v.version} — ${when} — ${v.comment || "(no comment)"}`;
    opt.title = `v${v.version}\n${when}\n${v.comment || "(no comment)"}`;
    sel.appendChild(opt);
  }
  const wanted = selectedPromptVersion ?? currentPromptVersion;
  if (wanted != null && [...sel.options].some((o) => o.value === String(wanted))) {
    sel.value = String(wanted);
  }
  renderVersionMeta();
}

async function loadSelectedPromptVersion() {
  const version = Number($("prompt-version").value);
  if (!version || version === selectedPromptVersion) {
    renderVersionMeta();
    return;
  }
  if (
    isPromptDirty() &&
    !confirm(`Discard unsaved prompt edits and load v${version}?`)
  ) {
    $("prompt-version").value =
      selectedPromptVersion != null ? String(selectedPromptVersion) : "";
    return;
  }
  try {
    const v = await api(`/api/config/prompts/versions/${version}`);
    $("system-prompt").value = v.system_prompt || "";
    $("run-prompt").value = v.run_prompt || "";
    selectedPromptVersion = version;
    renderPromptVersions();
    notify(
      version === currentPromptVersion
        ? `Loaded current prompt v${version}`
        : `Loaded v${version} — save with a comment to make it current`,
      "info"
    );
  } catch (e) {
    $("prompt-version").value =
      selectedPromptVersion != null ? String(selectedPromptVersion) : "";
    notify("Could not load prompt version: " + e.message, "bad");
  }
}

async function reloadPrompts() {
  if (!confirm("Reload prompts from the server? Unsaved edits will be lost.")) return;
  await loadConfig({ announce: true });
}

// Restore the last used model/effort once the catalog is loaded.
function applyModelSelection(model) {
  if (!model || !model.providerID || !model.id) return;
  const value = `${model.providerID}\u0000${model.id}`;
  const sel = $("model");
  if (![...sel.options].some((o) => o.value === value)) return;
  sel.value = value;
  updateVariants();
  if (model.variant) {
    const v = $("variant");
    if ([...v.options].some((o) => o.value === model.variant)) v.value = model.variant;
  }
}

async function savePrompts() {
  if (!isPromptDirty()) {
    notify("No prompt changes to save", "info");
    return;
  }
  const comment = $("prompt-comment").value.trim();
  if (!comment) {
    notify("Add a short version comment describing the change.", "bad");
    $("prompt-comment").focus();
    return;
  }
  const btn = $("save");
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Saving…";
  try {
    const res = await api("/api/config/prompts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        system_prompt: $("system-prompt").value,
        run_prompt: $("run-prompt").value,
        base_revision: promptRevision,
        prompt_comment: comment,
      }),
    });
    promptRevision = res.prompt_revision || promptRevision;
    promptVersions = Array.isArray(res.prompt_versions)
      ? res.prompt_versions
      : promptVersions;
    currentPromptVersion = res.prompt_version ?? currentPromptVersion;
    selectedPromptVersion = currentPromptVersion;
    if (typeof res.system_prompt === "string") {
      $("system-prompt").value = res.system_prompt;
    }
    if (typeof res.run_prompt === "string") {
      $("run-prompt").value = res.run_prompt;
    }
    promptBaseline = {
      system: $("system-prompt").value,
      run: $("run-prompt").value,
    };
    $("prompt-comment").value = "";
    renderPromptVersions();
    if (res.changed === false) {
      notify("No prompt changes to save", "info");
    } else {
      notify(`Saved prompt v${currentPromptVersion}`, "ok");
    }
  } catch (e) {
    const msg =
      e.status === 409
        ? "Save blocked: prompts changed on the server. Click Reload, then save again."
        : e.status === 400
          ? e.message
          : "Save failed: " + e.message;
    notify(msg, "bad");
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

// --- run -------------------------------------------------------------------
async function startRun() {
  const opt = $("model").selectedOptions[0];
  if (!opt || !opt.value.includes("\u0000")) return notify("No model selected", "bad");
  if (isPromptDirty()) {
    notify(
      "Save prompt changes with a version comment before starting a run.",
      "bad"
    );
    openPrompts();
    $("prompt-comment").focus();
    return;
  }
  const [providerID, id] = opt.value.split("\u0000");
  const variant = $("variant").value || null;
  const payload = {
    cycles: parseInt($("cycles").value, 10) || 1,
    model: { providerID, id, variant },
    out_name: $("out-name").value || null,
    base_revision: promptRevision,
  };
  showLiveResults();
  $("live").innerHTML = "";
  $("results").innerHTML = "";
  cycles.clear();
  setRunning(true);
  try {
    const res = await api("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    promptRevision = res.prompt_revision || promptRevision;
    const { job } = res;
    currentJob = job.id;
    rememberJob(job);
    $("file-info").textContent = job.prompt_version
      ? `${job.out_path} · prompt v${job.prompt_version}`
      : job.out_path;
    $("run-state").textContent = `job ${job.id}`;
    subscribe(job.id);
    activateTab("live");
  } catch (e) {
    setRunning(false);
    if (e.status === 409) {
      notify("Prompts changed on the server. Reload the page before starting a run.", "bad");
    } else {
      notify("Failed to start: " + e.message, "bad");
    }
  }
}

function rememberJob(job) {
  try {
    sessionStorage.setItem("mb.currentJob", job.id);
  } catch (e) {
    // Storage can be unavailable; reattachment is a convenience, not a requirement.
  }
}

function forgetJob() {
  try {
    sessionStorage.removeItem("mb.currentJob");
  } catch (e) {
    // Ignore storage failures.
  }
}

// Reattach a reloaded page to a job that is still running server-side. New
// backends can report the active job directly; older ones fall back to the id
// this tab remembered when it started or continued the job.
async function reattachRun() {
  let job = null;
  try {
    const res = await api("/api/run/active");
    job = res.job || null;
  } catch (e) {
    // Older backend without the endpoint; fall through to sessionStorage.
  }
  if (!job) {
    let id = null;
    try {
      id = sessionStorage.getItem("mb.currentJob");
    } catch (e) {
      // Ignore storage failures.
    }
    if (!id) return;
    try {
      job = await api(`/api/run/${encodeURIComponent(id)}`);
    } catch (e) {
      forgetJob();
      return;
    }
  }
  if (!job || (job.status !== "running" && job.status !== "pending")) {
    forgetJob();
    return;
  }
  currentJob = job.id;
  showLiveResults();
  $("live").innerHTML = "";
  $("results").innerHTML = "";
  cycles.clear();
  for (const result of job.results || []) completeCycle(result);
  setRunning(true);
  $("file-info").textContent = job.prompt_version
    ? `${job.out_path} · prompt v${job.prompt_version}`
    : (job.out_path || "");
  $("run-state").textContent = `job ${job.id} (reattached)`;
  rememberJob(job);
  subscribe(job.id);
  activateTab("live");
  notify(`Reattached to running job ${job.id}`, "info");
}

function subscribe(jobId) {
  if (source) source.close();
  source = new EventSource(`/api/run/${jobId}/events`);
  const handlers = {
    "job.started": () => {},
    "cycle.started": (d) => ensureCycle(d.cycle, "running"),
    "reasoning.delta": (d) => addDelta(d.cycle, "reasoning", d.text),
    "text.delta": (d) => addDelta(d.cycle, "text", d.text),
    "tool.call": (d) => addTool(d.cycle, d.name, null),
    "tool.result": (d) => addTool(d.cycle, d.name, d.ok),
    "cycle.completed": (d) => completeCycle(d),
    "cycle.retry": (d) => retryCycle(d),
    "cycle.waiting": (d) => waitingCycle(d),
    "job.completed": (d) => finishJob(d),
  };
  for (const [event, fn] of Object.entries(handlers)) {
    source.addEventListener(event, (e) => fn(JSON.parse(e.data)));
  }
  source.onerror = () => {
    if (source && source.readyState === EventSource.CLOSED) setRunning(false);
  };
}

// Auto-follow: keep the pane pinned to the newest text unless the user has
// scrolled up to read earlier output.
const AUTOSCROLL_PX = 24;

function nearBottom(el) {
  return el.scrollHeight - el.scrollTop - el.clientHeight <= AUTOSCROLL_PX;
}

function keepPinned(el, wasAtBottom) {
  if (el && wasAtBottom) el.scrollTop = el.scrollHeight;
}

function ensureCycle(n, status) {
  if (cycles.has(n)) return cycles.get(n);
  const el = document.createElement("details");
  el.className = "cycle";
  el.dataset.status = status;
  el.innerHTML = `
    <summary><strong>Cycle ${n}</strong><span class="state muted">${status}</span></summary>
    <div class="body">
      <div class="tabs">
        <button data-tab="output" class="active">Output</button>
        <button data-tab="thinking">Thinking</button>
        <button data-tab="tools">Tools</button>
      </div>
      <pre class="tab-pane output"></pre>
      <pre class="tab-pane thinking thinking-pane" hidden></pre>
      <pre class="tab-pane tools" hidden></pre>
    </div>`;
  $("live").appendChild(el);
  const record = {
    el,
    reasoning: "",
    text: "",
    tools: [],
    render(pane) {
      const t = record.el.querySelector(".thinking-pane");
      const o = record.el.querySelector("pre.output");
      const tp = record.el.querySelector("pre.tools");
      if (pane === "thinking") t.textContent = record.reasoning || "(no reasoning captured)";
      if (pane === "output") o.textContent = record.text;
      if (pane === "tools") tp.textContent = record.tools.join("\n") || "(no tools)";
    },
  };
  el.querySelectorAll(".tabs button").forEach((btn) => {
    btn.onclick = () => {
      record.el.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b === btn));
      record.el.querySelectorAll(".tab-pane").forEach((p) => (p.hidden = !p.classList.contains(btn.dataset.tab)));
      record.render(btn.dataset.tab);
      // Opening a tab mid-stream jumps to the latest text.
      keepPinned(el.querySelector(`.tab-pane.${btn.dataset.tab}`), true);
    };
  });
  // Expanding a cycle mid-run should reveal the newest text too.
  el.addEventListener("toggle", () => {
    if (!el.open) return;
    const active = el.querySelector(".tabs button.active");
    if (!active) return;
    keepPinned(el.querySelector(`.tab-pane.${active.dataset.tab}`), true);
  });
  cycles.set(n, record);
  return record;
}

function addDelta(n, kind, text) {
  const c = ensureCycle(n, "running");
  c[kind] += text;
  const pane = kind === "reasoning" ? "thinking" : "output";
  const btn = c.el.querySelector(`.tabs button[data-tab="${pane}"]`);
  if (btn && btn.classList.contains("active")) {
    const paneEl = c.el.querySelector(`.tab-pane.${pane}`);
    const wasAtBottom = paneEl ? nearBottom(paneEl) : true;
    c.render(pane);
    keepPinned(paneEl, wasAtBottom);
  }
}

function addTool(n, name, ok) {
  const c = ensureCycle(n, "running");
  const line = ok === null ? `→ ${name}` : `  ${ok ? "✓" : "✗"} ${name}`;
  c.tools.push(line);
  const btn = c.el.querySelector('.tabs button[data-tab="tools"]');
  if (btn && btn.classList.contains("active")) {
    const paneEl = c.el.querySelector(".tab-pane.tools");
    const wasAtBottom = paneEl ? nearBottom(paneEl) : true;
    c.render("tools");
    keepPinned(paneEl, wasAtBottom);
  }
}

function retryCycle(d) {
  const c = ensureCycle(d.cycle, "running");
  const type = d.error?.type || "rate limited";
  c.text += `\n⚠ ${type}; retrying in ${Math.round(d.delay)}s (attempt ${d.attempt})…\n`;
  c.el.querySelector(".state").textContent = "retrying";
  const btn = c.el.querySelector('.tabs button[data-tab="output"]');
  const paneEl = c.el.querySelector(".tab-pane.output");
  const wasAtBottom = btn && btn.classList.contains("active") && paneEl ? nearBottom(paneEl) : false;
  c.render("output");
  keepPinned(paneEl, wasAtBottom);
  $("run-state").textContent = `cycle ${d.cycle} rate-limited — retrying in ${Math.round(d.delay)}s`;
}

function waitingCycle(d) {
  ensureCycle(d.cycle, "running");
  $("run-state").textContent = `cycle ${d.cycle} — ${d.message || "waiting for provider"} (${d.seconds}s)`;
}

function describeFailure(d) {
  const rec = d.record || {};
  if (rec._error) {
    const message = rec._message ? ` — ${String(rec._message).split("\n")[0]}` : "";
    return `${rec._error}${message}`;
  }
  if (d.error) {
    const status = d.error.status ? ` (${d.error.status})` : "";
    const message = d.error.message ? ` — ${String(d.error.message).split("\n")[0]}` : "";
    return `${d.error.type || "provider error"}${status}${message}`;
  }
  if (d.status === "failed") return "cycle failed with no output";
  return "";
}

function completeCycle(d) {
  const c = ensureCycle(d.cycle, d.status);
  c.el.dataset.status = d.status;
  c.reasoning = d.reasoning || c.reasoning;
  const failure = describeFailure(d);
  if (failure) {
    c.text = d.raw ? `⚠ ${failure}\n\n${d.raw}` : `⚠ ${failure}`;
  } else if (d.raw) {
    c.text = d.raw;
  }
  if (Array.isArray(d.tools)) c.tools = d.tools.map((t) => `${t.status === "completed" ? "✓" : "•"} ${t.name}`);
  c.el.querySelector(".state").textContent = d.status;
  // Render whichever pane is open, keeping it pinned if it was following.
  const active = c.el.querySelector(".tabs button.active");
  const pane = active ? active.dataset.tab : "output";
  const paneEl = c.el.querySelector(`.tab-pane.${pane}`);
  const wasAtBottom = paneEl ? nearBottom(paneEl) : true;
  c.render(pane);
  keepPinned(paneEl, wasAtBottom);
  renderResult(d);
  $("run-state").textContent = `cycle ${d.cycle} ${d.status}`;
}

function renderResult(d, opts = {}) {
  // Live records always go to #results. If the user is reading a historical
  // run, flag the Results tab so they know new live records are waiting there.
  if (!opts.container && resultsMode === "history") {
    const badge = $("results-badge");
    if (badge) badge.hidden = false;
  }
  const box = document.createElement("details");
  box.className = "cycle";
  box.dataset.status = d.status;
  if (opts.exampleIndex !== undefined) box.dataset.exampleIndex = opts.exampleIndex;

  const summary = document.createElement("summary");
  const strong = document.createElement("strong");
  strong.textContent = d.title || `Record ${d.cycle}`;
  const state = document.createElement("span");
  state.className = "muted";
  state.textContent = d.status;
  summary.append(strong, state);

  const actions = document.createElement("span");
  actions.className = "row-actions";
  if (opts.exampleIndex !== undefined) {
    const rm = document.createElement("button");
    rm.className = "danger";
    rm.textContent = "Remove";
    rm.title = "Remove this example";
    rm.onclick = (e) => {
      e.preventDefault();
      e.stopPropagation();
      removeExample(opts.exampleIndex);
    };
    actions.appendChild(rm);
  } else if (d.object && !d.object._error) {
    const add = document.createElement("button");
    add.className = "secondary";
    add.textContent = "☆ Example";
    add.title = "Add this record to examples.jsonl";
    add.onclick = (e) => {
      e.preventDefault();
      e.stopPropagation();
      addExample(d, add);
    };
    actions.appendChild(add);
  }
  summary.append(actions);

  const body = document.createElement("div");
  body.className = "body";
  const pre = document.createElement("pre");
  const obj = d.object || d.record || (d.error
    ? { _error: d.error.type, _message: d.error.message, _status: d.error.status }
    : { _raw: d.raw });
  pre.textContent = JSON.stringify(obj, null, 2);
  body.appendChild(pre);

  box.append(summary, body);
  (opts.container || $("results")).appendChild(box);
}

async function addExample(d, btn) {
  const record = d.object;
  if (!record) return;
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Adding…";
  }
  try {
    const res = await api("/api/examples", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ record }),
    });
    if (res.duplicate) {
      notify("Already in examples", "info");
      if (btn) {
        btn.textContent = "★ Saved";
        btn.disabled = false;
      }
      return;
    }
    notify("Added to examples", "ok");
    if (btn) {
      btn.textContent = "★ Saved";
      btn.disabled = false;
    }
    activateTab("examples");
    await loadExamples();
    // Reveal the record that was just stored.
    const items = $("examples").querySelectorAll("details");
    const last = items[items.length - 1];
    if (last) {
      last.open = true;
      last.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  } catch (e) {
    if (btn) {
      btn.textContent = "☆ Example";
      btn.disabled = false;
    }
    notify("Could not add example: " + e.message, "bad");
  }
}

async function removeExample(index) {
  if (!confirm("Remove this example?")) return;
  try {
    await api(`/api/examples/${index}`, { method: "DELETE" });
    notify("Example removed", "info");
    loadExamples();
  } catch (e) {
    notify("Could not remove example: " + e.message, "bad");
  }
}

async function loadExamples() {
  const box = $("examples");
  box.textContent = "Loading examples…";
  try {
    const { records } = await api("/api/examples");
    box.innerHTML = "";
    $("examples-count").textContent = records.length ? `(${records.length})` : "";
    if (!records.length) {
      box.textContent = "No examples yet — open a record and click ☆ Example.";
      return;
    }
    records.forEach((rec, i) => {
      renderResult(
        {
          cycle: i + 1,
          title: `Example ${i + 1}`,
          status: rec && rec._error ? "failed" : "example",
          object: rec,
          record: rec,
          raw: "",
        },
        { container: box, exampleIndex: i }
      );
    });
  } catch (e) {
    box.textContent = "Could not load examples: " + e.message;
    console.error("Could not load examples:", e);
  }
}

function finishJob(d) {
  forgetJob();
  setRunning(false);
  $("run-state").textContent = `job ${d.status}${d.error ? ": " + d.error : ""}`;
  if (source) { source.close(); source = null; }
  if (d.out_path) {
    $("file-info").textContent = d.prompt_version
      ? `${d.out_path} · prompt v${d.prompt_version}`
      : d.out_path;
  }
  if (d.status === "completed") {
    if (resultsMode === "history") {
      notify("Run completed — live results are ready", "ok");
    } else {
      notify("Run completed", "ok");
      activateTab("results");
    }
  } else if (d.status === "stopped") {
    notify("Run stopped", "info");
  } else {
    notify(`Run ${d.status}${d.error ? ": " + d.error : ""}`, "bad");
  }
  loadRuns();
}

function stopRun() {
  if (currentJob) api(`/api/run/${currentJob}/stop`, { method: "POST" }).catch(() => {});
}

function setRunning(on) {
  $("start").disabled = on;
  $("stop").disabled = !on;
}

// --- history ---------------------------------------------------------------
async function loadRuns() {
  const box = $("runs");
  box.textContent = "Loading runs…";
  try {
    const { runs, directory } = await api("/api/runs");
    $("runs-dir").textContent = directory ? directory.replace(/^.*\/output\//, "output/") : "";
    box.innerHTML = "";
    if (!runs.length) { box.textContent = "No runs yet."; return; }
    for (const r of runs) {
      const div = document.createElement("div");
      div.className = "run-item";
      const a = document.createElement("a");
      a.textContent = r.name;
      a.title = "Open this run";
      a.onclick = () => loadRun(r.name);
      const size = document.createElement("span");
      size.className = "muted";
      size.textContent = `${(r.size / 1024).toFixed(1)} KiB`;
      const progress = document.createElement("span");
      progress.className = "muted";
      const recordCount = Number.isFinite(r.records) ? r.records : null;
      if (recordCount !== null) {
        progress.textContent = r.cycles
          ? `${recordCount}/${r.cycles} cycles`
          : `${recordCount} record${recordCount === 1 ? "" : "s"}`;
      }
      const dl = document.createElement("a");
      dl.className = "download";
      dl.textContent = "Download";
      dl.title = `Download ${r.name}`;
      dl.href = `/api/runs/${encodeURIComponent(r.name)}/download`;
      dl.setAttribute("download", r.name);
      const del = document.createElement("button");
      del.className = "danger";
      del.textContent = "Delete";
      del.title = `Delete ${r.name}`;
      del.onclick = () => deleteRun(r.name);
      div.append(a, size, progress);
      if (r.resumable) {
        const cont = document.createElement("button");
        cont.textContent = "Continue";
        cont.title = r.cycles
          ? `Resume from cycle ${r.records + 1} to ${r.cycles}`
          : `Resume from cycle ${r.records + 1}`;
        cont.onclick = () => continueRun(r);
        div.appendChild(cont);
      }
      div.append(dl, del);
      box.appendChild(div);
    }
  } catch (e) {
    box.textContent = "Could not load runs: " + e.message;
    console.error("Could not load runs:", e);
  }
}

async function deleteRun(name) {
  if (!confirm(`Delete ${name}? This cannot be undone.`)) return;
  try {
    await api(`/api/runs/${encodeURIComponent(name)}`, { method: "DELETE" });
    notify("Run deleted", "info");
    loadRuns();
  } catch (e) {
    notify("Could not delete run: " + e.message, "bad");
  }
}

async function continueRun(run) {
  const records = Number(run.records) || 0;
  const fromInput = parseInt($("cycles").value, 10) || 1;
  const suggested = run.cycles && run.cycles > records ? run.cycles : records + fromInput;
  const answer = window.prompt(
    `Continue ${run.name} from cycle ${records + 1}.\nTarget total cycles:`,
    String(suggested)
  );
  if (answer === null) return;
  const target = parseInt(answer, 10);
  if (!target || target <= records) {
    notify(`Target must be greater than ${records}.`, "bad");
    return;
  }
  let model = run.model && run.model.id ? run.model : null;
  if (!model) {
    const opt = $("model").selectedOptions[0];
    if (!opt || !opt.value.includes("\u0000")) return notify("No model selected", "bad");
    const [providerID, id] = opt.value.split("\u0000");
    model = { providerID, id, variant: $("variant").value || null };
  }
  showLiveResults();
  $("live").innerHTML = "";
  $("results").innerHTML = "";
  cycles.clear();
  setRunning(true);
  try {
    const res = await api(`/api/runs/${encodeURIComponent(run.name)}/continue`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model, cycles: target, base_revision: promptRevision }),
    });
    promptRevision = res.prompt_revision || promptRevision;
    const { job } = res;
    currentJob = job.id;
    rememberJob(job);
    $("file-info").textContent = job.prompt_version
      ? `${job.out_path} · prompt v${job.prompt_version}`
      : job.out_path;
    $("run-state").textContent = `job ${job.id} (continuing)`;
    subscribe(job.id);
    activateTab("live");
  } catch (e) {
    setRunning(false);
    if (e.status === 409) {
      notify("Prompts changed on the server. Reload the page before continuing.", "bad");
    } else {
      notify("Could not continue: " + e.message, "bad");
    }
  }
}

async function loadRun(name) {
  try {
    const data = await api(`/api/runs/${encodeURIComponent(name)}`);
    const box = $("history-results");
    box.innerHTML = "";
    if (!data.records.length) {
      box.textContent = "This run has no records.";
    } else {
      data.records.forEach((obj, i) =>
        renderResult(
          {
            cycle: i + 1,
            status: obj && obj._error ? "failed" : "completed",
            object: obj,
            record: obj,
            raw: "",
          },
          { container: box }
        )
      );
    }
    // Do not touch #results or #file-info: those belong to the live run.
    showHistoryView(name);
  } catch (e) {
    notify("Could not open run: " + e.message, "bad");
  }
}

// Keep each section an accordion: at most one record open per section.
function wireAccordions() {
  for (const id of ["live", "results", "history-results", "examples"]) {
    const box = $(id);
    if (!box) continue;
    // `toggle` does not bubble, so listen in the capture phase.
    box.addEventListener(
      "toggle",
      (e) => {
        const el = e.target;
        if (!(el instanceof HTMLDetailsElement) || !el.open) return;
        for (const other of box.querySelectorAll("details")) {
          if (other !== el && other.open) other.open = false;
        }
      },
      true
    );
  }
}

// --- utils -----------------------------------------------------------------
function escapeHtml(s) {
  return s.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}

// Standard notification view used across the app.
function notify(msg, kind = "info") {
  const box = $("toasts");
  if (!box) return;
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = msg;
  box.appendChild(el);
  requestAnimationFrame(() => el.classList.add("show"));
  setTimeout(() => {
    el.classList.remove("show");
    setTimeout(() => el.remove(), 200);
  }, 3200);
}

let flashTimer = null;
function flash(msg) {
  const el = $("run-state");
  el.textContent = msg;
  clearTimeout(flashTimer);
  flashTimer = setTimeout(() => (el.textContent = ""), 2000);
}

boot().catch((e) => alert("Startup error: " + e.message));
