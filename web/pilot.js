"use strict";

const $ = (id) => document.getElementById(id);
const AUTOSCROLL_PX = 28;

let promptRevision = null;
let promptVersions = [];
let currentPromptVersion = null;
let selectedPromptVersion = null;
let promptBaseline = { seed: "", step: "", extract: "" };
let lastModel = null;
let source = null;
let currentJob = null;
let memoryQueryTimer = null;
const stepCards = new Map();

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

async function api(path, options = {}) {
  const response = await fetch(path, options);
  let data = {};
  try {
    data = await response.json();
  } catch (_) {
    data = {};
  }
  if (!response.ok) {
    const error = new Error(data.detail || response.statusText || "request failed");
    error.status = response.status;
    error.data = data;
    throw error;
  }
  return data;
}

function notify(message, kind = "info") {
  const box = $("toasts");
  if (!box) return;
  const toast = el("div", `toast ${kind}`, message);
  box.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateY(6px)";
    setTimeout(() => toast.remove(), 250);
  }, kind === "bad" ? 7000 : 3800);
}

// --- drawer ----------------------------------------------------------------
function openDrawer() {
  $("prompts-drawer").classList.add("open");
  $("prompts-drawer").setAttribute("aria-hidden", "false");
  $("drawer-backdrop").classList.add("open");
}

function closeDrawer() {
  $("prompts-drawer").classList.remove("open");
  $("prompts-drawer").setAttribute("aria-hidden", "true");
  $("drawer-backdrop").classList.remove("open");
}

// --- prompts ---------------------------------------------------------------
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

function renderPromptVersions() {
  const select = $("prompt-version");
  select.innerHTML = "";
  for (const version of promptVersions) {
    const option = document.createElement("option");
    option.value = String(version.version);
    option.textContent = `${version.current ? "● " : ""}v${version.version} — ${version.comment || "no comment"} · ${formatVersionDate(version.created)}`;
    select.appendChild(option);
  }
  if (selectedPromptVersion !== null) {
    select.value = String(selectedPromptVersion);
  }
  const current = promptVersions.find((v) => v.current);
  $("version-meta").textContent = current
    ? `Current: v${current.version} — ${current.comment || "no comment"}`
    : "";
}

function setPromptFields(prompts) {
  $("seed-prompt").value = prompts.seed_prompt || "";
  $("step-prompt").value = prompts.step_prompt || "";
  $("extract-prompt").value = prompts.extract_prompt || "";
}

function currentPromptFields() {
  return {
    seed: $("seed-prompt").value,
    step: $("step-prompt").value,
    extract: $("extract-prompt").value,
  };
}

function isPromptDirty() {
  const now = currentPromptFields();
  return (
    now.seed !== promptBaseline.seed ||
    now.step !== promptBaseline.step ||
    now.extract !== promptBaseline.extract
  );
}

