"use strict";
// buildctl console. Renders the kernel's read model; pulls levers through the console server,
// which runs the buildctl CLI. Nothing here decides what is allowed: the model's levers do.

const S = {
  token: "", model: null, fingerprint: "", route: { view: "plan", arg: null },
  tab: {}, open: {}, typed: {}, results: {}, stamps: {}, busy: {},
  audit: [], auditSeq: 0, ledger: { phase: "", event: "" },
  cells: {}, trustDiff: null, trustDiffKey: "", artifact: null, error: null,
  activity: null, activityKey: "", recordRow: null, decision: { answer: "" },
  docs: null, docsKey: "", docOpen: {},
  drafts: null, draftsKey: "", draftRun: "", draftMilestone: "",
};
const PRIMARY = ["start", "recover", "pause", "resume", "approve", "decide", "grant", "retry", "reconcile-accept", "reconcile-retry"];
const STOP_LEVERS = ["approve", "decide", "grant", "retry", "reconcile-accept", "reconcile-retry", "recover"];
// The decide lever carries the operator's ruling: one of these answers, and text for "own".
const ANSWERS = [
  { id: "finding-stands", label: "The finding stands", hint: "Codex makes the changes the findings require." },
  { id: "code-stands", label: "The code stands", hint: "The findings close. The phase is reviewed once more, then commits." },
  { id: "own", label: "My own ruling", hint: "Codex implements what you write below." },
];
const SIDES = { implementer: "Codex, for the code", reviewer: "Claude, for the findings" };
const SETUP_LEVERS = ["trust", "isolation", "auth", "bootstrap"];
const ASPECT_WORDS = { danger: "stopped", caution: "waiting for you", clear: "clear", unknown: "outcome unknown", occupied: "in progress", pending: "not started" };

// ---------- utilities ----------
const $ = (sel, root = document) => root.querySelector(sel);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const sig = (aspect, label, cls = "") =>
  `<svg class="sig ${cls}" role="img" aria-label="${esc(label || ASPECT_WORDS[aspect] || aspect)}"><use href="#sig-${aspect}"/></svg>`;