async function loadPromptVersion(version) {
  try {
    const record = await api(`/api/pilot/prompts/versions/${version}`);
    setPromptFields(record);
    selectedPromptVersion = version;
    $("version-meta").textContent = `Viewing v${version} — ${record.comment || "no comment"}`;
    if (Number(version) === Number(currentPromptVersion)) {
      $("version-meta").textContent += " (current)";
    }
  } catch (error) {
    notify("Could not load prompt version: " + error.message, "bad");
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
  const button = $("save");
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Saving…";
  try {
    const fields = currentPromptFields();
    const result = await api("/api/pilot/prompts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        seed_prompt: fields.seed,
        step_prompt: fields.step,
        extract_prompt: fields.extract,
        base_revision: promptRevision,
        prompt_comment: comment,
      }),
    });
    promptRevision = result.prompt_revision || promptRevision;
    promptVersions = Array.isArray(result.prompt_versions)
      ? result.prompt_versions
      : promptVersions;
    currentPromptVersion = result.prompt_version ?? currentPromptVersion;
    selectedPromptVersion = currentPromptVersion;
    if (result.prompts) setPromptFields(result.prompts);
    promptBaseline = currentPromptFields();
    $("prompt-comment").value = "";
    renderPromptVersions();
    if (result.changed === false) {
      notify("No prompt changes to save", "info");
    } else {
      notify(`Saved pilot prompt v${currentPromptVersion}`, "ok");
    }
  } catch (error) {
    const message =
      error.status === 409
        ? "Save blocked: pilot prompts changed on the server. Reload, then save again."
        : error.status === 400
          ? error.message
          : "Save failed: " + error.message;
    notify(message, "bad");
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

// --- models ----------------------------------------------------------------
async function loadModels() {
  const select = $("model");
  try {
    const data = await api("/api/models");
    select.innerHTML = "";
    const groups = new Map();
    for (const model of data.models || []) {
      const provider = model.providerID || "unknown";
      if (!groups.has(provider)) groups.set(provider, []);
      groups.get(provider).push(model);
    }
    for (const [provider, models] of [...groups.entries()].sort()) {
      const group = document.createElement("optgroup");
      group.label = provider;
      models.sort((a, b) => String(a.id).localeCompare(String(b.id)));
      for (const model of models) {
        const option = document.createElement("option");
        option.value = `${model.providerID}\u0000${model.id}`;
        option.textContent = model.name || model.id;
        option.dataset.variants = JSON.stringify(model.variants || []);
        group.appendChild(option);
      }
      select.appendChild(group);
    }
    updateVariants();
  } catch (error) {
    select.innerHTML = "";
    const option = document.createElement("option");
    option.textContent = "Model list unavailable — check OpenCode";
    select.appendChild(option);
    notify("Could not load models: " + error.message, "bad");
  }
}

function selectedModelOption() {
  const option = $("model").selectedOptions[0];
  if (!option || !option.value.includes("\u0000")) return null;
  return option;
}

function updateVariants() {
  const option = selectedModelOption();
  const variants = option ? JSON.parse(option.dataset.variants || "[]") : [];
  const select = $("variant");
  const previous = select.value;
  select.innerHTML = "";
  const none = document.createElement("option");
  none.value = "";
  none.textContent = "(default)";
  select.appendChild(none);
  for (const variant of variants) {
    const item = document.createElement("option");
    item.value = variant;
    item.textContent = variant;
    select.appendChild(item);
  }
  if ([...select.options].some((item) => item.value === previous)) {
    select.value = previous;
  }
}

function applyModelSelection(saved) {
  if (!saved || !saved.providerID || !saved.id) return;
  const value = `${saved.providerID}\u0000${saved.id}`;
  const option = [...$("model").options].find((item) => item.value === value);
  if (!option) return;
  option.selected = true;
  updateVariants();
  if (saved.variant) {
    const variant = [...$("variant").options].find((item) => item.value === saved.variant);
    if (variant) variant.selected = true;
  }
}

function modelRef() {
  const option = selectedModelOption();
  if (!option) return null;
  const [providerID, id] = option.value.split("\u0000");
  const variant = $("variant").value || null;
  return variant ? { providerID, id, variant } : { providerID, id };
}

// --- config/health ---------------------------------------------------------
async function loadConfig({ announce = false } = {}) {
  try {
    const config = await api("/api/pilot/config");
    promptRevision = config.prompt_revision || null;
    promptVersions = Array.isArray(config.prompt_versions)
      ? config.prompt_versions
      : [];
    currentPromptVersion =
      Number(config.prompt_version) ||
      promptVersions.find((v) => v.current)?.version ||
      null;
    selectedPromptVersion = currentPromptVersion;
    setPromptFields(config.prompts || {});
    promptBaseline = currentPromptFields();
    $("prompt-comment").value = "";
    renderPromptVersions();
    $("steps").value = config.last_steps || 12;
    $("top-k").value = config.last_top_k || 3;
    lastModel = config.last_model || null;
    applyModelSelection(lastModel);
    renderMemoryStats(config.memory || {});
    if (announce) notify("Reloaded pilot prompts and settings", "ok");
  } catch (error) {
    notify("Could not load pilot config: " + error.message, "bad");
  }
}

async function loadHealth() {
  try {
    const health = await api("/api/health");
    const pill = $("pill-opencode");
    pill.textContent = health.ok ? "opencode: ok" : "opencode: error";
    pill.className = `pill ${health.ok ? "ok" : "bad"}`;
  } catch (_) {
    $("pill-opencode").textContent = "opencode: error";
    $("pill-opencode").className = "pill bad";
  }
  try {
    const data = await api("/api/barygraph");
    const connected = (data.servers || []).some((server) => {
      const status = server.status || {};
      return server.name === "barygraph" && status.status === "connected";
    });
    const pill = $("pill-barygraph");
    pill.textContent = connected ? "barygraph: ready" : "barygraph: unavailable";
    pill.className = `pill ${connected ? "ok" : "bad"}`;
  } catch (_) {
    $("pill-barygraph").textContent = "barygraph: unavailable";
    $("pill-barygraph").className = "pill bad";
  }
}

// --- live trajectory -------------------------------------------------------
function trajectoryNearBottom() {
  const box = $("trajectory");
  return box.scrollHeight - box.scrollTop - box.clientHeight <= AUTOSCROLL_PX;
}

function scrollTrajectory(wasNearBottom) {
  const box = $("trajectory");
  if (wasNearBottom) box.scrollTop = box.scrollHeight;
}

function clearTrajectory() {
  stepCards.clear();
  $("trajectory").innerHTML = "";
}

function ensureStep(number) {
  if (stepCards.has(number)) return stepCards.get(number);
  if (!stepCards.size) {
    const empty = $("trajectory").querySelector(".empty-state");
    if (empty) empty.remove();
  }
  const card = el("article", "step");
  card.dataset.step = String(number);
  card.dataset.status = "running";

  const head = el("div", "step-head");
  const title = el("div", "step-title");
  const numberLabel = el("span", "step-number", `Step ${number}`);
  const stageBadge = el("span", "badge think", "starting");
  title.append(numberLabel, stageBadge);
  const status = el("span", "step-status muted", "running");
  head.append(title, status);

  const body = el("div", "step-body");

  const thoughtDetails = el("details");
  thoughtDetails.open = true;
  thoughtDetails.appendChild(el("summary", "", "Thought"));
  const thoughtPre = el("pre", "thought", "");
  thoughtDetails.appendChild(thoughtPre);

  const reasoningDetails = el("details");
  reasoningDetails.appendChild(el("summary", "", "Reasoning"));
  const reasoningPre = el("pre", "reasoning", "");
  reasoningDetails.appendChild(reasoningPre);

  const usedWrap = el("div", "used-hits");
  usedWrap.appendChild(el("div", "section-label", "Memory used in this step"));

  const constructsWrap = el("div", "constructs");
  constructsWrap.appendChild(el("div", "section-label", "Extracted constructs"));

  const retrievedWrap = el("div", "memory-hits");
  retrievedWrap.appendChild(el("div", "section-label", "Retrieved for next step"));

  const toolsWrap = el("div", "tools");
  toolsWrap.appendChild(el("div", "section-label", "Tools"));

  const warnings = el("div", "warnings");

  // Keep the step focused on the thought and reasoning; the raw memory,
  // construct, and tool data lives in a fold that is closed by default.
  const extraDetails = el("details");
  extraDetails.appendChild(el("summary", "", "Memory, constructs & tools"));
  const extraBody = el("div", "details-body");
  extraBody.append(usedWrap, constructsWrap, retrievedWrap, toolsWrap, warnings);
  extraDetails.appendChild(extraBody);

  body.append(thoughtDetails, reasoningDetails, extraDetails);
  card.append(head, body);
  $("trajectory").appendChild(card);

  const entry = {
    el: card,
    status,
    stageBadge,
    thoughtPre,
    reasoningPre,
    usedWrap,
    constructsWrap,
    retrievedWrap,
    toolsWrap,
    warnings,
    memoryShift: "none",
  };
  stepCards.set(number, entry);
  return entry;
}

function setStage(number, stage) {
  const entry = ensureStep(number);
  entry.stageBadge.textContent = stage === "extract" || stage === "extract-retry" ? "extracting" : "thinking";
  entry.stageBadge.className = `badge ${stage === "extract" || stage === "extract-retry" ? "extract" : "think"}`;
  entry.status.textContent = stage === "extract" || stage === "extract-retry" ? "extracting triads" : "thinking";
}

function appendDelta(number, which, text) {
  if (!text) return;
  const wasNear = trajectoryNearBottom();
  const entry = ensureStep(number);
  const pre = which === "reasoning" ? entry.reasoningPre : entry.thoughtPre;
  pre.textContent += text;
  scrollTrajectory(wasNear);
}

function renderTool(number, name, ok) {
  const entry = ensureStep(number);
  const line = el("div", "tool-line");
  const status = ok === null || ok === undefined ? "…" : ok ? "ok" : "failed";
  line.textContent = `${name || "tool"} — ${status}`;
  entry.toolsWrap.appendChild(line);
  entry.toolsWrap.scrollTop = entry.toolsWrap.scrollHeight;
}

function renderUsedHits(number, hits) {
  const entry = ensureStep(number);
  entry.usedWrap.querySelectorAll(".hit, .empty-state").forEach((node) => node.remove());
  if (!hits || !hits.length) {
    entry.usedWrap.appendChild(el("div", "empty-state", "No memory hit was injected into this step."));
    return;
  }
  for (const hit of hits) entry.usedWrap.appendChild(renderHit(hit));
}

function renderRetrievedHits(number, hits) {
  const entry = ensureStep(number);
  entry.retrievedWrap.querySelectorAll(".hit, .empty-state").forEach((node) => node.remove());
  if (!hits || !hits.length) {
    entry.retrievedWrap.appendChild(el("div", "empty-state", "No matching construct found for the next step."));
    return;
  }
  for (const hit of hits) entry.retrievedWrap.appendChild(renderHit(hit));
}

function renderHit(hit) {
  const box = el("div", "hit");
  const head = el("div", "hit-head");
  const score = Number(hit.score ?? 0).toFixed(2);
  const source = `${hit.run_id || "unknown"} · step ${hit.step ?? "?"}`;
  head.append(el("span", "", `score ${score}`), el("span", "", source));
  box.appendChild(head);
  const body = el("div", "hit-body");
  body.appendChild(el("div", "", `cm1: ${(hit.cm1_terms || []).join(", ")}`));
  body.appendChild(el("div", "", `bridge: ${(hit.bridge_terms || []).join(", ")}`));
  body.appendChild(el("div", "", `cm2: ${(hit.cm2_terms || []).join(", ")}`));
  if (hit.tension) body.appendChild(el("div", "", `tension: ${hit.tension}`));
  if (hit.rationale) body.appendChild(el("div", "", hit.rationale));
  box.appendChild(body);
  if (hit.shared_terms && hit.shared_terms.length) {
    const terms = el("div", "memory-terms");
    for (const term of hit.shared_terms) terms.appendChild(el("span", "term", term));
    box.appendChild(terms);
  }
  return box;
}

function renderConstructs(number, constructs, shift, note, warning) {
  const entry = ensureStep(number);
  entry.constructsWrap.querySelectorAll(".construct, .empty-state, .shift-line").forEach((node) => node.remove());
  entry.memoryShift = shift || "none";
  if (!constructs || !constructs.length) {
    entry.constructsWrap.appendChild(el("div", "empty-state", "No construct was parsed for this step."));
  } else {
    for (const construct of constructs) {
      const box = el("div", "construct");
      const triad = el("div", "triad");
      const rows = [
        ["cm1", (construct.cm1_terms || []).join(", ")],
        ["bridge", (construct.bridge_terms || []).join(", ")],
        ["cm2", (construct.cm2_terms || []).join(", ")],
        ["tension", construct.tension || "—"],
        ["rationale", construct.rationale || "—"],
      ];
      for (const [label, value] of rows) {
        triad.append(el("span", "", label), el("span", "", value));
      }
      box.appendChild(triad);
      entry.constructsWrap.appendChild(box);
    }
  }
  const shiftLine = el("div", "shift-line");
  const badgeClass =
    shift === "redirected" ? "redir" : shift === "reinforced" ? "reinf" : "unused";
  shiftLine.appendChild(el("span", `badge ${badgeClass}`, `memory: ${shift || "none"}`));
  if (note) shiftLine.appendChild(el("span", "muted", ` ${note}`));
  entry.constructsWrap.appendChild(shiftLine);
  entry.warnings.innerHTML = "";
  if (warning) entry.warnings.appendChild(el("div", "warning", warning));
}

function completeStep(number) {
  const entry = ensureStep(number);
  entry.el.dataset.status = "done";
  entry.status.textContent = "complete";
  entry.stageBadge.textContent = "complete";
  entry.stageBadge.className = "badge";
}

function renderStepFromRecord(step) {
  const number = Number(step.step);
  const entry = ensureStep(number);
  entry.thoughtPre.textContent = step.thought || "";
  entry.reasoningPre.textContent = step.reasoning || "";
  renderUsedHits(number, step.memory_hits || []);
  renderConstructs(
    number,
    step.constructs || [],
    step.memory_shift || "none",
    step.shift_note || "",
    step.extract_warning || ""
  );
  renderRetrievedHits(number, step.retrieved_hits || []);
  entry.toolsWrap.querySelectorAll(".tool-line").forEach((node) => node.remove());
  for (const tool of step.tools || []) {
    const line = el("div", "tool-line", `${tool.name || "tool"} — ${tool.status || "called"}`);
    entry.toolsWrap.appendChild(line);
  }
  completeStep(number);
}

function buildConstructBoxes(constructs) {
  const wrap = el("div", "constructs");
  wrap.appendChild(el("div", "section-label", "Final constructs"));
  for (const construct of constructs || []) {
    const box = el("div", "construct");
    const triad = el("div", "triad");
    const rows = [
      ["cm1", (construct.cm1_terms || []).join(", ")],
      ["bridge", (construct.bridge_terms || []).join(", ")],
      ["cm2", (construct.cm2_terms || []).join(", ")],
      ["tension", construct.tension || "—"],
      ["rationale", construct.rationale || "—"],
    ];
    for (const [label, value] of rows) {
      triad.append(el("span", "", label), el("span", "", value));
    }
    box.appendChild(triad);
    wrap.appendChild(box);
  }
  return wrap;
}

function renderFinal(record) {
  if (!record || record._error) return;
  const steps = Array.isArray(record.steps) ? record.steps : [];
  const finalStep = [...steps].reverse().find((step) => (step.thought || "").trim());
  const constructs =
    (finalStep && finalStep.constructs) || record.final_constructs || [];

  const card = el("article", "step final-step");
  card.dataset.status = record._stopped ? "stopped" : "done";
  const head = el("div", "step-head");
  const title = el("div", "step-title");
  title.append(
    el("span", "step-number", "Final answer"),
    el("span", "badge", record._stopped ? "stopped" : "complete")
  );
  head.append(
    title,
    el(
      "span",
      "step-status muted",
      `${steps.length} step${steps.length === 1 ? "" : "s"}`
    )
  );

  const body = el("div", "step-body");

  const answer = el("details");
  answer.open = true;
  answer.appendChild(el("summary", "", "Final thought"));
  answer.appendChild(
    el(
      "pre",
      "thought",
      (finalStep && finalStep.thought) || "No final thought recorded."
    )
  );
  body.appendChild(answer);

  if (constructs.length) body.appendChild(buildConstructBoxes(constructs));

  if (record.trajectory_summary) {
    const summary = el("details");
    summary.appendChild(el("summary", "", "Trajectory summary"));
    summary.appendChild(el("pre", "", record.trajectory_summary));
    body.appendChild(summary);
  }

  card.append(head, body);
  $("trajectory").appendChild(card);
}

function renderRunRecord(record) {
  clearTrajectory();
  if (!record) {
    $("trajectory").appendChild(el("div", "empty-state", "No record in this pilot run."));
    return;
  }
  if (Array.isArray(record.steps) && record.steps.length) {
    for (const step of record.steps) renderStepFromRecord(step);
  }
  if (record._error) {
    const error = el("div", "step");
    error.dataset.status = "error";
    const head = el("div", "step-head");
    head.append(el("div", "step-title", `Error: ${record._error}`));
    const body = el("div", "step-body");
    body.appendChild(el("pre", "", record._message || "Pilot failed."));
    error.append(head, body);
    $("trajectory").appendChild(error);
    return;
  }
  renderFinal(record);
}

// --- pilot run -------------------------------------------------------------
function setRunning(running) {
  $("start").disabled = running;
  $("stop").disabled = !running;
  $("run-state").textContent = running ? "Pilot running…" : "Pilot idle.";
}

function subscribe(jobId) {
  if (source) source.close();
  source = new EventSource(`/api/pilot/run/${jobId}/events`);
  const handlers = {
    "pilot.started": (data) => {
      const job = data.job || {};
      $("run-meta").textContent = `${job.out_path || ""}`;
      $("trajectory-meta").textContent = `${job.total_steps || "?"} steps · ${job.model?.id || ""}`;
    },
    "step.started": (data) => {
      setStage(data.step, "think");
      renderUsedHits(data.step, data.memory_hits || []);
    },
    "text.delta": (data) => appendDelta(data.step, "thought", data.text),
    "reasoning.delta": (data) => appendDelta(data.step, "reasoning", data.text),
    "tool.call": (data) => renderTool(data.step, data.name, null),
    "tool.result": (data) => renderTool(data.step, data.name, data.ok),
    "step.thought": (data) => {
      const entry = ensureStep(data.step);
      entry.thoughtPre.textContent = data.thought || "";
      entry.reasoningPre.textContent = data.reasoning || "";
      setStage(data.step, "extract");
    },
    "step.constructs": (data) => {
      setStage(data.step, "extract");
      renderConstructs(
        data.step,
        data.constructs || [],
        data.memory_shift || "none",
        data.shift_note || "",
        data.warning || ""
      );
    },
    "step.memory": (data) => renderRetrievedHits(data.step, data.hits || []),
    "step.completed": (data) => completeStep(data.step),
    "pilot.waiting": (data) => {
      $("run-state").textContent = `Waiting for step ${data.step} (${data.stage})…`;
    },
    "pilot.retry": (data) => {
      notify(`Rate limit on step ${data.step}; retrying in ${Math.round(data.delay)}s.`, "info");
    },
    "pilot.completed": (data) => {
      setRunning(false);
      $("run-state").textContent =
        data.status === "completed"
          ? "Pilot complete."
          : data.status === "stopped"
            ? "Pilot stopped."
            : `Pilot ${data.status}.`;
      if (data.error) notify(`Pilot error: ${data.error}`, "bad");
      if (data.result?._error) {
        renderRunRecord(data.result);
      } else if (!stepCards.size && data.result) {
        renderRunRecord(data.result);
      } else if (data.result) {
        renderFinal(data.result);
      }
      if (source) {
        source.close();
        source = null;
      }
      loadMemory();
      loadRuns();
    },
  };
  for (const [event, handler] of Object.entries(handlers)) {
    source.addEventListener(event, (message) => {
      try {
        handler(JSON.parse(message.data));
      } catch (error) {
        console.error("pilot event parse failed", error);
      }
    });
  }
  source.onerror = () => {
    if (source && source.readyState === EventSource.CLOSED) {
      setRunning(false);
    }
  };
}

async function startPilot() {
  const question = $("question").value.trim();
  if (question.length < 3) {
    notify("Add a general question first.", "bad");
    $("question").focus();
    return;
  }
  const model = modelRef();
  if (!model) {
    notify("Select a model first.", "bad");
    return;
  }
  if (isPromptDirty()) {
    notify("Save pilot prompt changes with a comment before starting.", "bad");
    openDrawer();
    $("prompt-comment").focus();
    return;
  }
  const steps = Math.max(2, Math.min(50, parseInt($("steps").value, 10) || 12));
  const topK = Math.max(1, Math.min(10, parseInt($("top-k").value, 10) || 3));
  clearTrajectory();
  $("trajectory-meta").textContent = "";
  $("run-meta").textContent = "";
  setRunning(true);
  $("run-state").textContent = "Starting pilot…";
  try {
    const result = await api("/api/pilot/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        question,
        model,
        steps,
        top_k: topK,
        base_revision: promptRevision,
      }),
    });
    promptRevision = result.prompt_revision || promptRevision;
    currentJob = result.job?.id || null;
    $("run-meta").textContent = result.job?.out_path || "";
    $("trajectory-meta").textContent = `${result.job?.total_steps || steps} steps · ${result.job?.model?.id || ""}`;
    subscribe(currentJob);
  } catch (error) {
    setRunning(false);
    $("run-state").textContent = "Pilot failed to start.";
    const message =
      error.status === 409
        ? "Pilot prompts changed on the server. Reload the page before starting."
        : "Failed to start pilot: " + error.message;
    notify(message, "bad");
  }
}

async function stopPilot() {
  if (!currentJob) return;
  try {
    await api(`/api/pilot/run/${currentJob}/stop`, { method: "POST" });
    $("run-state").textContent = "Stopping pilot…";
  } catch (error) {
    notify("Could not stop pilot: " + error.message, "bad");
  }
}

// --- memory ----------------------------------------------------------------
function renderMemoryStats(stats) {
  $("memory-count").textContent = `${stats.count || 0} constructs`;
  $("download-memory").href = "/api/pilot/memory/download";
}

function renderMemoryList(records) {
  const list = $("memory-list");
  list.innerHTML = "";
  if (!records || !records.length) {
    list.appendChild(el("div", "empty-state", "No constructs in memory."));
    return;
  }
  const fragment = document.createDocumentFragment();
  for (const record of records) {
    const box = el("div", "memory-item");
    const meta = el("div", "meta");
    meta.textContent = `${record.model || "unknown"} · ${record.run_id || "seed"} · step ${record.step ?? "?"}`;
    box.appendChild(meta);
    const cm1 = el("div", "line", `cm1: ${(record.cm1_terms || []).join(", ")}`);
    const bridge = el("div", "line", `bridge: ${(record.bridge_terms || []).join(", ")}`);
    const cm2 = el("div", "line", `cm2: ${(record.cm2_terms || []).join(", ")}`);
    box.append(cm1, bridge, cm2);
    if (record.rationale) {
      box.appendChild(el("div", "line", record.rationale.length > 260 ? record.rationale.slice(0, 260) + "…" : record.rationale));
    }
    if (record.terms && record.terms.length) {
      const terms = el("div", "memory-terms");
      for (const term of record.terms.slice(0, 16)) terms.appendChild(el("span", "term", term));
      box.appendChild(terms);
    }
    fragment.appendChild(box);
  }
  list.appendChild(fragment);
}