const ico = (name, cls = "ico") => `<svg class="${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const short = (value, n = 12) => (value ? String(value).slice(0, n) : "none");
const money = (value) => `$${Number(value || 0).toFixed(2)}`;
const pad = (n) => String(n).padStart(2, "0");

function ago(iso) {
  if (!iso) return "never";
  const seconds = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m ago`;
  return `${Math.floor(seconds / 86400)}d ${Math.floor((seconds % 86400) / 3600)}h ago`;
}
function span(fromIso, toIso) {
  const ms = Math.max(0, (toIso ? Date.parse(toIso) : Date.now()) - Date.parse(fromIso));
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${pad(m)}m` : `${m}m ${pad(s % 60)}s`;
}
function clock(iso) {
  const d = new Date(iso);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
function day(iso) {
  return new Date(iso).toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric" });
}
function announce(text) { $("#announce").textContent = text; }

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "X-Buildctl-Token": S.token, ...(options.body ? { "Content-Type": "application/json" } : {}) },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok && !(response.status === 409 && body.command)) {
    const error = new Error(body.error || `HTTP ${response.status}`);
    error.status = response.status; error.body = body;
    throw error;
  }
  return { status: response.status, body };
}

// ---------- model ----------
function phaseByNumber(n) { return S.model.phases.find((p) => p.number === n); }
function leverKey(lever) {
  const i = lever.argv.indexOf("--operation");
  return i >= 0 ? `${lever.action}:${lever.argv[i + 1]}` : lever.action;
}
function leverNumber(lever) { return lever.number; }
function leverOperation(lever) { const i = lever.argv.indexOf("--operation"); return i >= 0 ? lever.argv[i + 1] : null; }

function buildAspect(m) {
  const st = m.state;
  if (m.state_error || !m.audit.ok) return { aspect: "danger", word: "State unreadable", sub: m.state_error || m.audit.error };
  if (!m.bootstrapped) return { aspect: "pending", word: "Not set up", sub: "finish setup to create the build" };
  const unknown = (m.levers || []).some((l) => l.action === "reconcile-retry");
  if (st.status === "running" && m.lock.held) {
    const current = m.current ? `phase ${m.current.number} · ${m.current.step.replace(/_/g, " ")}` : "";
    return { aspect: "occupied", word: "Running", sub: current };
  }
  if (st.status === "running") return { aspect: "unknown", word: "Run died", sub: "no live process holds the build" };
  if (unknown) return { aspect: "unknown", word: "Outcome unknown", sub: m.escalation?.code };
  if (st.pending_approval || m.escalation?.aspect === "caution") return { aspect: "caution", word: "Waiting for you", sub: m.escalation?.code };
  if (st.status === "human_escalation" || st.status === "failed") return { aspect: "danger", word: "Stopped", sub: m.escalation?.code };
  if (st.status === "paused") return { aspect: "caution", word: "Paused", sub: "no new work starts" };
  if (st.status === "complete") return { aspect: "clear", word: "Complete", sub: `${st.completed_phases.length} phases` };
  return { aspect: "clear", word: "Idle", sub: `${m.phases.length - st.completed_phases.length} phases to run` };
}

// ---------- shell ----------
function renderBar(m) {
  document.body.classList.toggle("is-draft", !!m.draft);
  if (m.draft) return renderDraftBar(m);
  const repoName = m.repo.path.split("/").filter(Boolean).pop();
  $("#bar-repo").innerHTML = `<strong>${esc(repoName)}</strong><span>${esc(m.repo.branch || "detached")} @ ${esc(short(m.repo.head_sha, 7))}</span>`;
  const a = buildAspect(m);
  $("#bar-aspect").innerHTML = `${sig(a.aspect, a.word, "lg")}<span><b>${esc(a.word)}</b><br><span class="mono">${esc(a.sub || "")}</span></span>`;
  const spent = m.budget.spent_today, limit = m.budget.limit || 0;
  const ratio = limit ? Math.min(1, spent / limit) : 0;
  const iso = m.isolation;
  const isoAspect = iso.fresh ? "clear" : iso.verified_at ? "caution" : "danger";
  const lock = m.lock.held ? `pid ${m.lock.pid}` : "free";
  $("#bar-facts").innerHTML = `
    ${m.demo ? `<div><dd><span class="demo-flag" title="Synthetic state written by buildctl-ui --demo">DEMO BUILD</span></dd></div>` : ""}
    <div><dt>Spend today</dt><dd>${money(spent)} <span class="muted">of ${money(limit)}</span></dd>
      <span class="meter ${ratio > 0.95 ? "over" : ratio > 0.75 ? "hot" : ""}" role="img" aria-label="${Math.round(ratio * 100)} percent of the daily limit"><i data-w="${ratio}"></i></span></div>
    <div class="opt"><dt>Sandbox</dt><dd>${sig(isoAspect, iso.fresh ? "verified" : "not verified")} ${iso.verified_at ? esc(ago(iso.verified_at)) : "never verified"}</dd></div>
    <div class="opt"><dt>Trust</dt><dd>${sig(m.trust.ok ? "clear" : "danger", m.trust.ok ? "trusted" : "not trusted")} ${m.trust.ok ? `<code>${esc(short(m.trust.governance_sha, 8))}</code>` : "not trusted"}</dd></div>
    <div><dt>Lock</dt><dd>${m.lock.held ? sig("occupied", "held") : ""}${esc(lock)}</dd></div>`;
  for (const i of document.querySelectorAll(".meter i")) i.style.width = `${Number(i.dataset.w) * 100}%`;

  const flag = $("#stop-flag");
  flag.hidden = !m.escalation;
  const waiting = m.escalation?.aspect === "caution";
  flag.className = `rail-flag ${waiting ? "caution" : ""}`;
  flag.setAttribute("aria-label", m.escalation ? `${waiting ? "waiting for you" : "stopped"}: ${m.escalation.code}` : "");
  const drafts = $("#drafts-flag");
  drafts.hidden = !m.drafts_waiting;
  drafts.setAttribute("aria-label", m.drafts_waiting ? `${m.drafts_waiting} drafts waiting for you` : "");
  const root = m.control_root.split("/").filter(Boolean);
  $("#rail-foot").innerHTML = `<span title="${esc(m.control_root)}">State in <code>…/${esc(root.slice(-2).join("/"))}</code></span><br>Generation ${m.state.generation}`;
  for (const link of document.querySelectorAll(".rail a")) {
    if (link.dataset.route === S.route.view) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  document.title = `${a.word}${a.sub && a.aspect !== "clear" ? ` · ${a.sub}` : ""} · buildctl`;
}

// ---------- the phase line ----------
function renderLine(m) {
  const phases = m.phases;
  if (!phases.length) return `<div class="line empty"><p>No phases: build.yaml could not be read.</p></div>`;
  const W = Math.max(300, ($("#main")?.clientWidth || 1100) - 66);
  const narrow = W < 600;
  const left = 14, right = 14, trackY = 92;
  // The cleared stretch from phase 0 collapses into one counted segment, so the live part of
  // the line and its milestones always fit.
  let cleared = 0;
  while (cleared < phases.length && phases[cleared].aspect === "clear") cleared++;
  const collapse = cleared >= 3;
  const units = collapse ? 4 : 0;
  const rest = phases.slice(collapse ? cleared : 0);
  const unitW = (W - left - right) / (units + rest.length);
  const current = phases.find((p) => !["clear", "pending"].includes(p.aspect));
  const parts = [];
  let x = left;
  if (collapse) {
    const x1 = x + units * unitW;
    parts.push(`<line class="track-done" x1="${x}" y1="${trackY}" x2="${x1 - 1.5}" y2="${trackY}"/>`);
    parts.push(`<line class="joint" x1="${x}" y1="${trackY - 6}" x2="${x}" y2="${trackY + 6}"/>`);
    parts.push(`<text class="num on" x="${(x + x1) / 2}" y="${trackY - 12}" text-anchor="middle">${phases[0].number}–${phases[cleared - 1].number} clear</text>`);
    parts.push(`<rect class="hit" x="${x}" y="${trackY - 16}" width="${x1 - x}" height="32" tabindex="0" role="link" data-goto="#/phases"
      aria-label="Phases ${phases[0].number} to ${phases[cleared - 1].number}, ${cleared} clear"><title>${cleared} phases clear</title></rect>`);
    x = x1;
  }
  const glyphSize = unitW < 12 ? 10 : 14;
  let station = 0;
  rest.forEach((p, i) => {
    const x0 = x + i * unitW, x1 = x0 + unitW;
    const cls = p.aspect === "clear" ? "track-done" : p.aspect === "pending" ? "track-todo" : `track-${p.aspect}`;
    parts.push(`<line class="${cls}" x1="${x0 + 1.5}" y1="${trackY}" x2="${x1 - 1.5}" y2="${trackY}"/>`);
    parts.push(`<line class="joint" x1="${x0}" y1="${trackY - 6}" x2="${x0}" y2="${trackY + 6}"/>`);
    if (p.checkpoint) {
      const approvedOrPast = p.aspect === "clear" || (p === current && !m.state.pending_approval);
      const aspect = p === current && m.state.pending_approval ? "caution" : approvedOrPast ? "clear" : "danger";
      parts.push(`<line class="post" x1="${x0 + 1}" y1="${trackY - 7}" x2="${x0 + 1}" y2="${trackY - 34}"/>`);
      parts.push(`<use href="#sig-${aspect}" x="${x0 + 1 - glyphSize / 2}" y="${trackY - 34 - glyphSize}" width="${glyphSize}" height="${glyphSize}"/>`);
    }
    const every = narrow ? 10 : 5;
    if (p.number % every === 0 || p === current || i === rest.length - 1) {
      parts.push(`<text class="num ${p === current ? "on" : ""}" x="${x0 + unitW / 2}" y="${trackY + 22}" text-anchor="middle">${p.number}</text>`);
    }
    for (const name of p.milestones) {
      const y = trackY + 40 + (station % 2) * 14;
      parts.push(`<rect class="station-mark" x="${x1 - 5}" y="${trackY - 5}" width="10" height="10"/>`);
      if (station % 2) parts.push(`<line class="joint" x1="${x1}" y1="${trackY + 8}" x2="${x1}" y2="${y - 22}"/>`);
      parts.push(`<text class="station" x="${x1}" y="${y}" text-anchor="middle">${esc(name.toUpperCase())} · ${p.number}</text>`);
      station++;
    }
    parts.push(`<rect class="hit" x="${x0}" y="${trackY - 16}" width="${unitW}" height="32" tabindex="0" role="link" data-phase="${p.number}"
      aria-label="Phase ${p.number}, ${esc(p.name)}, ${ASPECT_WORDS[p.aspect]}"><title>${p.number} · ${esc(p.name)} · ${ASPECT_WORDS[p.aspect]}${p.cost_usd ? ` · ${money(p.cost_usd)}` : ""}</title></rect>`);
  });
  parts.push(`<line class="joint" x1="${W - right}" y1="${trackY - 6}" x2="${W - right}" y2="${trackY + 6}"/>`);
  if (current) {
    const i = rest.indexOf(current);
    const mid = x + i * unitW + unitW / 2;
    const half = Math.min(170, W / 2 - 4);
    const anchorX = Math.min(Math.max(mid, half), W - half);
    const op = currentOperation(m);
    const name = current.name;
    parts.push(`<line class="post" x1="${mid}" y1="34" x2="${mid}" y2="${trackY - (collapse ? 26 : 10)}" stroke-dasharray="2 3"/>`);
    parts.push(`<text class="here" x="${anchorX}" y="14" text-anchor="middle">${current.number} · ${esc(name)}</text>`);
    parts.push(`<text class="here-sub" x="${anchorX}" y="28" text-anchor="middle">${esc(ASPECT_WORDS[current.aspect])} · attempt ${current.attempts}${op ? ` · ${esc(op.kind)}` : ""}</text>`);
  }
  return `<figure class="line" aria-label="Phase line: ${m.state.completed_phases.length} of ${phases.length} phases clear">
    <svg viewBox="0 0 ${W} 160" width="${W}" height="160">${parts.join("")}</svg>
    <figcaption class="line-legend">
      <span>${sig("clear")} clear</span><span>${sig("occupied")} in progress</span><span>${sig("caution")} waiting for you</span>
      <span>${sig("danger")} stopped, or a checkpoint at danger</span><span>${sig("unknown")} outcome unknown</span>
      <span>Signals stand before checkpoint phases. Squares on the line are milestones.</span>
    </figcaption></figure>`;
}

function currentOperation(m) {
  const n = m.state.current_phase;
  if (n == null) return null;
  // State stores operations keyed by id, so they arrive alphabetically; "last" means last to start.
  const ops = (phaseByNumber(n)?.operations || []).slice().sort((a, b) => String(a.started_at || "").localeCompare(String(b.started_at || "")));
  return ops.find((o) => o.status === "started") || ops.find((o) => o.status === "unknown") || ops[ops.length - 1] || null;
}

// ---------- levers ----------
function renderLevers(levers, { emptyText = "No levers apply here." } = {}) {
  if (!levers.length) return `<div class="interlock"><p class="empty">${esc(emptyText)}</p></div>`;
  return `<div class="interlock">${levers.map(renderLever).join("")}</div>`;
}

function buttonLabel(lever) {
  return { approve: "Review and sign", decide: "Decide…", grant: "Authorize…", "reconcile-accept": "Accept…", "reconcile-retry": "Authorize retry…", trust: "Pin this commit…" }[lever.action] || lever.label;
}

function renderLever(lever) {
  const key = leverKey(lever);
  const blocked = lever.conditions.filter((c) => c.held === false);
  const locked = !lever.enabled;
  const busy = S.busy[key];
  const opens = lever.confirm || lever.action === "decide";
  const button = `<button class="btn ${opens ? "quiet" : ""} ${busy ? "busy" : ""}" data-lever="${esc(key)}"
      ${locked ? `disabled aria-describedby="lock-${esc(key)}"` : ""} ${S.open[key] ? 'aria-expanded="true"' : opens ? 'aria-expanded="false"' : ""}>
      ${busy ? "Running…" : esc(buttonLabel(lever))}</button>`;
  const conds = lever.conditions.length ? `<ul class="conds">${lever.conditions.map((c) => {
    const aspect = c.held === true ? "clear" : c.held === false ? "danger" : "unknown";
    const word = c.held === true ? "held" : c.held === false ? "blocked" : "checked at call time";
    return `<li class="${c.held === false ? "blocked" : ""}">${sig(aspect, word)}<span>${esc(c.label)}</span><span class="detail">${esc(c.detail || "")}</span></li>`;
  }).join("")}</ul>` : "";
  const note = locked ? `<p class="lock-note" id="lock-${esc(key)}">${ico("lock")}Locked by ${blocked.length === 1 ? "one condition" : `${blocked.length} conditions`}: ${esc(blocked.map((c) => c.label.toLowerCase()).join("; "))}.</p>` : "";
  const tags = [lever.spends ? `<span class="tag">paid</span>` : "", lever.detached ? `<span class="tag">runs detached</span>` : ""].join(" ");
  return `<article class="lever ${locked ? "locked" : ""}" id="lever-${esc(key)}">
    <div class="lever-plate"><b aria-label="Lever ${leverNumber(lever)}">${pad(leverNumber(lever))}</b>${locked ? ico("lock") : ""}</div>
    <div class="lever-body">
      <div class="lever-head"><div><h3>${esc(lever.label)} ${tags}</h3><p>${esc(lever.summary)}</p></div>${button}</div>
      ${conds}${note}${S.open[key] && lever.enabled ? (lever.action === "decide" ? renderDecision(key) : renderCeremony(lever, key)) : ""}${renderResult(key)}
    </div></article>`;
}

function renderCeremony(lever, key) {
  const c = lever.confirm;
  const value = String(c.value);
  const want = c.length ? value.slice(0, c.length) : value;
  const evidence = c.kind === "tree_hash" || c.kind === "sha"
    ? `<mark>${esc(want)}</mark>${esc(value.slice(want.length))}` : `<mark>${esc(value)}</mark>`;
  const typed = S.typed[key] || "";
  const match = typed.trim().toLowerCase() === want.toLowerCase();
  const what = { tree_hash: "Tree hash under review", sha: "Commit to pin", phase: "Phase", operation: "Operation" }[c.kind] || "Evidence";
  const sign = { approve: "Sign and approve", grant: "Sign the grant", trust: "Sign and pin", "reconcile-accept": "Accept output", "reconcile-retry": "Authorize retry" }[lever.action] || "Confirm";
  return `<div class="ceremony" role="group" aria-label="Confirm ${esc(lever.label)}">
    <div><div class="muted">${esc(what)}</div><div class="evidence">${evidence}</div></div>
    <div class="ceremony-row">
      <div class="field"><label for="typed-${esc(key)}">${esc(c.prompt)}</label>
        <input id="typed-${esc(key)}" data-typed="${esc(key)}" value="${esc(typed)}" autocomplete="off" spellcheck="false"
          aria-invalid="${typed && !match ? "true" : "false"}" aria-describedby="hint-${esc(key)}">
        <span class="hint" id="hint-${esc(key)}">${typed && !match ? "Does not match the evidence yet." : "The console sends the nonce it showed you, so a stale approval is refused."}</span></div>
      <button class="btn sign" data-sign="${esc(key)}" ${match ? "" : "disabled"}>${esc(sign)}</button>
      <button class="btn quiet" data-cancel="${esc(key)}">Cancel</button>
    </div></div>`;
}

function decisionReady() {
  const answer = S.decision.answer;
  return Boolean(answer) && (answer !== "own" || (S.typed["decide-text"] || "").trim().length > 0);
}

function renderDecision(key) {
  const text = S.typed["decide-text"] || "";
  const own = S.decision.answer === "own";
  return `<div class="ceremony" role="group" aria-label="Decide the debate">
    <fieldset class="answers"><legend>Your ruling</legend>
      ${ANSWERS.map((a) => `<label class="answer"><input type="radio" name="decide-answer" data-answer="${a.id}" ${S.decision.answer === a.id ? "checked" : ""}>
        <span><b>${esc(a.label)}</b><span class="hint">${esc(a.hint)}</span></span></label>`).join("")}
    </fieldset>
    <div class="field"><label for="typed-decide-text">${own ? "Your ruling" : "A note for the record (optional)"}</label>
      <textarea id="typed-decide-text" data-typed="decide-text" rows="4" spellcheck="true">${esc(text)}</textarea>
      <span class="hint">It becomes an ADR marked "Decided by: operator". The console sends the nonce it showed you, so a decision against a build that has moved is refused.</span></div>
    <div class="ceremony-row">
      <button class="btn sign" data-sign="${esc(key)}" ${decisionReady() ? "" : "disabled"}>Record my decision</button>
      <button class="btn quiet" data-cancel="${esc(key)}">Cancel</button>
    </div></div>`;
}

function debatePanel(d) {
  const proposal = (p) => p ? `<div class="proposal"><span class="muted">Proposes</span> ${esc(p.decision)}
    <ul>${(p.finding_dispositions || []).map((f) => `<li><code>${esc(short(f.finding_id))}</code> ${esc(f.disposition)}</li>`).join("")}</ul></div>` : "";
  const findings = d.findings.length ? `<h3>Debated findings</h3><ul class="debated">${d.findings.map((f) =>
    `<li><b>${esc(f.severity)}</b> <code>${esc(f.path)}</code> ${esc(f.claim)}<br><span class="muted">Asks for:</span> ${esc(f.required_change)}</li>`).join("")}</ul>` : "";
  const turns = d.turns.map((t) => `<li class="turn ${esc(t.side)}"><div class="turn-head"><b>Turn ${t.turn}</b> ${esc(SIDES[t.side] || t.side)}${t.accepted ? ` <span class="tag">accepted the proposal</span>` : ""}</div>
    <p class="argument">${esc(t.argument)}</p>${proposal(t.proposal)}</li>`).join("");
  const state = d.awaiting_decision ? "The agents did not agree. Your decision is waiting in the levers."
    : d.answer ? `You decided: ${esc((ANSWERS.find((a) => a.id === d.answer) || {}).label || d.answer)}.` : "";
  return `<div class="panel-pad">${state ? `<p>${state}</p>` : ""}${findings}<h3>Debate ${d.number}</h3><ol class="turns">${turns}</ol>
    <p class="muted">Agent text, shown as written. Each turn is also in What agents did.</p></div>`;
}

function renderResult(key, { stamp: withStamp = true } = {}) {
  const stamp = withStamp && S.stamps[key];
  const r = S.results[key];
  let html = "";
  if (stamp) html += `<div class="stamp fresh" role="img" aria-label="Signed by you">SIGNED <span>${esc(stamp)}</span></div>`;
  if (r) html += `<div class="result ${r.ok ? "" : "bad"}" role="${r.ok ? "status" : "alert"}">${esc(r.text)}${r.output ? `<pre>${esc(r.output)}</pre>` : ""}</div>`;
  return html;
}

async function pull(key) {
  const lever = S.model.levers.find((l) => leverKey(l) === key);
  if (!lever) return;
  S.busy[key] = true; delete S.results[key]; render();
  try {
    const { status, body } = await api("/api/pull", {
      method: "POST",
      body: JSON.stringify({ action: lever.action, generation: S.model.state.generation, confirm: S.typed[key] || "", operation: leverOperation(lever),
        ...(lever.action === "decide" ? { answer: S.decision.answer, text: S.typed["decide-text"] || "" } : {}) }),
    });
    const ok = status < 300;
    const text = body.detached ? `Started ${body.command} as pid ${body.pid}. It keeps running if you close the console.`
      : ok ? `${body.command} finished.` : `${body.command} exited ${body.returncode}.`;
    S.results[key] = { ok, text, output: body.output };
    if (ok && (lever.confirm || lever.action === "decide")) {
      S.stamps[key] = `${lever.label.toLowerCase()} · ${new Date().toLocaleTimeString()}`;
      // The lever disappears once the kernel accepts it, so the receipt lives outside the lever.
      S.signed = { label: lever.label, summary: lever.summary, at: new Date().toLocaleTimeString(), command: body.command };
    }
    if (ok) { S.open[key] = false; S.typed[key] = ""; if (lever.action === "decide") { S.decision = { answer: "" }; S.typed["decide-text"] = ""; } }
    announce(text);
  } catch (error) {
    S.results[key] = { ok: false, text: error.message };
    announce(error.message);
  } finally {
    S.busy[key] = false;
    await refresh(true);
  }
}

// ---------- views ----------
function receipt() {
  const r = S.signed;
  if (!r) return "";
  return `<section class="receipt" role="status" aria-label="Your last signature">
    <div class="stamp fresh" role="img" aria-label="Signed by you">SIGNED <span>${esc(r.at)}</span></div>
    <div><b>${esc(r.label)}</b> <span class="muted">${esc(r.summary)}</span><br><code>${esc(r.command)}</code> ran and the kernel recorded it in the audit chain.</div>
    <button class="btn quiet" data-dismiss-receipt>Dismiss</button></section>`;
}

function stopBlock(m, compact) {
  const e = m.escalation;
  const a = buildAspect(m);
  const cls = a.aspect === "caution" ? "caution" : a.aspect === "unknown" ? "unknown" : "";
  const n = m.state.current_phase;
  const p = n != null ? phaseByNumber(n) : null;
  return `<section class="stop ${cls}" aria-labelledby="stop-code">
    ${sig(a.aspect, a.word, "lg")}
    <div>
      <h2 class="stop-code" id="stop-code">${esc(e.code)}</h2>
      <p class="stop-meaning">${esc(e.meaning)}</p>
      <p class="stop-remedy"><b>What to do:</b> ${esc(e.remedy)} <a href="/docs/#${encodeURIComponent(e.code)}" target="_blank" rel="noopener">${esc(e.code)} in the manual</a></p>
      ${!compact && e.detail && e.code !== "CHECKPOINT_REQUIRED" ? `<p class="stop-detail"><span class="muted">Detail</span> ${esc(e.detail)}</p>` : ""}
      ${compact ? `<p class="stop-remedy"><a href="#/stop">Open the stop: evidence and levers</a></p>` : ""}
    </div>
    ${p ? `<div class="stop-where">Phase <b>${p.number}</b>${esc(p.name)}<br><span class="muted">attempt ${p.attempts} · ${esc(m.state.phase_step.replace(/_/g, " "))}</span></div>` : "<div></div>"}
  </section>`;
}

function opCard(m) {
  const op = currentOperation(m);
  const n = m.state.current_phase;
  if (n == null || !op) {
    const last = m.state.completed_phases.at(-1);
    return `<div class="panel"><div class="empty"><p>No phase is in progress.</p>
      <p>${last != null ? `Phase ${last} was the last to clear.` : "Nothing has run yet."} Start pulls the next phase onto the line.</p></div></div>`;
  }
  const running = op.status === "started" && m.lock.held;
  const aspect = op.status === "unknown" ? "unknown" : op.status === "failed" ? "danger" : running ? "occupied" : op.status === "completed" ? "clear" : "danger";
  const limit = m.config.limits?.max_phase_minutes || 0;
  const deadline = m.current?.deadline_at;
  let deadlineBar = "";
  if (deadline && limit && running) {
    const total = limit * 60000, left = Date.parse(deadline) - Date.now();
    const used = Math.min(1, Math.max(0, 1 - left / total));
    deadlineBar = `<div class="deadline"><span class="muted">Phase deadline</span>
      <div class="bar-track ${left < 0 ? "danger" : ""}" role="img" aria-label="${Math.round(used * 100)} percent of the phase time used"><i data-w="${used}"></i></div>
      <span class="mono muted">${left > 0 ? `${span(new Date().toISOString(), deadline)} left of ${limit}m` : "deadline passed"}</span></div>`;
  }
  const documenter = op.id.includes(":document-") && m.documentation?.documenter;
  return `<div class="panel op">
    <div class="op-head">${sig(aspect, op.status)}<h3>${running ? "In flight" : op.status === "unknown" ? "Outcome unknown" : "Last call"}: ${esc(op.kind)}</h3></div>
    ${stepTrack(phaseByNumber(n), { vertical: true })}
    <dl>${documenter ? `<dt>Role</dt><dd>documenter · ${esc([documenter.model_family, documenter.prompt_lineage].filter(Boolean).join(" · "))}</dd>` : ""}
      <dt>Operation</dt><dd><code>${esc(op.id)}</code></dd>
      <dt>Status</dt><dd>${esc(op.status)}${op.detail_code ? ` · <code>${esc(op.detail_code)}</code>` : ""}</dd>
      <dt>Started</dt><dd>${op.started_at ? `${esc(clock(op.started_at))} · <span data-since="${esc(op.started_at)}" ${op.completed_at ? `data-until="${esc(op.completed_at)}"` : ""}>${esc(span(op.started_at, op.completed_at))}</span>` : "not started"}</dd>
      ${op.output_digest ? `<dt>Output</dt><dd><code>${esc(short(op.output_digest, 16))}</code></dd>` : ""}
    </dl>${activityStrip(m, op)}${deadlineBar || (deadline && !running ? `<p class="muted">Nothing is in flight. A retry starts a new attempt with a fresh ${limit}-minute deadline.</p>` : "")}</div>`;
}

function costList(m) {
  const rows = m.phases.filter((p) => p.cost_usd > 0);
  if (!rows.length) return `<div class="panel"><p class="empty">Nothing spent yet.</p></div>`;
  const max = Math.max(...rows.map((p) => p.cost_usd));
  const recent = rows.slice(-12);
  const live = m.state.status === "running" && m.lock.held;
  return `<div class="panel panel-pad">${recent.length < rows.length ? `<p class="muted costs-head">Latest ${recent.length} of ${rows.length} phases with spend</p>` : ""}
    <ul class="costs" aria-label="Cost by phase">
    ${recent.map((p) => `<li><span class="n">${p.number}</span><span class="b ${live && p.number === m.state.current_phase ? "now" : ""}" data-w="${p.cost_usd / max}"></span><span class="v">${money(p.cost_usd)}</span></li>`).join("")}
  </ul><p class="muted costs-total">${money(m.budget.spent_total)} in total across ${rows.length} phases.</p></div>`;
}

function jobsList(m) {
  if (!m.jobs.length) return "";
  return `<section class="section"><h2>Commands from this console</h2><div class="panel"><ul class="jobs">
    ${m.jobs.slice().reverse().map((j) => `<li><code>${esc(j.command)}</code>
      <span class="st ${j.returncode ? "bad" : ""}">${j.detached ? `pid ${j.pid} · ` : ""}${j.returncode == null ? "running" : `exit ${j.returncode}`}</span></li>`).join("")}
  </ul></div></section>`;
}

// ---------- what agents did ----------
const KIND_WORDS = { launched: "launched", exited: "exited", ran: "ran", edit: "edited", read: "read", search: "searched", tool: "tool", message: "said", reasoning: "thought" };
const SOURCE_WORDS = { observed: "buildctl saw", agent_reported: "agent says" };
const FLAG_WORDS = {
  changed_unreported: "Changed, and the agent never mentioned it",
  changed_possibly_by_command: "Changed with no file report. A command it ran may have written it",
  reported_no_net_change: "The agent reported writing this, but it did not change",
  reviewer_tool_outside_allowlist: "The reviewer used a tool outside its allowed list",
  events_unparsed: "Some of the agent's events could not be read, so this comparison is weaker",
};

function activityPhase(m) {
  if (S.route.view === "phases" && S.route.arg != null && S.route.arg !== "") return Number(S.route.arg);
  return m.state.current_phase;
}

function currentRecord(m, op) {
  if (!S.activity || !op || S.activity.phase !== op.phase) return null;
  return S.activity.operations.filter((o) => o.operation === op.id).at(-1) || null;
}

function usageText(usage) {
  if (!usage) return "";
  const tokens = (usage.input_tokens || 0) + (usage.output_tokens || 0);
  const parts = [];
  if (tokens) parts.push(`${tokens.toLocaleString()} tokens`);
  if (usage.reported_cost_usd != null) parts.push(money(usage.reported_cost_usd));
  return parts.join(" · ");
}

function activityStrip(m, op) {
  if (!m.evidence?.recording) return `<p class="muted">Recording off. No activity feed for this build.</p>`;
  const record = currentRecord(m, op);
  if (!record) return "";
  const open = record.state === "open";
  // Quiet is all it can honestly say: a long test run and a hang look the same from here.
  const quiet = open && record.last_output_at ? `<dt>Last output</dt><dd><span data-since="${esc(record.last_output_at)}"></span> ago</dd>` : "";
  const usage = usageText(record.reported_usage);
  return `<dl class="strip">${quiet}
    <dt>Agent</dt><dd>${esc(record.role)} (${esc(record.cli)})</dd>
    <dt>Reported</dt><dd>${record.files_reported} ${record.files_reported === 1 ? "file" : "files"} changed · ${record.commands_reported} ${record.commands_reported === 1 ? "command" : "commands"} <span class="tag">agent says</span></dd>
    ${usage ? `<dt>Usage</dt><dd>${esc(usage)} <span class="tag">agent says</span></dd>` : ""}
    </dl>`;
}

function flagsList(flags) {
  if (!flags?.length) return "";
  return `<ul class="flags">${flags.map((f) => `<li>${sig("caution", "flag")}<span>${esc(FLAG_WORDS[f.flag] || f.flag)}${f.path || f.tool || f.operation ? `: <code>${esc(f.path || f.tool || f.operation)}</code>` : ""}</span></li>`).join("")}</ul>`;
}

function flagsNote(m) {
  const flags = m.current?.evidence_flags;
  return flags?.length ? `<div class="panel-pad"><p><b>The agent's account and the change disagree</b></p>${flagsList(flags)}</div>` : "";
}