async function loadMemory() {
  try {
    const query = $("memory-search").value.trim();
    const suffix = query ? `&q=${encodeURIComponent(query)}` : "";
    const data = await api(`/api/pilot/memory?limit=100${suffix}`);
    renderMemoryStats(data.stats || {});
    renderMemoryList(data.records || []);
  } catch (error) {
    notify("Could not load construct memory: " + error.message, "bad");
  }
}

async function seedMemory() {
  const button = $("seed-memory");
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "Seeding…";
  try {
    const result = await api("/api/pilot/memory/seed", { method: "POST" });
    notify(`Seeded ${result.added} constructs from ${result.scanned_files} files.`, "ok");
    renderMemoryStats(result.stats || {});
    await loadMemory();
  } catch (error) {
    notify("Could not seed memory: " + error.message, "bad");
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
}

async function clearMemory() {
  if (!window.confirm("Clear the pilot construct memory? This cannot be undone.")) return;
  try {
    await api("/api/pilot/memory", { method: "DELETE" });
    notify("Pilot memory cleared.", "ok");
    await loadMemory();
  } catch (error) {
    notify("Could not clear memory: " + error.message, "bad");
  }
}

// --- runs ------------------------------------------------------------------
function formatBytes(size) {
  if (!size) return "0 B";
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

async function loadRuns() {
  try {
    const data = await api("/api/pilot/runs");
    const list = $("pilot-runs");
    list.innerHTML = "";
    const runs = data.runs || [];
    if (!runs.length) {
      list.appendChild(el("div", "empty-state", "No pilot runs yet."));
      return;
    }
    for (const run of runs) {
      const row = el("div", "run-row");
      const name = el("span", "name", run.name);
      const actions = el("div", "actions");
      const load = el("button", "secondary small", "Load");
      load.onclick = () => loadRun(run.name);
      const download = el("a", "button-link small", "Download");
      download.href = `/api/pilot/runs/${encodeURIComponent(run.name)}/download`;
      const remove = el("button", "danger small", "Delete");
      remove.onclick = () => deleteRun(run.name);
      actions.append(load, download, remove);
      row.append(name, actions);
      list.appendChild(row);
    }
  } catch (error) {
    notify("Could not load pilot runs: " + error.message, "bad");
  }
}

async function loadRun(name) {
  try {
    const data = await api(`/api/pilot/runs/${encodeURIComponent(name)}`);
    const record = (data.records || [])[0];
    renderRunRecord(record);
    $("run-state").textContent = `Loaded ${name}`;
    $("trajectory-meta").textContent = name;
    if (record?.question) $("question").value = record.question;
    notify(`Loaded pilot run ${name}`, "ok");
  } catch (error) {
    notify("Could not load pilot run: " + error.message, "bad");
  }
}

async function deleteRun(name) {
  if (!window.confirm(`Delete pilot run ${name}?`)) return;
  try {
    await api(`/api/pilot/runs/${encodeURIComponent(name)}`, { method: "DELETE" });
    notify("Pilot run deleted.", "ok");
    await loadRuns();
  } catch (error) {
    notify("Could not delete pilot run: " + error.message, "bad");
  }
}

// --- init ------------------------------------------------------------------
function bindEvents() {
  $("toggle-prompts").onclick = openDrawer;
  $("close-prompts").onclick = closeDrawer;
  $("drawer-backdrop").onclick = closeDrawer;
  $("save").onclick = savePrompts;
  $("reload").onclick = () => loadConfig({ announce: true });
  $("prompt-version").onchange = (event) => loadPromptVersion(Number(event.target.value));
  $("model").onchange = updateVariants;
  $("start").onclick = startPilot;
  $("stop").onclick = stopPilot;
  $("seed-memory").onclick = seedMemory;
  $("clear-memory").onclick = clearMemory;
  $("refresh-runs").onclick = loadRuns;
  $("memory-search").oninput = () => {
    clearTimeout(memoryQueryTimer);
    memoryQueryTimer = setTimeout(loadMemory, 250);
  };
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeDrawer();
  });
}

async function init() {
  bindEvents();
  await loadModels();
  await loadConfig();
  await Promise.all([loadHealth(), loadMemory(), loadRuns()]);
  setRunning(false);
}

init();