function receiptsNote(m) {
  const e = m.evidence;
  if (!e?.count) return "";
  return `<p class="muted panel-pad source">${e.count} ${e.count === 1 ? "receipt" : "receipts"} on the evidence branch${e.pushed ? ", pushed." : ". Receipts not pushed."}</p>`;
}

function activityPanel(m, number, flags) {
  if (!m.evidence?.recording) return `<p class="empty">Recording is off for this build, so there is no feed and no receipts. Turning it on takes a change to <code>evidence.recording</code> in <code>build.yaml</code> and a re-trust.</p>`;
  const rows = S.activity?.phase === number ? S.activity.rows : [];
  const head = `${receiptsNote(m)}${flags?.length ? `<div class="panel-pad">${flagsList(flags)}</div>` : ""}`;
  if (!rows.length) return `${head}<p class="empty">Nothing recorded for this phase. Its records may have been pruned, or no agent has started.</p>`;
  const selected = S.recordRow;
  return `${head}${S.activity.truncated ? `<p class="muted panel-pad source">Showing the most recent rows.</p>` : ""}${selected ? `<pre class="file record-row" aria-label="Full record for this row">${esc(selected.text)}</pre>` : `<p class="muted panel-pad source">Choose a row to read the full record for it.</p>`}<div class="table-wrap flush feed-wrap"><table class="feed"><thead><tr><th>Time</th><th>Agent</th><th>Did</th><th>What</th><th class="num">Exit</th><th>Source</th></tr></thead><tbody>
    ${rows.slice().reverse().map((r) => `<tr class="row-link" tabindex="0" data-record="${esc(r.record)}" data-seq="${r.seq}" aria-selected="${selected?.record === r.record && selected?.seq === r.seq}">
      <td class="mono">${esc(clock(r.at))}</td><td>${esc(r.role)}</td><td>${esc(KIND_WORDS[r.kind] || r.kind)}</td><td class="mono what">${esc(r.target)}</td>
      <td class="num mono">${r.exit_code ?? ""}</td><td><span class="tag ${r.source === "observed" ? "observed" : ""}">${esc(SOURCE_WORDS[r.source] || r.source)}</span></td></tr>`).join("")}
  </tbody></table></div>`;
}

// ---------- the step track: one phase attempt, drawn like the phase line ----------
const DOC_MODE_WORDS = { required: "documentation required", optional: "documentation optional", off: "documentation off for this phase" };
function stepTrack(p, { vertical = false, note = "" } = {}) {
  const t = p?.track;
  if (!t || !t.steps?.length) return "";
  const head = [`attempt ${t.attempt || 1}`, t.documentation ? DOC_MODE_WORDS[t.documentation] : "", note].filter(Boolean).join(" · ");
  return `<div class="steps${vertical ? " v" : ""}">
    <p class="steps-head"><b>Phase ${p.number}</b><span>${esc(head)}</span></p>
    <ol aria-label="Steps of phase ${p.number}">${t.steps.map((st) => `<li class="${st.aspect === "clear" ? "done" : ""}${st.skipped ? " skipped" : ""}">
      ${sig(st.aspect, st.skipped ? "off" : ASPECT_WORDS[st.aspect])}<span class="name">${esc(st.name)}</span>
      <span class="meta${st.aspect === "danger" ? " blocked" : ""}">${esc(st.meta || "")}</span></li>`).join("")}</ol></div>`;
}

const KIND_LABELS = { added: "added", updated: "updated", removed: "removed", fixed_stale: "fixed stale" };
function diffBlock(path, d) {
  if (!d) return "";
  if (!d.available) return `<p class="muted diff-note">${esc(d.reason)}</p>`;
  return `<div class="diff" role="group" aria-label="Change to ${esc(path)}">${d.lines.map((line) => {
    const mark = line[0], cls = line.startsWith("@@") ? "hunk" : mark === "+" ? "add" : mark === "-" ? "del" : "";
    if (cls === "hunk") {
      const at = /\+(\d+)(?:,(\d+))?/.exec(line), from = at ? Number(at[1]) : null, count = at ? Number(at[2] ?? 1) : 0;
      const where = from == null ? "" : count > 1 ? `lines ${from} to ${from + count - 1}` : `line ${from}`;
      return `<div class="hunk"><i aria-hidden="true">@@</i><span>${esc([where, line.replace(/^@@[^@]*@@ ?/, "")].filter(Boolean).join(" · "))}</span></div>`;
    }
    return `<div class="${cls}"><i aria-hidden="true">${mark === "-" ? "−" : mark === "+" ? "+" : " "}</i><span>${cls === "add" ? '<span class="announce">added: </span>' : cls === "del" ? '<span class="announce">removed: </span>' : ""}${esc(line.slice(1))}</span></div>`;
  }).join("")}${d.truncated ? `<div class="hunk"><i>⋯</i><span>${d.truncated} more lines</span></div>` : ""}</div>`;
}

function changeRow(number, path, d, { aspect = "clear", kind = "", reflects = "" } = {}) {
  const key = `${number}:${path}`;
  const lines = d?.available ? `+${d.added}${d.removed ? ` −${d.removed}` : ""}` : "";
  return `<details class="change" data-doc-open="${esc(key)}" ${S.docOpen[key] ? "open" : ""}>
    <summary>${sig(aspect, aspect === "danger" ? "outside the documentation paths" : "inside the documentation paths")}<span class="path">${esc(path)}</span>
      <span class="kind">${esc(kind)}</span><span class="reflects">${esc(reflects)}</span><span class="lines">${esc(lines)}</span></summary>
    ${diffBlock(path, d)}</details>`;
}

const GATE_NAMES = { links: "Links", changelog: "Changelog", forbidden_phrases: "Forbidden phrases" };
function docGates(gate, mode) {
  if (!gate) return `<p class="empty">The documentation gates have not run.</p>`;
  const failures = Object.fromEntries((gate.documentation?.failures || []).map((f) => [f.gate, f.problems || []]));
  const recorded = mode === "optional";
  const row = (name, ok, why) => `<div class="gate-row${ok ? "" : " failed"}">${sig(ok ? "clear" : "danger", ok ? "passed" : recorded ? "failed, recorded" : "failed")}<span>${esc(name)}</span><span class="why">${esc(why)}${!ok && recorded ? " · recorded, did not stop" : ""}</span></div>`;
  const acceptance = gate.acceptance || {};
  const failedCommands = (acceptance.details || []).filter((d) => d.passed === false);
  return [
    row("Acceptance re-run", acceptance.passed !== false, acceptance.passed === false ? failedCommands.map((d) => d.output || JSON.stringify(d.target)).join(" · ") : "the phase's acceptance commands pass with the new docs"),
    ...Object.entries(GATE_NAMES).map(([id, name]) => row(name, !failures[id], failures[id] ? failures[id].join(" · ") : { links: "every relative link and anchor resolves", changelog: "a user-visible change has a new entry, or none was needed", forbidden_phrases: "no documentation file gained a forbidden phrase" }[id])),
  ].join("");
}

function docsPanel(m, number) {
  const d = S.docs?.phase === number ? S.docs : null;
  if (!d) return `<p class="empty">Reading the documentation record…</p>`;
  const sum = d.summary || {};
  const result = d.results.at(-1), gate = d.gates.at(-1);
  const passes = d.results.length;
  const label = (file) => (file || "").replace(/^DOC_(GATE_)?RESULT-|\.json$/g, "");
  const stale = Boolean(result && gate && label(result.file) !== label(gate.file));
  const facts = [["Mode", sum.mode], ["Documenter", [sum.documenter?.vendor, sum.documenter?.model_family, sum.documenter?.prompt_lineage].filter(Boolean).join(" · ")],
    ["Reviewer", [sum.reviewer?.vendor, sum.reviewer?.model_family].filter(Boolean).join(" · ")],
    ["Passes", passes ? `${passes}${passes > 1 ? ` · ${passes - 1} send-back${passes > 2 ? "s" : ""}` : " · no send-backs"}` : "none yet"],
    ...(result ? [["User-visible", result.user_visible ? "yes" : "no"]] : [])];
  const out = d.out_of_scope.length ? `<h3 class="docs-sub">Outside the documentation paths <small>documentation.paths: ${esc((sum.paths || []).join(", "))}</small></h3>
    ${d.out_of_scope.map((path) => changeRow(number, path, d.diffs[path], { aspect: "danger", kind: (result?.changes || []).some((c) => c.path === path) ? "reported" : "not reported", reflects: "outside the documentation paths" })).join("")}` : "";
  const changes = result?.changes || [];
  const listed = changes.filter((c) => !d.out_of_scope.includes(c.path));
  return `<dl class="docs-facts">${facts.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v || "")}</dd></div>`).join("")}</dl>
    ${out}
    ${result ? `<p class="docs-summary">${inline(result.summary || "")}<small>The documenter's own account, from <code>${esc(result.file)}</code>. Treat it like implementer claims: the changes below are what happened.${result.status === "no_change" ? ` It changed nothing: ${esc(result.reason)}` : ""}</small></p>` : `<p class="empty">The documenter has not reported yet.</p>`}
    ${listed.length ? `<h3 class="docs-sub">${d.out_of_scope.length ? "Reported changes" : "Changes"} <small>${listed.length} file${listed.length > 1 ? "s" : ""} · choose one to read its change</small></h3>
      ${listed.map((c) => changeRow(number, c.path, d.diffs[c.path], { kind: KIND_LABELS[c.kind] || c.kind, reflects: c.reflects })).join("")}` : ""}
    <h3 class="docs-sub">Documentation gates <small>${gate ? `from <code>${esc(gate.file)}</code>` : ""}${sum.mode === "optional" ? " · optional: failures go to the reviewer and do not stop the phase" : ""}</small></h3>
    ${stale ? `<p class="note">These results are from pass ${d.gates.length}, which sent the documenter back. Pass ${passes} stopped before its gates ran.</p>` : ""}
    ${docGates(gate, sum.mode)}`;
}

function viewLine(m) {
  const done = m.state.completed_phases.length;
  const primary = m.levers.filter((l) => PRIMARY.includes(l.action));
  const shown = primary.filter((l) => l.enabled || ["start", "recover"].includes(l.action))
    .sort((a, b) => Number(b.enabled) - Number(a.enabled));
  return `<div class="view-head"><h1>Line</h1><p>${done} of ${m.phases.length} phases clear${m.state.started_at ? ` · started ${esc(ago(m.state.started_at))}` : ""}</p></div>
    ${receipt()}${m.escalation ? stopBlock(m, true) : ""}
    <div class="section">${renderLine(m)}</div>
    <div class="cols section">
      <section><h2 class="h2">Next move <small>Interlocking: a lever unlocks only when every condition holds.</small></h2>${renderLevers(shown)}</section>
      <aside><h2 class="h2">Current call</h2>${opCard(m)}
        <h2 class="h2 gap">Spend by phase</h2>${costList(m)}${jobsList(m)}</aside>
    </div>`;
}

function findingsPanel(m) {
  const review = m.current?.review;
  const register = m.findings.open;
  const items = (review?.findings || []).filter((f) => !["resolved", "superseded"].includes(f.status));
  if (!items.length && !register.length) return `<p class="empty">No open findings.</p>`;
  const list = items.length ? items : register;
  const order = { CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3 };
  list.sort((a, b) => (order[a.severity] ?? 9) - (order[b.severity] ?? 9));
  return `${review ? `<p class="muted panel-pad source">From <code>${esc(review.file)}</code>, verdict <code>${esc(review.verdict)}</code>. ${register.length} open in the register across phases.</p>` : ""}
    ${list.map((f) => `<article class="finding">
      <div><span class="sev sev-${esc(f.severity)}">${esc(f.severity)}</span></div>
      <div><h3>${esc(f.claim)}</h3>
        <p class="where">${esc(f.path)}${f.line ? `:${f.line}` : ""} · ${esc(f.kind)} · ${esc(f.claim_key)}</p>
        <dl>${f.rationale ? `<dt>Why</dt><dd>${esc(f.rationale)}</dd>` : ""}${f.required_change ? `<dt>Change</dt><dd>${esc(f.required_change)}</dd>` : ""}
          <dt>Status</dt><dd>${esc(f.status || "open")} · ${esc(f.treatment || "")}${(f.occurrences || 1) > 1 ? ` · seen ${f.occurrences} times` : ""}</dd></dl></div>
    </article>`).join("")}`;
}

function gatesPanel(m) {
  const gates = m.current?.gates || [];
  if (!gates.length) return `<p class="empty">No gate has run for this attempt.</p>`;
  return `<div class="table-wrap flush"><table><thead><tr><th>Lap</th><th>Check</th><th class="num">Exit</th><th class="num">Seconds</th></tr></thead><tbody>
    ${gates.flatMap((g) => (Array.isArray(g.details) ? g.details : []).map((d) => `<tr>
      <td class="mono">${esc(g.file.replace("GATE_RESULT-", "").replace(".json", ""))}</td>
      <td class="mono">${esc(Array.isArray(d.command) ? d.command.join(" ") : JSON.stringify(d))}</td>
      <td class="num">${sig(d.exit_code === 0 ? "clear" : "danger", d.exit_code === 0 ? "passed" : "failed")} ${esc(d.exit_code ?? "")}</td>
      <td class="num mono">${esc(d.seconds ?? "")}</td></tr>`)).join("")}
  </tbody></table></div>`;
}

function artifactsPanel(m, files) {
  if (!files?.length) return `<p class="empty">No artifacts for this phase.</p>`;
  return `<div class="split"><div class="table-wrap flush"><table><thead><tr><th>File</th><th class="num">Bytes</th></tr></thead><tbody>
    ${files.map((f) => `<tr class="row-link" tabindex="0" data-artifact="${esc(f.path)}" aria-selected="${S.artifact?.path === f.path}"><td class="mono">${esc(f.name)}</td><td class="num mono">${f.bytes.toLocaleString()}</td></tr>`).join("")}
  </tbody></table></div><pre class="file" aria-label="File contents">${S.artifact ? esc(S.artifact.text) : "Choose a file to read it here."}</pre></div>`;
}

function approvalPanel(m) {
  const a = m.state.pending_approval;
  const p = phaseByNumber(a.phase);
  return `<div class="panel-pad"><p>You are signing <b>${esc(a.checkpoint)}</b> for phase ${a.phase}${p ? `, ${esc(p.name)}` : ""}. The approval binds to exactly this scope:</p>
    <dl class="kv"><dt>Checkpoint</dt><dd>${esc(a.checkpoint)}</dd><dt>Phase attempt</dt><dd>${a.phase_attempt}</dd>
      <dt>Tree hash</dt><dd>${esc(a.tree_hash)}</dd><dt>Governance commit</dt><dd>${esc(a.governance_sha)}</dd>
      <dt>Issued at generation</dt><dd>${a.state_generation + 1}</dd><dt>Repository HEAD</dt><dd>${esc(m.repo.head_sha)}</dd></dl>
    <p class="muted spaced">Read the phase prompt and the governed files at that commit before signing. If the tree changes, this approval is refused.</p></div>`;
}

function operationsPanel(ops) {
  if (!ops?.length) return `<p class="empty">No agent calls recorded.</p>`;
  ops = ops.slice().sort((a, b) => String(a.started_at).localeCompare(String(b.started_at)));
  return `<div class="table-wrap flush"><table><thead><tr><th><span class="announce">Status</span></th><th>Operation</th><th>Status</th><th>Started</th><th class="num">Took</th><th>Output</th></tr></thead><tbody>
    ${ops.map((o) => {
      const aspect = { completed: "clear", started: "occupied", unknown: "unknown", failed: "danger" }[o.status] || "pending";
      return `<tr><td>${sig(aspect, o.status)}</td><td class="mono">${esc(o.kind)}</td><td>${esc(o.status)}${o.detail_code ? ` <code>${esc(o.detail_code)}</code>` : ""}</td>
        <td class="mono">${o.started_at ? esc(clock(o.started_at)) : ""}</td><td class="num mono">${o.started_at ? esc(span(o.started_at, o.completed_at)) : ""}</td>
        <td class="mono">${esc(short(o.output_digest, 12))}</td></tr>`;
    }).join("")}</tbody></table></div>`;
}

function tabs(id, entries) {
  const active = S.tab[id] && entries.some((e) => e.id === S.tab[id]) ? S.tab[id] : entries[0].id;
  return `<div class="tabs" role="tablist" aria-label="Evidence">${entries.map((e) => `<button role="tab" id="tab-${id}-${e.id}" aria-controls="panel-${id}"
      aria-selected="${e.id === active}" tabindex="${e.id === active ? 0 : -1}" data-tab="${id}" data-tab-id="${e.id}">${esc(e.label)}${e.count != null ? `<span class="count">${e.count}</span>` : ""}</button>`).join("")}</div>
    <div class="tab-panel" role="tabpanel" id="panel-${id}" aria-labelledby="tab-${id}-${active}">${entries.find((e) => e.id === active).render()}</div>`;
}

function viewStop(m) {
  if (!m.escalation) {
    const a = buildAspect(m);
    return `<div class="view-head"><h1>Stop</h1></div>${receipt()}<div class="panel"><div class="empty">
      <p>${sig(a.aspect, a.word)} Nothing is stopped. The build is ${esc(a.word.toLowerCase())}.</p>
      <p>When buildctl stops, this page explains the code, shows the evidence, and offers only the levers the kernel will accept.</p></div></div>`;
  }
  const n = m.state.current_phase;
  const p = n != null ? phaseByNumber(n) : null;
  const entries = [];
  if (m.state.pending_approval) entries.push({ id: "approval", label: "What you are signing", render: () => approvalPanel(m) + flagsNote(m) });
  const debate = m.current?.debate;
  if (debate?.awaiting_decision) entries.push({ id: "debate-turns", label: "Debate", count: debate.turns.length, render: () => debatePanel(debate) });
  const docCode = /^DOC(UMENTATION)?_/.test(m.escalation.code);
  if (p?.documentation && p.documentation !== "off" && docCode)
    entries.unshift({ id: "docs", label: "Docs", count: m.escalation.code === "DOC_SCOPE_VIOLATION" ? "outside the paths" : null, render: () => docsPanel(m, n) });
  entries.push({ id: "findings", label: "Findings", count: (m.current?.review?.findings || m.findings.open).filter((f) => !["resolved", "superseded", "operator_accepted"].includes(f.status)).length, render: () => findingsPanel(m) });
  if (debate && !debate.awaiting_decision) entries.push({ id: "debate-turns", label: "Debate", count: debate.turns.length, render: () => debatePanel(debate) });
  if (p?.documentation && p.documentation !== "off" && !docCode && p.doc_passes)
    entries.push({ id: "docs", label: "Docs", count: p.doc_passes, render: () => docsPanel(m, n) });
  entries.push({ id: "activity", label: "What agents did", count: m.current?.evidence_flags?.length || null, render: () => activityPanel(m, n, m.current?.evidence_flags) });
  entries.push({ id: "operations", label: "Agent calls", count: p?.operations.length || 0, render: () => operationsPanel(p?.operations) });
  entries.push({ id: "gates", label: "Gates", render: () => gatesPanel(m) });
  entries.push({ id: "artifacts", label: "Artifacts", count: m.current?.artifacts.length || 0, render: () => artifactsPanel(m, m.current?.artifacts) });
  if (m.current?.debate_request) entries.push({ id: "debate", label: "Debate request", render: () => `<p class="panel-pad">Run the debate, write the result to the path the request names, then retry.<br><code>${esc(m.current.debate_request)}</code></p>` });
  const levers = m.levers.filter((l) => STOP_LEVERS.includes(l.action));
  return `<div class="view-head"><h1>Stop</h1><p>Generation ${m.state.generation} · ${esc(ago(m.state.updated_at))}</p></div>
    ${stopBlock(m, false)}
    ${p && p.track?.steps.some((st) => st.aspect !== "pending" || st.skipped) ? `<div class="section">${stepTrack(p)}</div>` : ""}
    <div class="cols section">
      <section><h2 class="h2">Evidence</h2>${tabs("stop", entries)}</section>
      <aside><h2 class="h2">Levers <small>Only what the kernel will accept.</small></h2>${renderLevers(levers, { emptyText: "No lever applies. Fix the cause outside buildctl, then come back." })}</aside>
    </div>`;
}

function viewPhases(m) {
  const selected = S.route.arg != null ? phaseByNumber(Number(S.route.arg)) : null;
  const docs = Boolean(m.documentation);
  const rows = m.phases.map((p) => `<tr class="row-link" tabindex="0" data-goto="#/phases/${p.number}" aria-selected="${selected === p}">
      <td class="num mono">${p.number}</td><td>${sig(p.aspect)}</td><td>${esc(p.name)}${p.checkpoint ? ` <span class="tag">checkpoint</span>` : ""}${p.milestones.map((s) => ` <span class="tag">${esc(s)}</span>`).join("")}</td>
      <td class="num mono" data-cell="att-${p.number}">${p.attempts || ""}</td><td class="num mono" data-cell="rev-${p.number}">${p.reviews || ""}</td>
      <td class="num mono" data-cell="rem-${p.number}">${p.remediations || ""}</td><td class="num mono" data-cell="deb-${p.number}">${p.debates || ""}</td>
      ${docs ? `<td class="num mono${p.documentation === "off" ? " muted" : ""}" data-cell="doc-${p.number}">${p.documentation === "off" ? "off" : p.doc_passes || ""}</td>` : ""}
      <td class="num mono" data-cell="cost-${p.number}">${p.cost_usd ? money(p.cost_usd) : ""}</td><td class="mono">${esc(p.merge_policy)}</td></tr>`).join("");
  const table = `<div class="table-wrap"><table><thead><tr><th class="num">#</th><th><span class="announce">Aspect</span></th><th>Phase</th><th class="num">Attempts</th><th class="num">Reviews</th><th class="num">Fixes</th><th class="num">Debates</th>${docs ? `<th class="num" title="Documenter passes">Docs</th>` : ""}<th class="num">Cost</th><th>Merge</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  let detail = "";
  if (selected) {
    const isCurrent = selected.number === m.state.current_phase;
    const limits = m.config.limits || {};
    detail = `<section class="section" aria-labelledby="phase-title"><div class="view-head"><h2 id="phase-title" class="stop-code">${sig(selected.aspect, ASPECT_WORDS[selected.aspect], "lg")} ${selected.number} · ${esc(selected.name)}</h2>
        <p>${esc(ASPECT_WORDS[selected.aspect])}${selected.cost_usd ? ` · ${money(selected.cost_usd)}` : ""}</p></div>
      <dl class="kv panel panel-pad"><dt>Attempts</dt><dd>${selected.attempts} of ${limits.max_phase_attempts ?? "?"}</dd>
        <dt>Semantic reviews</dt><dd>${selected.reviews} of ${limits.max_semantic_review_attempts ?? "?"}</dd>
        <dt>Remediation laps</dt><dd>${selected.remediations}</dd><dt>Debates</dt><dd>${selected.debates} of ${limits.max_debate_rounds ?? "?"}</dd>
        <dt>Merge</dt><dd>${esc(selected.merge_policy)}${selected.checkpoint ? " · needs approval before it starts" : ""}</dd></dl>
      ${selected.track?.steps.some((st) => st.aspect !== "pending") ? `<div class="section">${stepTrack(selected)}</div>` : ""}
      <div class="section">${tabs("phase", [
        ...(selected.documentation && selected.documentation !== "off" && selected.doc_passes ? [{ id: "docs", label: "Docs", count: selected.doc_passes, render: () => docsPanel(m, selected.number) }] : []),
        { id: "activity", label: "What agents did", render: () => activityPanel(m, selected.number, isCurrent ? m.current?.evidence_flags : null) },
        { id: "operations", label: "Agent calls", count: selected.operations.length, render: () => operationsPanel(selected.operations) },
        ...(isCurrent ? [{ id: "findings", label: "Findings", render: () => findingsPanel(m) }, { id: "artifacts", label: "Artifacts", count: m.current?.artifacts.length || 0, render: () => artifactsPanel(m, m.current?.artifacts) }] : []),
      ])}</div></section>`;
  }
  return `<div class="view-head"><h1>Phases</h1><p>${m.phases.length} in build.yaml (${esc(m.config.source || "unreadable")})</p></div>${detail}<div class="section">${table}</div>`;
}

// ---------- plan: the roadmap in the operator's words ----------
// Discovery text marks code with backticks; set it in mono (the Machine Text Rule), escape everything else.
const inline = (value) => esc(value).replace(/`([^`]+)`/g, "<code>$1</code>");
function planNeeds(m) {
  const plan = m.plan || {}, notes = plan.notes || { before: {}, after: {} }, items = [];
  const e = m.escalation, n = m.state.current_phase;
  if (e) {
    const a = buildAspect(m);
    const note = e.code === "CHECKPOINT_REQUIRED" && n != null ? notes.before?.[n] : null;
    items.push({ aspect: a.aspect === "caution" ? "caution" : a.aspect === "unknown" ? "unknown" : "danger",
      text: note ? `Checkpoint before phase ${n}: ${note}` : `${e.code}: ${e.meaning}`,
      link: "#/stop", linkText: e.code === "CHECKPOINT_REQUIRED" ? "Open the stop to approve" : "Open the stop" });
  }
  if (m.bootstrapped && m.trust && m.trust.ok === false)
    items.push({ aspect: "caution", text: `Trust needs renewing: ${m.trust.error || "the trust anchor no longer matches"}`, link: "#/setup", linkText: "Open setup" });
  // Drafts never hold up the build, so they come last; the rail flag can scroll out of view on a narrow screen.
  if (m.drafts_waiting)
    items.push({ aspect: "caution", text: `${m.drafts_waiting === 1 ? "1 release draft waits" : `${m.drafts_waiting} release drafts wait`} for your signature or discard. The build does not wait on them.`, link: "#/drafts", linkText: "Open drafts" });
  return items;
}

function planSentence(m) {
  const st = m.state, total = m.phases.length, done = (st.completed_phases || []).length, n = st.current_phase;
  const a = buildAspect(m);
  if (!m.bootstrapped) return "Not set up yet.";
  if (a.aspect === "occupied") return `Running phase ${n}. ${done} of ${total} phases clear.`;
  if (a.word === "Waiting for you") {
    const approval = st.pending_approval;
    if (approval && approval.checkpoint === "mvp_completion") return `Waiting for your sign-off after phase ${approval.phase}, the MVP milestone. ${done} of ${total} phases clear.`;
    if (approval && approval.checkpoint.startsWith("merge-")) return `Waiting for your approval to merge phase ${approval.phase}. ${done} of ${total} phases clear.`;
    return n != null ? `Waiting for you before phase ${n}. ${done} of ${total} phases clear.` : `Waiting for you. ${done} of ${total} phases clear.`;
  }
  if (a.aspect === "danger") return `Stopped at phase ${n}. ${done} of ${total} phases clear.`;
  if (st.status === "complete") return `Complete. All ${total} phases clear.`;
  return `${done} of ${total} phases clear.`;
}

function checkpointRow(m, p, where) {
  const plan = m.plan || {}, notes = plan.notes || { before: {}, after: {} };
  const st = m.state, done = new Set(st.completed_phases || []);
  const note = (where === "before" ? notes.before : notes.after)?.[p.number];
  const waiting = where === "before" && st.current_phase === p.number && (st.pending_approval || m.escalation?.code === "CHECKPOINT_REQUIRED");
  const later = (st.current_phase ?? -1) > p.number || st.status === "complete";
  const passed = where === "before" ? (done.has(p.number) || (st.current_phase === p.number && !waiting)) : done.has(p.number) && later;
  const aspect = waiting ? "caution" : passed ? "clear" : "danger";
  const label = waiting ? "waiting for your approval" : passed ? "approved" : "ahead, needs approval";
  return `<li class="plan-row plan-gate${waiting ? " is-waiting" : ""}">
    <span class="plan-sig">${sig(aspect, `Checkpoint ${label}`)}</span><span class="plan-num"></span>
    <div class="plan-body"><p class="plan-gate-text"><b>Checkpoint ${where} phase ${p.number}</b> <span class="muted">${esc(label)}</span></p>
      ${note ? `<p class="plan-out">${esc(note)}</p>` : ""}
      ${waiting ? `<p class="plan-meta"><a href="#/stop">Open the stop to approve</a></p>` : ""}</div></li>`;
}

function viewPlan(m) {
  const plan = m.plan || { outcomes: {}, notes: { before: {}, after: {} }, decisions: [], risks: [], has_lines: false };
  const a = buildAspect(m), needs = planNeeds(m);
  const finalAt = m.config?.checkpoints?.mvp_completion;
  const rows = [];
  // Like the Phase Line, the cleared run from phase 0 folds into one row once it gets long.
  const done = new Set(m.state.completed_phases || []);
  let cleared = 0;
  while (cleared < m.phases.length && done.has(m.phases[cleared].number)) cleared++;
  const fold = cleared > 3 && cleared < m.phases.length;
  if (fold) rows.push(`<li class="plan-row plan-fold"><span class="plan-sig">${sig("clear")}</span><span class="plan-num"></span>
      <div class="plan-body"><button class="plan-fold-btn" type="button" data-fold aria-expanded="${!!S.planOpen}">${S.planOpen ? "Hide" : "Show"} phases 0 to ${cleared - 1} <span class="muted">${cleared} clear</span></button></div></li>`);
  for (const p of m.phases) {
    if (fold && !S.planOpen && p.number < cleared && !(p.checkpoint && p.number === cleared)) continue;
    if (p.checkpoint) rows.push(checkpointRow(m, p, "before"));
    const outcome = plan.outcomes?.[p.number];
    const facts = [p.attempts > 1 ? `${p.attempts} attempts` : "", p.cost_usd ? money(p.cost_usd) : ""].filter(Boolean);
    rows.push(`<li class="plan-row">
      <span class="plan-sig">${sig(p.aspect)}</span><span class="plan-num mono">${pad(p.number)}</span>
      <div class="plan-body"><h3 class="plan-name">${esc(p.name)}${p.milestones.map((s) => ` <span class="tag">${esc(s)}</span>`).join("")}</h3>
        ${outcome ? `<p class="plan-out">${inline(outcome)}</p>` : ""}
        <p class="plan-meta">${esc(ASPECT_WORDS[p.aspect] || p.aspect)}${facts.length ? ` · <span class="mono">${esc(facts.join(" · "))}</span>` : ""} · <a href="#/phases/${p.number}">Details</a></p></div></li>`);
    if (finalAt === p.number && (plan.notes?.after?.[p.number])) rows.push(checkpointRow(m, p, "after"));
  }
  const bySection = {};
  for (const d of plan.decisions || []) (bySection[d.section] ||= []).push(d);
  const risks = plan.risks || [];
  return `<div class="view-head"><h1>Plan</h1><p>${m.phases.length} phases${plan.sha ? ` · roadmap at <span class="mono">${esc(short(plan.sha, 7))}</span>` : ""}</p></div>
    <section class="plan-now panel" aria-labelledby="plan-say">
      ${sig(a.aspect, a.word, "lg")}
      <div><p class="plan-say" id="plan-say" role="status">${esc(planSentence(m))}</p>
        ${needs.length ? `<ul class="plan-needs">${needs.map((x) => `<li>${sig(x.aspect)}<span>${esc(x.text)} <a href="${x.link}">${esc(x.linkText)}</a></span></li>`).join("")}</ul>`
          : `<p class="muted">Nothing waits on you.</p>`}</div>
    </section>
    <div class="section">${renderLine(m)}</div>
    <section class="section" aria-labelledby="plan-steps"><h2 class="h2" id="plan-steps">Phases <small>${plan.has_lines ? "In plain words, from docs/ROADMAP.md." : "docs/ROADMAP.md has no outcome lines yet; buildctl-discover writes them when a phase has an outcome."}</small></h2>
      <ol class="plan-list panel">${rows.join("")}</ol></section>
    <div class="cols section">
      <section aria-labelledby="plan-risks"><h2 class="h2" id="plan-risks">Accepted risks <small>${risks.length} recorded in docs/DISCOVERY_RISKS.md</small></h2>
        ${risks.length ? `<ul class="plan-risks panel">${risks.map((r) => `<li>${r.severity ? `<span class="sev sev-${esc(r.severity)}">${esc(r.severity)}</span>` : `<span class="sev sev-LOW">OPEN</span>`}
          <div><p><span class="mono muted">${esc(r.id)}</span> ${inline(r.text)}</p>${r.accepted_by ? `<p class="muted">Accepted by ${esc(r.accepted_by)}${r.reason ? `: ${inline(r.reason)}` : ""}</p>` : ""}</div></li>`).join("")}</ul>`
          : `<p class="empty panel">No accepted risks.</p>`}</section>
      <aside aria-labelledby="plan-decisions"><h2 class="h2" id="plan-decisions">Decisions <small>${(plan.decisions || []).length} in docs/DISCOVERY_DECISIONS.md</small></h2>
        ${Object.keys(bySection).length ? `<div class="plan-decisions panel">${Object.entries(bySection).map(([section, list]) => `<details><summary>${esc(section)} <span class="muted mono">${list.length}</span></summary>
          <ul>${list.map((d) => `<li><span class="mono muted">${esc(d.id)}</span> ${inline(d.text)}</li>`).join("")}</ul></details>`).join("")}</div>`
          : `<p class="empty panel">No discovery decisions recorded.</p>`}</aside>
    </div>`;
}

// ---------- draft: a discovery session, read-only, before anyone finalizes it ----------
// buildctl-ui --session NAME. The model comes from buildctl-discover status and check; the console
// shows it and names the next command. It never finalizes: that stays a terminal act.
function draftAspect(d) {
  const toFix = d.failures.length + d.findings.length;
  if (d.passes) return { aspect: "clear", word: "Ready to finalize", sub: "the gate passes" };
  if (d.questions.length) return { aspect: "caution", word: "Waiting for you", sub: `${d.questions.length} call${d.questions.length === 1 ? "" : "s"} open` };
  if (toFix) return { aspect: "danger", word: "Gate failing", sub: `${toFix} to fix` };
  return { aspect: "unknown", word: "Not checked", sub: "buildctl-discover check gave no verdict" };
}

function renderDraftBar(m) {
  const d = m.draft, a = draftAspect(d);
  const repoName = m.repo.path.split("/").filter(Boolean).pop();
  $("#bar-repo").innerHTML = `<strong>${esc(repoName)}</strong><span>draft · session ${esc(d.session)}</span>`;
  $("#bar-aspect").innerHTML = `${sig(a.aspect, a.word, "lg")}<span><b>${esc(a.word)}</b><br><span class="mono">${esc(a.sub || "")}</span></span>`;
  $("#bar-facts").innerHTML = `
    <div><dd><span class="demo-flag" title="A buildctl-discover proposal. Nothing here is governed until it is finalized and trusted.">DRAFT</span></dd></div>
    <div><dt>Digest</dt><dd><code>${esc(short(d.digest, 12))}</code></dd></div>
    <div class="opt"><dt>Phases</dt><dd>${m.phases.length}</dd></div>
    <div class="opt"><dt>Calls open</dt><dd>${d.questions.length}</dd></div>`;
  $("#stop-flag").hidden = true;
  const root = m.control_root.split("/").filter(Boolean);
  $("#rail-foot").innerHTML = `<span title="${esc(m.control_root)}">Session in <code>…/${esc(root.slice(-2).join("/"))}</code></span><br>${d.events.length} recent events`;
  for (const link of document.querySelectorAll(".rail a")) {
    if (link.dataset.route === "plan") link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  document.title = `${a.word} · draft ${d.session} · buildctl`;
}

function draftNext(d) {
  const base = `buildctl-discover --session ${d.session}`;
  if (d.passes) return { say: "The gate passes. Finalizing writes the roadmap into the repository; trust it afterwards.", cmd: `${base} finalize --confirm ${short(d.digest, 12)}` };
  if (d.questions.length) return { say: "Answer the open calls through the interviewer, then check again.", cmd: `${base} check` };
  return { say: "The interviewer fixes what the gate lists, or you accept a finding as a risk, then check again.", cmd: `${base} check` };
}

function viewDraft(m) {
  const d = m.draft, a = draftAspect(d), next = draftNext(d);
  const plan = m.plan || { outcomes: {}, notes: { before: {}, after: {} }, decisions: [], risks: [] };
  const finalAt = m.config?.checkpoints?.mvp_completion;
  const gate = (p, where) => {
    const note = plan.notes?.[where]?.[p.number];
    return `<li class="plan-row plan-gate"><span class="plan-sig">${sig("pending", "Checkpoint")}</span><span class="plan-num"></span>
      <div class="plan-body"><p class="plan-gate-text"><b>Checkpoint ${where} phase ${p.number}</b></p>${note ? `<p class="plan-out">${inline(note)}</p>` : ""}</div></li>`;
  };
  const rows = [];
  for (const p of m.phases) {
    if (p.checkpoint) rows.push(gate(p, "before"));
    const outcome = plan.outcomes?.[p.number];
    rows.push(`<li class="plan-row"><span class="plan-sig">${sig("pending")}</span><span class="plan-num mono">${pad(p.number)}</span>
      <div class="plan-body"><h3 class="plan-name">${esc(p.name)}${p.milestones.map((s) => ` <span class="tag">${esc(s)}</span>`).join("")}</h3>
        ${outcome ? `<p class="plan-out">${inline(outcome)}</p>` : `<p class="plan-meta">No outcome line yet.</p>`}</div></li>`);
    if (finalAt === p.number) rows.push(gate(p, "after"));
  }
  const calls = d.questions.map((q) => `<li>${sig("caution")}<span><span class="mono muted">${esc(q.id)}</span> ${inline(q.text)}</span></li>`);
  const fixes = d.failures.map((f) => `<li>${sig("danger")}<span><span class="mono muted">${esc(f.rule)}</span> ${inline(f.text)}</span></li>`);
  const findings = d.findings;
  const bySection = {};
  for (const x of plan.decisions || []) (bySection[x.section] ||= []).push(x);
  const events = [...d.events].reverse();
  return `<div class="view-head"><h1>Draft plan</h1><p>Session <span class="mono">${esc(d.session)}</span> · ${m.phases.length} phases · digest <span class="mono">${esc(short(d.digest, 12))}</span></p></div>
    <section class="plan-now panel" aria-labelledby="draft-say">
      ${sig(a.aspect, a.word, "lg")}
      <div><p class="plan-say" id="draft-say" role="status">${esc(next.say)}</p>
        ${calls.length || fixes.length ? `<ul class="plan-needs">${calls.join("")}${fixes.join("")}</ul>` : ""}
        <pre class="file draft-cmd" aria-label="Next command">${esc(next.cmd)}</pre></div>
    </section>
    <section class="section" aria-labelledby="draft-steps"><h2 class="h2" id="draft-steps">Phases <small>From the proposal's docs/ROADMAP.md. Nothing has run.</small></h2>
      <ol class="plan-list panel">${rows.join("") || `<li class="plan-row"><p class="empty">No phases proposed yet.</p></li>`}</ol></section>
    <div class="cols section">
      <section aria-labelledby="draft-findings"><h2 class="h2" id="draft-findings">Open review findings <small>${findings.length} the gate counts against the draft</small></h2>
        ${findings.length ? `<ul class="plan-risks panel">${findings.map((f) => `<li><span class="sev sev-${esc(f.severity)}">${esc(f.severity)}</span>
          <div><p><span class="mono muted">${esc(f.id)}</span> ${inline(f.text)}</p><p class="muted">On ${esc(f.target)}</p></div></li>`).join("")}</ul>`
          : `<p class="empty panel">No open findings.</p>`}
        <h2 class="h2 gap" id="draft-decisions">Decisions <small>${(plan.decisions || []).length} so far</small></h2>
        ${Object.keys(bySection).length ? `<div class="plan-decisions panel">${Object.entries(bySection).map(([section, list]) => `<details><summary>${esc(section)} <span class="muted mono">${list.length}</span></summary>
          <ul>${list.map((x) => `<li><span class="mono muted">${esc(x.id)}</span> ${inline(x.text)}</li>`).join("")}</ul></details>`).join("")}</div>`
          : `<p class="empty panel">No decisions recorded yet.</p>`}</section>
      <aside aria-labelledby="draft-feed"><h2 class="h2" id="draft-feed">What the interviewer recorded <small>newest first, refreshes on its own</small></h2>
        ${events.length ? `<div class="table-wrap flush feed-wrap panel"><table class="feed"><tbody>${events.map((e) => `<tr><td class="mono">${e.at ? esc(clock(e.at)) : ""}</td><td class="what">${inline(e.text)}</td></tr>`).join("")}</tbody></table></div>`
          : `<p class="empty panel">Nothing recorded yet.</p>`}</aside>
    </div>`;
}

// ---------- release drafts ----------
// buildctl-drafts' runs, read from its files. Approving is the operator's read, so it is signed with the
// draft's digest; discarding records only. Nothing here publishes.
const DRAFT_SIGN = 7;
function releaseAspect(d) {
  if (d.state === "approved") return ["clear", "approved"];
  if (d.verdict !== "sourced") return ["danger", "failed a check"];
  return ["caution", "waiting for you"];
}
function draftCount(d) { return d.claims.length ? `${d.backed}/${d.claims.length}` : "no claims"; }

function markdown(text) {
  // A small, escaped renderer for draft bodies: headings, paragraphs, lists, fenced code, inline code, bold.
  const span = (value) => esc(value).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>");
  const out = []; let list = null, para = [], code = null;
  const flush = () => { if (para.length) { out.push(`<p>${span(para.join(" "))}</p>`); para = []; } if (list) { out.push(`</${list}>`); list = null; } };
  for (const line of String(text || "").split("\n")) {
    if (code !== null) { if (/^```/.test(line)) { out.push(`<pre><code>${esc(code.join("\n"))}</code></pre>`); code = null; } else code.push(line); continue; }
    if (/^```/.test(line)) { flush(); code = []; continue; }
    const h = line.match(/^(#{1,6})\s+(.*)$/);
    if (h) { flush(); const level = Math.min(4, h[1].length + 1); out.push(`<h${level}>${span(h[2])}</h${level}>`); continue; }
    const item = line.match(/^\s*(?:([-*])|(\d+)\.)\s+(.*)$/);
    if (item) { if (para.length) { out.push(`<p>${span(para.join(" "))}</p>`); para = []; } const kind = item[1] ? "ul" : "ol";
      if (list !== kind) { if (list) out.push(`</${list}>`); out.push(`<${kind}>`); list = kind; } out.push(`<li>${span(item[3])}</li>`); continue; }
    if (!line.trim()) { flush(); continue; }
    if (list) { out.push(`</${list}>`); list = null; }
    para.push(line.trim());
  }
  if (code !== null) out.push(`<pre><code>${esc(code.join("\n"))}</code></pre>`);
  flush();
  return out.join("");
}

function claimRow(c, open) {
  const where = [c.ref, c.line ? `:${c.line}` : ""].join("");
  const kind = { file: "file", commit: "commit", changelog: "changelog", finding: "open finding" }[c.kind] || c.kind || "source";
  const quote = c.quote ? `<div class="quote"><span class="muted">Quoted from ${esc(c.ref || "")}</span>${esc(c.quote)}</div>` : "";
  const why = c.why ? `<span class="why">${esc(c.why)}</span>` : "";
  return `<li class="${c.why ? "unbacked" : ""}"><details ${open ? "open" : ""}><summary>${sig(c.why ? "danger" : "clear", c.why ? "unbacked" : "backed")}<span class="n">${c.n}</span>
    <span>${inline(c.text)}</span><span class="src">${esc(kind)} · ${esc(kind === "commit" ? short(where, 7) : where)}</span>${why}</summary>${quote}</details></li>`;
}

function draftChecks(d) {
  const rows = [];
  rows.push(`<li>${sig(d.voice && !/plain/.test(d.voice) ? "clear" : "pending", "voice file")}<span>Voice</span><span class="detail">${esc(d.voice || "none, written plain")}</span></li>`);
  for (const item of d.contract) rows.push(`<li class="blocked">${sig("danger", "failed")}<span>Shape of the draft</span><span class="detail">${esc(item)}</span></li>`);
  if (d.lint.length) for (const item of d.lint) rows.push(`<li class="blocked">${sig("danger", "failed")}<span>Voice rules</span><span class="detail">${esc(item)}</span></li>`);
  else rows.push(`<li>${sig("clear", "held")}<span>Voice rules</span><span class="detail">no banned phrases, within the word limit</span></li>`);
  return `<ul class="conds draft-checks">${rows.join("")}</ul>`;
}

function draftSign(d) {
  const key = `draft:${d.id}`;
  if (d.state === "approved") {
    return `<div class="draft-decision"><div class="stamp${S.stamps[key] ? " fresh" : ""}" role="img" aria-label="Signed by you">SIGNED <span>approved${d.decided_at ? ` · ${esc(day(d.decided_at))} ${esc(clock(d.decided_at))}` : ""}</span></div>
      <p class="muted">Recorded in the drafts log. Nothing was posted.</p>${renderResult(key, { stamp: false })}</div>`;
  }
  const busy = S.busy[key];
  if (!S.open[key]) {
    return `<div class="draft-decision"><button class="btn quiet" data-draft-open="${esc(d.id)}" aria-expanded="false">Review and sign…</button>
      <button class="btn quiet" data-draft-discard="${esc(d.id)}" ${busy ? "disabled" : ""}>Discard</button>
      <p class="muted">Either one only records your decision in the drafts log. Nothing is posted.</p>${renderResult(key)}</div>`;
  }
  const typed = S.typed[key] || "";
  const want = d.digest.slice(0, DRAFT_SIGN);
  const match = typed.trim().toLowerCase() === want;
  const unbacked = d.claims.filter((c) => c.why);
  const failing = unbacked.length || d.lint.length || d.contract.length
    ? `<p class="ceremony-warn">${sig("danger", "failed a check")} You are signing a draft that failed ${unbacked.length + d.lint.length + d.contract.length === 1 ? "a check" : "checks"}:</p>
       <ul class="ceremony-list">${unbacked.map((c) => `<li>Claim ${c.n}: ${inline(c.text)} <span class="muted">${esc(c.why)}</span></li>`).join("")}${[...d.lint, ...d.contract].map((t) => `<li>${esc(t)}</li>`).join("")}</ul>` : "";
  return `<div class="ceremony" role="group" aria-label="Sign ${esc(d.title)}">
    <p class="ceremony-caption"><b>What you are signing.</b> The checks proved each claim cites a source that exists. They did not prove the source supports the claim, or that every sentence is in the ledger. That read is yours.</p>
    ${failing}
    <div><div class="muted">Digest of this draft</div><div class="evidence"><mark>${esc(want)}</mark>${esc(d.digest.slice(DRAFT_SIGN))}</div></div>
    <div class="ceremony-row">
      <div class="field"><label for="typed-${esc(key)}">Type the first ${DRAFT_SIGN} characters of the digest</label>
        <input id="typed-${esc(key)}" data-typed="${esc(key)}" data-want="${esc(want)}" value="${esc(typed)}" autocomplete="off" spellcheck="false"
          aria-invalid="${typed && !match ? "true" : "false"}" aria-describedby="hint-${esc(key)}">
        <span class="hint" id="hint-${esc(key)}">${typed && !match ? "Does not match the digest yet." : "If the draft changes before you sign, the console refuses."}</span></div>
      <button class="btn sign" data-draft-sign="${esc(d.id)}" ${match && !busy ? "" : "disabled"}>${busy ? "Signing…" : "Sign and approve"}</button>
      <button class="btn quiet" data-draft-cancel="${esc(d.id)}">Cancel</button>
    </div>${renderResult(key)}</div>`;
}

function draftPanel(d, run) {
  const unbacked = d.claims.filter((c) => c.why);
  const [aspect, word] = releaseAspect(d);
  const head = `<div class="ledger-head">${sig(d.verdict === "sourced" ? "clear" : "danger", d.verdict)}
      <h3>${d.backed} of ${d.claims.length} claims cite a source that exists</h3><small>checked by code, not a model</small></div>
    ${unbacked.length ? `<ol class="claims urgent">${unbacked.map((c) => claimRow(c, true)).join("")}</ol>` : ""}`;
  const backed = d.claims.filter((c) => !c.why);
  const fields = d.kind === "positioning" && (d.fields.description || (d.fields.topics || []).length)
    ? `<h4 class="ledger-sub">Repository</h4><dl class="ledger-fields"><dt>Description</dt><dd>${esc(d.fields.description || "")}</dd>
       <dt>Topics</dt><dd>${(d.fields.topics || []).map((t) => `<code>${esc(t)}</code>`).join(" ")}</dd></dl>` : "";
  const limits = d.known_limits.length ? `<h4 class="ledger-sub">Known limits <small>the writer's list, printed after the body</small></h4>
    <ul class="ledger-list">${d.known_limits.map((t) => `<li>${inline(t)}</li>`).join("")}</ul>` : "";
  const ledger = `<h4 class="ledger-sub">Claims</h4><ol class="claims">${backed.map((c) => claimRow(c, false)).join("")}</ol>
    ${fields}${limits}<h4 class="ledger-sub">Checks</h4>${draftChecks(d)}`;
  const words = (d.body.match(/[\w'-]+/g) || []).length;
  return `<div class="draft-grid">
    <div class="draft-head">${head}</div>
    <aside class="draft-ledger" aria-label="Claims ledger">${ledger}</aside>
    <article class="draft-page" aria-label="${esc(d.title)}">
      <p class="draft-meta">${sig(aspect, word)} ${esc(word)} · ${esc(d.kind)} · ${d.attempts || 1} ${d.attempts > 1 ? "attempts" : "attempt"} · ${words} words · ${money(d.cost)}</p>
      <div class="prose"><h3 class="prose-title">${esc(d.heading || d.title)}</h3>${markdown(d.body.replace(/^#\s[^\n]*\n/, ""))}</div>
      ${draftSign(d)}
    </article></div>`;
}

function draftLever(lever) {
  const key = "drafts-make";
  const locked = !lever.enabled;
  const blocked = lever.conditions.filter((c) => !c.held);
  const conds = `<ul class="conds">${lever.conditions.map((c) => `<li class="${c.held ? "" : "blocked"}">${sig(c.held ? "clear" : "danger", c.held ? "held" : "blocked")}<span>${esc(c.label)}</span><span class="detail">${esc(c.detail || "")}</span></li>`).join("")}</ul>`;
  const note = locked ? `<p class="lock-note" id="lock-${key}">${ico("lock")}Locked by ${blocked.length === 1 ? "one condition" : `${blocked.length} conditions`}: ${esc(blocked.map((c) => c.label.toLowerCase()).join("; "))}.</p>` : "";
  const kinds = lever.kinds.map((k) => ({ "release-notes": "release notes", positioning: "positioning", "launch-post": "launch post", "demo-script": "demo script", social: "social post" }[k]));
  const milestone = S.draftMilestone && lever.milestones.includes(S.draftMilestone) ? S.draftMilestone : lever.milestones[lever.milestones.length - 1];
  let ceremony = "";
  if (S.open[key] && !locked) {
    const typed = S.typed[key] || "";
    const match = typed.trim().toLowerCase() === (milestone || "").toLowerCase();
    const pick = lever.milestones.length > 1 ? `<div class="field"><label for="draft-milestone">Milestone</label><select id="draft-milestone" data-draft-milestone>
      ${lever.milestones.map((name) => `<option ${name === milestone ? "selected" : ""}>${esc(name)}</option>`).join("")}</select></div>` : "";
    ceremony = `<div class="ceremony" role="group" aria-label="Authorize a paid drafts run">
      <p class="ceremony-caption"><b>What you are authorizing.</b> ${kinds.length} writer calls at about ${money(lever.estimate / kinds.length / 2)} each, booked against today's cap. Each draft may be rewritten once, so the run books at most ${money(lever.estimate)}.</p>
      ${pick}<div><div class="muted">Milestone</div><div class="evidence"><mark>${esc(milestone)}</mark></div></div>
      <div class="ceremony-row"><div class="field"><label for="typed-${key}">Type the milestone's name</label>
        <input id="typed-${key}" data-typed="${key}" data-want="${esc(milestone)}" value="${esc(typed)}" autocomplete="off" spellcheck="false" aria-invalid="${typed && !match ? "true" : "false"}" aria-describedby="hint-${key}">
        <span class="hint" id="hint-${key}">The run starts detached and keeps going if you close the console.</span></div>
      <button class="btn sign" data-draft-make ${match && !S.busy[key] ? "" : "disabled"}>${S.busy[key] ? "Starting…" : "Sign and write"}</button>
      <button class="btn quiet" data-draft-cancel="make">Cancel</button></div></div>`;
  }
  return `<div class="interlock"><article class="lever ${locked ? "locked" : ""}" id="lever-${key}">
    <div class="lever-plate">${locked ? ico("lock") : ""}</div>
    <div class="lever-body"><div class="lever-head"><div><h3>Write drafts <span class="tag">paid</span> <span class="tag">runs detached</span></h3>
      <p>${esc(kinds.join(", "))}, from one completed milestone. Up to ${money(lever.estimate)}: ${kinds.length} drafts at ${money(lever.estimate / kinds.length / 2)}, each may be rewritten once.</p></div>
      <button class="btn quiet" data-draft-open="make" ${locked ? `disabled aria-describedby="lock-${key}"` : ""} aria-expanded="${S.open[key] ? "true" : "false"}">Write drafts…</button></div>
      ${conds}${note}${ceremony}${renderResult(key)}</div></article></div>`;
}

function viewDrafts(m) {
  const v = S.drafts;
  if (!v) return `<div class="view-head"><h1>Release drafts</h1></div><p class="loading">Reading the drafts…</p>`;
  const lever = `<section class="section"><h2 class="h2">Write new drafts <small>A fresh run from a milestone. Earlier drafts stay.</small></h2>${draftLever(v.lever)}</section>`;
  if (!v.runs.length) {
    return `<div class="view-head"><h1>Release drafts</h1><p>Nothing here is published</p></div>
      <div class="panel"><div class="empty"><p>No drafts yet.</p><p class="muted">buildctl-drafts writes release notes, positioning, a launch post and a demo script from a finished milestone, and checks that every claim cites a source that exists.</p></div></div>${lever}`;
  }
  const run = v.runs.find((r) => r.id === S.draftRun) || v.runs[0];
  const shown = run.drafts.filter((d) => d.state !== "discarded");
  const discarded = run.drafts.filter((d) => d.state === "discarded");
  const selected = run.drafts.find((d) => d.id === S.tab.drafts) || shown[0] || run.drafts[0];
  const social = !run.drafts.some((d) => d.kind === "social");
  const tab = (d) => { const [aspect, word] = releaseAspect(d); const on = d.id === selected.id;
    return `<button role="tab" id="tab-drafts-${esc(d.id)}" aria-selected="${on}" tabindex="${on ? 0 : -1}" aria-controls="panel-drafts" data-tab="drafts" data-tab-id="${esc(d.id)}">${sig(aspect, word)}${esc(d.title)}<span class="count">${esc(draftCount(d))}</span></button>`; };
  const skipped = social ? `<span class="tab-skipped" title="${v.operator_voice ? "Not part of this run." : "Write ~/.config/buildctl/voice.md to get a social post."}">${sig("pending", "skipped")}Social post<span class="count">${v.operator_voice ? "not in this run" : "no voice file"}</span></span>` : "";
  const runs = v.runs.length > 1 ? `<label class="drafts-run">Run <select data-draft-run>${v.runs.map((r) => `<option value="${esc(r.id)}" ${r.id === run.id ? "selected" : ""}>${esc(day(r.at))} ${esc(clock(r.at))} · ${esc(r.label)} · ${r.drafts.length} drafts · ${money(r.cost)}</option>`).join("")}</select></label>`
    : `<p class="drafts-run mono">${esc(day(run.at))} ${esc(clock(run.at))} · ${run.drafts.length} drafts · ${money(run.cost)}</p>`;
  const gone = discarded.length ? `<p class="drafts-discarded">${discarded.length} discarded: ${discarded.map((d) => `<button class="linkish" data-tab="drafts" data-tab-id="${esc(d.id)}">${esc(d.title)}</button>`).join(", ")}</p>` : "";
  const banner = selected.state === "discarded" ? `<p class="draft-gone">You discarded this draft${selected.decided_at ? ` ${esc(ago(selected.decided_at))}` : ""}. It stays in the run; approving it would still need your signature.</p>` : "";
  return `<div class="view-head"><h1>Release drafts</h1><p>Milestone ${esc(run.label)} · written from <span class="mono">${esc(short(run.until, 7))}</span> · nothing here is published</p>${runs}</div>
    <div class="tabs drafts-tabs" role="tablist" aria-label="Drafts in this run">${shown.map(tab).join("")}${skipped}</div>${gone}
    <div class="tab-panel" role="tabpanel" id="panel-drafts">${banner}${draftPanel(selected, run)}</div>${lever}`;
}

async function loadDrafts() {
  if (S.route.view !== "drafts") { const had = S.drafts != null; S.drafts = null; S.draftsKey = ""; return had; }
  try {
    const { body } = await api("/api/drafts");
    const key = JSON.stringify(body);
    if (key === S.draftsKey) return false;
    S.drafts = body; S.draftsKey = key;
    return true;
  } catch (error) { S.drafts = { runs: [], lever: { enabled: false, conditions: [{ label: "Drafts readable", held: false, detail: error.message }], kinds: [], milestones: [], estimate: 0 }, operator_voice: false }; return true; }
}

async function draftPull(key, payload, { stampOnOk = false } = {}) {
  S.busy[key] = true; delete S.results[key]; render();
  try {
    const { status, body } = await api("/api/drafts", { method: "POST", body: JSON.stringify(payload) });
    const ok = status < 300;
    const text = body.detached ? `Started ${body.command} as pid ${body.pid}. It keeps running if you close the console; new drafts appear here when it finishes.`
      : ok ? `${body.command} finished.` : `${body.command} exited ${body.returncode}.`;
    S.results[key] = { ok, text, output: body.detached ? "" : body.output };
    if (ok) { S.open[key] = false; S.typed[key] = ""; if (stampOnOk) S.stamps[key] = "approved"; }
    announce(text);
  } catch (error) {
    S.results[key] = { ok: false, text: error.message }; announce(error.message);
  } finally { S.busy[key] = false; S.draftsKey = ""; await refresh(true); }
}

const DAY_FMT = (iso) => iso.slice(0, 10);
function viewLedger(m) {
  const events = S.audit;
  const types = [...new Set(events.map((e) => e.event_type))].sort();
  const phaseOf = (e) => e.payload?.phase ?? (typeof e.payload?.operation === "string" ? Number(e.payload.operation.split(":")[0].replace("phase-", "")) : e.payload?.current_phase ?? "");
  const shown = events.filter((e) => (!S.ledger.event || e.event_type === S.ledger.event) && (S.ledger.phase === "" || String(phaseOf(e)) === S.ledger.phase)).slice(-400).reverse();
  let lastDay = "";
  const rows = shown.map((e) => {
    const d = DAY_FMT(e.timestamp);
    const header = d !== lastDay ? `<tr class="day"><td colspan="5">${esc(day(e.timestamp))}</td></tr>` : "";
    lastDay = d;
    const payload = { ...e.payload }; delete payload.phase;
    const human = e.event_type === "checkpoint_approved";
    const detail = human ? `<span class="stamp" role="img" aria-label="Signed by a person">SIGNED <span>${esc(e.payload.scope?.checkpoint || "")}</span></span>`
      : esc(Object.entries(payload).map(([k, v]) => `${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join("  ")).slice(0, 220);
    return `${header}<tr><td class="num mono">${e.sequence}</td><td class="mono">${esc(clock(e.timestamp))}</td>
      <td><span class="event-name ev-${esc(e.event_type)}">${esc(e.event_type)}</span></td><td class="num mono">${esc(phaseOf(e))}</td><td class="mono">${detail}</td></tr>`;
  }).join("");
  return `<div class="view-head"><h1>Ledger</h1><p>Every state change, hash-chained.</p></div>
    <div class="chain">${sig(m.audit.ok ? "clear" : "danger", m.audit.ok ? "chain intact" : "chain broken")}
      <b>${m.audit.ok ? "Chain intact" : "Chain broken"}</b><span>${m.audit.sequence} events</span><span class="mono">head ${esc(short(m.audit.head, 16))}</span>
      ${m.audit.error ? `<span class="stop-detail">${esc(m.audit.error)}</span>` : ""}</div>
    <div class="filters">
      <div class="field"><label for="f-phase">Phase</label><select id="f-phase" data-filter="phase"><option value="">All phases</option>
        ${m.phases.map((p) => `<option value="${p.number}" ${S.ledger.phase === String(p.number) ? "selected" : ""}>${p.number} · ${esc(p.name)}</option>`).join("")}</select></div>
      <div class="field"><label for="f-event">Event</label><select id="f-event" data-filter="event"><option value="">All events</option>
        ${types.map((t) => `<option ${S.ledger.event === t ? "selected" : ""}>${esc(t)}</option>`).join("")}</select></div>
      <p class="muted">${shown.length < events.length ? `Showing ${shown.length} of ${events.length}, newest first.` : `${events.length} events, newest first.`}</p>
    </div>
    ${shown.length ? `<div class="table-wrap"><table><thead><tr><th class="num">Seq</th><th>Time</th><th>Event</th><th class="num">Phase</th><th>Detail</th></tr></thead><tbody>${rows}</tbody></table></div>`
      : `<div class="panel"><p class="empty">No events match these filters.</p></div>`}`;
}

function docSettings(m) {
  const d = m.documentation;
  if (!d) return "";
  const who = (r) => [r?.vendor, r?.model_family, r?.prompt_lineage].filter(Boolean).join(" · ");
  return `<h2 class="h2 gap">Documentation <small>from build.yaml, ${esc(m.config.source || "")}</small></h2>
    <dl class="kv panel panel-pad"><dt>Mode</dt><dd>${esc(d.mode)}</dd><dt>Paths</dt><dd>${esc(d.paths.join(" · "))}</dd>
      ${d.governed_inside.length ? `<dt>Governed inside</dt><dd>${esc(d.governed_inside.join(" · "))} (read-only to every agent)</dd>` : ""}
      ${d.changelog ? `<dt>Changelog</dt><dd>${esc(d.changelog)} · under "${esc(d.unreleased_heading)}"</dd>` : ""}
      ${d.forbidden_phrases.length ? `<dt>Forbidden</dt><dd>${esc(d.forbidden_phrases.join(" · "))}</dd>` : ""}
      <dt>Send-backs</dt><dd>up to ${d.max_attempts} per attempt</dd>
      <dt>Documenter</dt><dd>${esc(who(d.documenter))}${d.documenter?.derived_from ? " (the implementer's family)" : ""}</dd>
      <dt>Brief</dt><dd>${esc(d.brief)}</dd></dl>
    ${d.warnings.map((w) => `<p class="note panel">Warning: ${esc(w)}. Set <code>roles.documenter</code> to another model family and re-trust.</p>`).join("")}`;
}

function viewSetup(m) {
  const t = m.trust, iso = m.isolation;
  const kernelMatch = t.kernel && t.running_kernel && t.kernel.digest === t.running_kernel.digest;
  const steps = [
    { aspect: m.config.error ? "danger" : m.config.source?.startsWith("trusted") ? "clear" : "caution", name: "build.yaml",
      what: m.config.error ? esc(m.config.error) : `Valid, read from the ${esc(m.config.source)}. Edit it in your editor and commit; trust pins the commit.` },
    { aspect: t.ok ? "clear" : "danger", name: "Trust anchor",
      what: t.ok ? `Pinned to <code>${esc(short(t.governance_sha, 12))}</code>, ${t.governed_files.length} governed files, kernel ${esc(t.running_kernel.version)} <code>${esc(short(t.running_kernel.digest, 12))}</code>.` : esc(t.error || "Not initialized.") },
    { aspect: iso.fresh ? "clear" : iso.verified_at ? "caution" : "danger", name: "Sandbox",
      what: iso.fresh ? `Verified ${esc(ago(iso.verified_at))}. Expires 24 hours after verification.` : esc(iso.error || "Not verified.") },
    { aspect: "unknown", name: "Agent logins", what: "Checked when each agent call starts. Run the check below to confirm now." },
    { aspect: m.bootstrapped ? "clear" : "pending", name: "Build state", what: m.bootstrapped ? `Created. Generation ${m.state.generation}.` : "Not created yet. Bootstrap after trust and sandbox hold." },
  ];
  const diff = S.trustDiff;
  const diffBlock = diff && diff.from && diff.to && diff.from !== diff.to
    ? `<div class="table-wrap"><table><caption class="announce">Changes since the trusted commit</caption><thead><tr><th>File changed since ${esc(short(diff.from, 8))}</th><th class="num">Added</th><th class="num">Removed</th></tr></thead><tbody>
      ${diff.files.map((f) => `<tr><td class="mono">${esc(f.path)}</td><td class="num mono">${esc(f.added)}</td><td class="num mono">${esc(f.removed)}</td></tr>`).join("") || `<tr><td colspan="3" class="muted">No file changes; only the commit differs.</td></tr>`}</tbody></table></div>`
    : `<p class="muted">${t.ok ? "HEAD is the trusted commit. Nothing to review." : "No trusted commit yet, so every governed file at HEAD is new to buildctl."}</p>`;
  return `<div class="view-head"><h1>Setup</h1><p>Readiness, in the order buildctl checks it.</p></div>
    <ol class="ready">${steps.map((s) => `<li>${sig(s.aspect)}<b>${esc(s.name)}</b><span class="what">${s.what}</span></li>`).join("")}</ol>
    <div class="cols section">
      <section><h2 class="h2">Levers</h2>${renderLevers(m.levers.filter((l) => SETUP_LEVERS.includes(l.action)))}</section>
      <aside><h2 class="h2">Before you pin HEAD</h2>${diffBlock}
        ${docSettings(m)}
        ${t.ok ? `<h2 class="h2 gap">Pinned executables</h2><dl class="kv panel panel-pad">${Object.entries(t.executables || {}).map(([role, e]) => `<dt>${esc(role)}</dt><dd>${esc(e.path)}<br>${esc(short(e.sha256, 24))}</dd>`).join("")}
          <dt>kernel</dt><dd>${kernelMatch ? "matches the running buildctl" : "differs from the running buildctl"}</dd></dl>` : ""}</aside>
    </div>`;
}

// ---------- render loop ----------
function render() {
  const m = S.model;
  if (!m) return;
  renderBar(m);
  const focused = document.activeElement;
  const focusKey = focused?.dataset?.typed || focused?.dataset?.lever || focused?.dataset?.tabId || focused?.id;
  const caret = focused?.selectionStart;
  const view = m.draft ? viewDraft : { line: viewLine, stop: viewStop, phases: viewPhases, ledger: viewLedger, setup: viewSetup, plan: viewPlan, drafts: viewDrafts }[S.route.view] || viewPlan;
  const main = $("#main");
  main.innerHTML = view(m);
  for (const el of main.querySelectorAll("[data-w]")) el.style.width = `${Math.max(0.02, Number(el.dataset.w)) * 100}%`;
  for (const el of main.querySelectorAll("[data-cell]")) {
    const key = el.dataset.cell, value = el.textContent;
    if (S.cells[key] !== undefined && S.cells[key] !== value) el.classList.add("cell-changed");
    S.cells[key] = value;
  }
  if (focusKey) {
    const target = main.querySelector(`[data-typed="${CSS.escape(focusKey)}"]`) || main.querySelector(`[data-lever="${CSS.escape(focusKey)}"]`)
      || main.querySelector(`[data-tab-id="${CSS.escape(focusKey)}"]`) || document.getElementById(focusKey);
    if (target && main.contains(target)) { target.focus(); if (caret != null && target.setSelectionRange) target.setSelectionRange(caret, caret); }
  }
  tick();
}

function tick() {
  for (const el of document.querySelectorAll("[data-since]")) el.textContent = span(el.dataset.since, el.dataset.until);
}

function fingerprint(m) {
  const copy = { ...m, generated_at: null, isolation: { ...m.isolation, age_hours: Math.round((m.isolation.age_hours || 0) * 60) } };
  return JSON.stringify(copy);
}

async function refresh(force = false) {
  try {
    const { body } = await api("/api/model");
    const fp = fingerprint(body);
    const changed = force || fp !== S.fingerprint;
    S.model = body; S.fingerprint = fp; S.error = null;
    if (S.route.view === "ledger" || S.audit.length === 0 || body.audit.sequence > S.auditSeq) await loadAudit(body.audit.sequence);
    if (S.route.view === "setup") await loadTrustDiff(body);
    const activityChanged = await loadActivity(body);
    const docsChanged = await loadDocs(body);
    const draftsChanged = await loadDrafts();
    if (changed || activityChanged || docsChanged || draftsChanged) render();
  } catch (error) {
    if (error.status === 401) {
      $("#main").innerHTML = `<div class="panel"><div class="empty"><p>${sig("danger", "refused")} This tab has no console token.</p><p>Open the link <code>buildctl-ui</code> printed in your terminal. The token in it is what lets this page read the build.</p></div></div>`;
    } else if (!S.error) {
      S.error = error;
      $("#bar-aspect").innerHTML = `${sig("unknown", "unreachable", "lg")}<span><b>Console unreachable</b><br><span class="mono">${esc(error.message)}</span></span>`;
    }
  }
}

async function loadActivity(m) {
  const number = m.evidence?.recording && S.route.view !== "ledger" && S.route.view !== "setup" ? activityPhase(m) : null;
  if (number == null) {
    const had = S.activity != null;
    S.activity = null; S.activityKey = "";
    return had;
  }
  const { body } = await api(`/api/activity?phase=${number}`);
  const key = JSON.stringify(body);
  if (key === S.activityKey) return false;
  S.activity = body; S.activityKey = key;
  return true;
}

async function loadDocs(m) {
  // Only where a Docs tab can show; each read runs git for every documented file.
  const number = S.route.view === "stop" || S.route.view === "phases" ? activityPhase(m) : null;
  const p = number != null ? phaseByNumber(number) : null;
  if (!p || !p.documentation || p.documentation === "off" || !p.doc_passes) {
    const had = S.docs != null;
    S.docs = null; S.docsKey = "";
    return had;
  }
  try {
    const { body } = await api(`/api/documentation?phase=${number}`);
    const key = JSON.stringify(body);
    if (key === S.docsKey) return false;
    S.docs = body; S.docsKey = key;
    return true;
  } catch { return false; }
}

async function loadAudit(head) {
  if (head < S.auditSeq) { S.audit = []; S.auditSeq = 0; }
  while (S.auditSeq < head) {
    const { body } = await api(`/api/audit?after=${S.auditSeq}&limit=2000`);
    if (!body.length) break;
    S.audit.push(...body);
    S.auditSeq = body[body.length - 1].sequence;
  }
  if (S.audit.length > 6000) S.audit = S.audit.slice(-6000);
}

async function loadTrustDiff(m) {
  const key = `${m.trust.governance_sha}:${m.repo.head_sha}`;
  if (key === S.trustDiffKey) return;
  S.trustDiffKey = key;
  try { S.trustDiff = (await api("/api/trust-diff")).body; } catch { S.trustDiff = null; }
}

function parseRoute() {
  const [, view = "plan", arg = null] = (location.hash.replace(/^#/, "") || "/plan").split("/");
  S.route = { view, arg };
  if (view !== "stop" && view !== "phases") S.artifact = null;
}

// ---------- events ----------
document.addEventListener("toggle", (event) => {
  const key = event.target?.dataset?.docOpen;
  if (key) S.docOpen[key] = event.target.open;
}, true);
document.addEventListener("click", async (event) => {
  if (event.target.closest("[data-dismiss-receipt]")) { S.signed = null; render(); return; }
  const draft = event.target.closest("[data-draft-open],[data-draft-cancel],[data-draft-sign],[data-draft-discard],[data-draft-make]");
  if (draft) {
    const ds = draft.dataset;
    if (ds.draftOpen) { const key = ds.draftOpen === "make" ? "drafts-make" : `draft:${ds.draftOpen}`; S.open[key] = true; delete S.results[key]; render(); $(`[data-typed="${CSS.escape(key)}"]`)?.focus(); }
    else if (ds.draftCancel) { const key = ds.draftCancel === "make" ? "drafts-make" : `draft:${ds.draftCancel}`; S.open[key] = false; S.typed[key] = ""; render(); }
    else if (ds.draftSign) { const d = S.drafts.runs.flatMap((r) => r.drafts).find((x) => x.id === ds.draftSign); const key = `draft:${ds.draftSign}`;
      if (d) draftPull(key, { action: "approve", draft: d.id, digest: d.digest, confirm: S.typed[key] || "" }, { stampOnOk: true }); }
    else if (ds.draftDiscard) draftPull(`draft:${ds.draftDiscard}`, { action: "discard", draft: ds.draftDiscard });
    else if (draft.hasAttribute("data-draft-make")) draftPull("drafts-make", { action: "make", confirm: S.typed["drafts-make"] || "" });
    return;
  }
  if (event.target.closest("[data-fold]")) { S.planOpen = !S.planOpen; render(); $("[data-fold]")?.focus(); return; }
  const t = event.target.closest("[data-lever],[data-sign],[data-cancel],[data-tab],[data-goto],[data-phase],[data-artifact],[data-record]");
  if (!t) return;
  if (t.dataset.lever) {
    const key = t.dataset.lever;
    const lever = S.model.levers.find((l) => leverKey(l) === key);
    if (!lever || !lever.enabled || S.busy[key]) return;
    if (lever.action === "decide") { S.open[key] = !S.open[key]; render(); $(`[data-answer]`)?.focus(); }
    else if (lever.confirm) { S.open[key] = !S.open[key]; render(); $(`[data-typed="${CSS.escape(key)}"]`)?.focus(); }
    else pull(key);
  } else if (t.dataset.sign) {
    pull(t.dataset.sign);
  } else if (t.dataset.cancel) {
    S.open[t.dataset.cancel] = false; S.typed[t.dataset.cancel] = ""; render(); $(`[data-lever="${CSS.escape(t.dataset.cancel)}"]`)?.focus();
  } else if (t.dataset.tab) {
    S.tab[t.dataset.tab] = t.dataset.tabId; render();
  } else if (t.dataset.goto) {
    location.hash = t.dataset.goto;
  } else if (t.dataset.phase) {
    location.hash = `#/phases/${t.dataset.phase}`;
  } else if (t.dataset.record) {
    const seq = Number(t.dataset.seq), record = t.dataset.record, phase = S.activity?.phase;
    try {
      const { body } = await api(`/api/record?phase=${phase}&record=${encodeURIComponent(record)}&seq=${seq}`);
      S.recordRow = { record, seq, text: body.map((row) => `${row.at}  ${row.stream}${row.redacted ? "  [redacted]" : ""}\n${row.text}`).join("\n\n") };
    } catch (error) { S.recordRow = { record, seq, text: error.message }; }
    render();
  } else if (t.dataset.artifact) {
    try {
      const { body } = await api(`/api/artifact?path=${encodeURIComponent(t.dataset.artifact)}`);
      let text = body.text;
      try { text = JSON.stringify(JSON.parse(text), null, 2); } catch { /* not JSON */ }
      S.artifact = { path: t.dataset.artifact, text };
    } catch (error) { S.artifact = { path: t.dataset.artifact, text: error.message }; }
    render();
  }
});

document.addEventListener("input", (event) => {
  const key = event.target.dataset?.typed;
  if (!key) return;
  S.typed[key] = event.target.value;
  if (key === "decide-text") {
    const button = $(`[data-sign="decide"]`);
    if (button) button.disabled = !decisionReady();
    return;
  }
  if (event.target.dataset.want !== undefined) {
    const match = event.target.value.trim().toLowerCase() === event.target.dataset.want.toLowerCase();
    const button = key === "drafts-make" ? $("[data-draft-make]") : $(`[data-draft-sign="${CSS.escape(key.slice(6))}"]`);
    if (button) button.disabled = !match;
    event.target.setAttribute("aria-invalid", event.target.value && !match ? "true" : "false");
    return;
  }
  const lever = S.model.levers.find((l) => leverKey(l) === key);
  const c = lever?.confirm;
  if (!c) return;
  const want = c.length ? String(c.value).slice(0, c.length) : String(c.value);
  const match = event.target.value.trim().toLowerCase() === want.toLowerCase();
  const button = $(`[data-sign="${CSS.escape(key)}"]`);
  if (button) button.disabled = !match;
  event.target.setAttribute("aria-invalid", event.target.value && !match ? "true" : "false");
  const hint = $(`#hint-${CSS.escape(key)}`);
  if (hint) hint.textContent = event.target.value && !match ? "Does not match the evidence yet." : match ? "Matches. Signing records your approval in the audit chain." : "The console sends the nonce it showed you, so a stale approval is refused.";
});

document.addEventListener("change", (event) => {
  if (event.target.dataset?.draftRun !== undefined) { S.draftRun = event.target.value; S.tab.drafts = ""; render(); return; }
  if (event.target.dataset?.draftMilestone !== undefined) { S.draftMilestone = event.target.value; S.typed["drafts-make"] = ""; render(); return; }
  if (event.target.dataset?.answer) { S.decision.answer = event.target.dataset.answer; render(); $(`[data-answer="${event.target.dataset.answer}"]`)?.focus(); return; }
  const f = event.target.dataset?.filter;
  if (f) { S.ledger[f] = event.target.value; render(); }
});

document.addEventListener("keydown", (event) => {
  const t = event.target;
  if ((event.key === "Enter" || event.key === " ") && (t.dataset?.goto || t.dataset?.phase || t.dataset?.artifact || t.dataset?.record)) { event.preventDefault(); t.click(); return; }
  if (event.key === "Enter" && t.dataset?.typed) {
    const k = t.dataset.typed;
    const b = k === "drafts-make" ? $("[data-draft-make]") : k.startsWith("draft:") ? $(`[data-draft-sign="${CSS.escape(k.slice(6))}"]`) : $(`[data-sign="${CSS.escape(k)}"]`);
    if (b && !b.disabled) b.click(); return; }
  if (event.key === "Escape" && t.dataset?.typed && (t.dataset.typed === "drafts-make" || t.dataset.typed.startsWith("draft:"))) {
    $(`[data-draft-cancel="${t.dataset.typed === "drafts-make" ? "make" : CSS.escape(t.dataset.typed.slice(6))}"]`)?.click(); return; }
  if (event.key === "Escape" && t.dataset?.typed) { $(`[data-cancel="${CSS.escape(t.dataset.typed)}"]`)?.click(); return; }
  if (t.getAttribute?.("role") === "tab" && (event.key === "ArrowRight" || event.key === "ArrowLeft")) {
    const all = [...t.parentElement.querySelectorAll("[role=tab]")];
    const next = all[(all.indexOf(t) + (event.key === "ArrowRight" ? 1 : all.length - 1)) % all.length];
    S.tab[next.dataset.tab] = next.dataset.tabId; render(); $(`[data-tab-id="${next.dataset.tabId}"]`)?.focus(); return;
  }
  if (["INPUT", "SELECT", "TEXTAREA"].includes(t.tagName) || event.metaKey || event.ctrlKey || event.altKey) return;
  const routes = { 1: "plan", 2: "line", 3: "stop", 4: "phases", 5: "ledger", 6: "setup", 7: "drafts" };
  if (routes[event.key]) location.hash = `#/${routes[event.key]}`;
});

window.addEventListener("hashchange", () => { parseRoute(); render(); $("#main").focus({ preventScroll: true }); refresh(); });
let resizeTimer;
window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(render, 150); });

// ---------- start ----------
(function start() {
  const match = location.hash.match(/token=([\w-]+)/);
  try {
    const landing = (location.hash.match(/&r=([\w/]+)/) || [])[1] || "/plan";
    if (match) { sessionStorage.setItem("buildctl-token", match[1]); history.replaceState(null, "", location.pathname + "#" + landing); }
    S.token = match ? match[1] : sessionStorage.getItem("buildctl-token") || "";
  } catch { S.token = match ? match[1] : ""; if (match) history.replaceState(null, "", location.pathname + "#/plan"); }
  parseRoute();
  refresh().then(() => {
    if (S.model && !S.model.bootstrapped && ["plan", "line"].includes(S.route.view)) location.hash = "#/setup";
  });
  setInterval(refresh, 2000);
  setInterval(tick, 1000);
})();
