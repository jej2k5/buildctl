"use strict";
// buildctl console. Renders the kernel's read model; pulls levers through the console server,
// which runs the buildctl CLI. Nothing here decides what is allowed: the model's levers do.

const S = {
  token: "", model: null, fingerprint: "", route: { view: "line", arg: null },
  tab: {}, open: {}, typed: {}, results: {}, stamps: {}, busy: {},
  audit: [], auditSeq: 0, ledger: { phase: "", event: "" },
  cells: {}, trustDiff: null, trustDiffKey: "", artifact: null, error: null,
};
const PRIMARY = ["start", "recover", "pause", "resume", "approve", "grant", "retry", "reconcile-accept", "reconcile-retry"];
const STOP_LEVERS = ["approve", "grant", "retry", "reconcile-accept", "reconcile-retry", "recover"];
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
  const ops = phaseByNumber(n)?.operations || [];
  return ops.find((o) => o.status === "started") || ops.find((o) => o.status === "unknown") || ops[ops.length - 1] || null;
}

// ---------- levers ----------
function renderLevers(levers, { emptyText = "No levers apply here." } = {}) {
  if (!levers.length) return `<div class="interlock"><p class="empty">${esc(emptyText)}</p></div>`;
  return `<div class="interlock">${levers.map(renderLever).join("")}</div>`;
}

function buttonLabel(lever) {
  return { approve: "Review and sign", grant: "Authorize…", "reconcile-accept": "Accept…", "reconcile-retry": "Authorize retry…", trust: "Pin this commit…" }[lever.action] || lever.label;
}

function renderLever(lever) {
  const key = leverKey(lever);
  const blocked = lever.conditions.filter((c) => c.held === false);
  const locked = !lever.enabled;
  const busy = S.busy[key];
  const button = `<button class="btn ${lever.confirm ? "quiet" : ""} ${busy ? "busy" : ""}" data-lever="${esc(key)}"
      ${locked ? `disabled aria-describedby="lock-${esc(key)}"` : ""} ${S.open[key] ? 'aria-expanded="true"' : lever.confirm ? 'aria-expanded="false"' : ""}>
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
      ${conds}${note}${S.open[key] && lever.enabled ? renderCeremony(lever, key) : ""}${renderResult(key)}
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

function renderResult(key) {
  const stamp = S.stamps[key];
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
      body: JSON.stringify({ action: lever.action, generation: S.model.state.generation, confirm: S.typed[key] || "", operation: leverOperation(lever) }),
    });
    const ok = status < 300;
    const text = body.detached ? `Started ${body.command} as pid ${body.pid}. It keeps running if you close the console.`
      : ok ? `${body.command} finished.` : `${body.command} exited ${body.returncode}.`;
    S.results[key] = { ok, text, output: body.output };
    if (ok && lever.confirm) {
      S.stamps[key] = `${lever.label.toLowerCase()} · ${new Date().toLocaleTimeString()}`;
      // The lever disappears once the kernel accepts it, so the receipt lives outside the lever.
      S.signed = { label: lever.label, summary: lever.summary, at: new Date().toLocaleTimeString(), command: body.command };
    }
    if (ok) { S.open[key] = false; S.typed[key] = ""; }
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
  return `<div class="panel op">
    <div class="op-head">${sig(aspect, op.status)}<h3>${running ? "In flight" : op.status === "unknown" ? "Outcome unknown" : "Last call"}: ${esc(op.kind)}</h3></div>
    <dl>
      <dt>Operation</dt><dd><code>${esc(op.id)}</code></dd>
      <dt>Status</dt><dd>${esc(op.status)}${op.detail_code ? ` · <code>${esc(op.detail_code)}</code>` : ""}</dd>
      <dt>Started</dt><dd>${op.started_at ? `${esc(clock(op.started_at))} · <span data-since="${esc(op.started_at)}" ${op.completed_at ? `data-until="${esc(op.completed_at)}"` : ""}>${esc(span(op.started_at, op.completed_at))}</span>` : "not started"}</dd>
      ${op.output_digest ? `<dt>Output</dt><dd><code>${esc(short(op.output_digest, 16))}</code></dd>` : ""}
    </dl>${deadlineBar || (deadline && !running ? `<p class="muted">Nothing is in flight. A retry starts a new attempt with a fresh ${limit}-minute deadline.</p>` : "")}</div>`;
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
  if (m.state.pending_approval) entries.push({ id: "approval", label: "What you are signing", render: () => approvalPanel(m) });
  entries.push({ id: "findings", label: "Findings", count: (m.current?.review?.findings || m.findings.open).filter((f) => !["resolved", "superseded"].includes(f.status)).length, render: () => findingsPanel(m) });
  entries.push({ id: "operations", label: "Agent calls", count: p?.operations.length || 0, render: () => operationsPanel(p?.operations) });
  entries.push({ id: "gates", label: "Gates", render: () => gatesPanel(m) });
  entries.push({ id: "artifacts", label: "Artifacts", count: m.current?.artifacts.length || 0, render: () => artifactsPanel(m, m.current?.artifacts) });
  if (m.current?.debate_request) entries.push({ id: "debate", label: "Debate request", render: () => `<p class="panel-pad">Run the debate, write the result to the path the request names, then retry.<br><code>${esc(m.current.debate_request)}</code></p>` });
  const levers = m.levers.filter((l) => STOP_LEVERS.includes(l.action));
  return `<div class="view-head"><h1>Stop</h1><p>Generation ${m.state.generation} · ${esc(ago(m.state.updated_at))}</p></div>
    ${stopBlock(m, false)}
    <div class="cols section">
      <section><h2 class="h2">Evidence</h2>${tabs("stop", entries)}</section>
      <aside><h2 class="h2">Levers <small>Only what the kernel will accept.</small></h2>${renderLevers(levers, { emptyText: "No lever applies. Fix the cause outside buildctl, then come back." })}</aside>
    </div>`;
}

function viewPhases(m) {
  const selected = S.route.arg != null ? phaseByNumber(Number(S.route.arg)) : null;
  const rows = m.phases.map((p) => `<tr class="row-link" tabindex="0" data-goto="#/phases/${p.number}" aria-selected="${selected === p}">
      <td class="num mono">${p.number}</td><td>${sig(p.aspect)}</td><td>${esc(p.name)}${p.checkpoint ? ` <span class="tag">checkpoint</span>` : ""}${p.milestones.map((s) => ` <span class="tag">${esc(s)}</span>`).join("")}</td>
      <td class="num mono" data-cell="att-${p.number}">${p.attempts || ""}</td><td class="num mono" data-cell="rev-${p.number}">${p.reviews || ""}</td>
      <td class="num mono" data-cell="rem-${p.number}">${p.remediations || ""}</td><td class="num mono" data-cell="deb-${p.number}">${p.debates || ""}</td>
      <td class="num mono" data-cell="cost-${p.number}">${p.cost_usd ? money(p.cost_usd) : ""}</td><td class="mono">${esc(p.merge_policy)}</td></tr>`).join("");
  const table = `<div class="table-wrap"><table><thead><tr><th class="num">#</th><th><span class="announce">Aspect</span></th><th>Phase</th><th class="num">Attempts</th><th class="num">Reviews</th><th class="num">Fixes</th><th class="num">Debates</th><th class="num">Cost</th><th>Merge</th></tr></thead><tbody>${rows}</tbody></table></div>`;
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
      <div class="section">${tabs("phase", [
        { id: "operations", label: "Agent calls", count: selected.operations.length, render: () => operationsPanel(selected.operations) },
        ...(isCurrent ? [{ id: "findings", label: "Findings", render: () => findingsPanel(m) }, { id: "artifacts", label: "Artifacts", count: m.current?.artifacts.length || 0, render: () => artifactsPanel(m, m.current?.artifacts) }] : []),
      ])}</div></section>`;
  }
  return `<div class="view-head"><h1>Phases</h1><p>${m.phases.length} in build.yaml (${esc(m.config.source || "unreadable")})</p></div>${detail}<div class="section">${table}</div>`;
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
  const view = { line: viewLine, stop: viewStop, phases: viewPhases, ledger: viewLedger, setup: viewSetup }[S.route.view] || viewLine;
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
    if (changed) render();
  } catch (error) {
    if (error.status === 401) {
      $("#main").innerHTML = `<div class="panel"><div class="empty"><p>${sig("danger", "refused")} This tab has no console token.</p><p>Open the link <code>buildctl-ui</code> printed in your terminal. The token in it is what lets this page read the build.</p></div></div>`;
    } else if (!S.error) {
      S.error = error;
      $("#bar-aspect").innerHTML = `${sig("unknown", "unreachable", "lg")}<span><b>Console unreachable</b><br><span class="mono">${esc(error.message)}</span></span>`;
    }
  }
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
  const [, view = "line", arg = null] = (location.hash.replace(/^#/, "") || "/line").split("/");
  S.route = { view, arg };
  if (view !== "stop" && view !== "phases") S.artifact = null;
}

// ---------- events ----------
document.addEventListener("click", async (event) => {
  if (event.target.closest("[data-dismiss-receipt]")) { S.signed = null; render(); return; }
  const t = event.target.closest("[data-lever],[data-sign],[data-cancel],[data-tab],[data-goto],[data-phase],[data-artifact]");
  if (!t) return;
  if (t.dataset.lever) {
    const key = t.dataset.lever;
    const lever = S.model.levers.find((l) => leverKey(l) === key);
    if (!lever || !lever.enabled || S.busy[key]) return;
    if (lever.confirm) { S.open[key] = !S.open[key]; render(); $(`[data-typed="${CSS.escape(key)}"]`)?.focus(); }
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
  const f = event.target.dataset?.filter;
  if (f) { S.ledger[f] = event.target.value; render(); }
});

document.addEventListener("keydown", (event) => {
  const t = event.target;
  if ((event.key === "Enter" || event.key === " ") && (t.dataset?.goto || t.dataset?.phase || t.dataset?.artifact)) { event.preventDefault(); t.click(); return; }
  if (event.key === "Enter" && t.dataset?.typed) { const b = $(`[data-sign="${CSS.escape(t.dataset.typed)}"]`); if (b && !b.disabled) b.click(); return; }
  if (event.key === "Escape" && t.dataset?.typed) { $(`[data-cancel="${CSS.escape(t.dataset.typed)}"]`)?.click(); return; }
  if (t.getAttribute?.("role") === "tab" && (event.key === "ArrowRight" || event.key === "ArrowLeft")) {
    const all = [...t.parentElement.querySelectorAll("[role=tab]")];
    const next = all[(all.indexOf(t) + (event.key === "ArrowRight" ? 1 : all.length - 1)) % all.length];
    S.tab[next.dataset.tab] = next.dataset.tabId; render(); $(`[data-tab-id="${next.dataset.tabId}"]`)?.focus(); return;
  }
  if (["INPUT", "SELECT", "TEXTAREA"].includes(t.tagName) || event.metaKey || event.ctrlKey || event.altKey) return;
  const routes = { 1: "line", 2: "stop", 3: "phases", 4: "ledger", 5: "setup" };
  if (routes[event.key]) location.hash = `#/${routes[event.key]}`;
});

window.addEventListener("hashchange", () => { parseRoute(); render(); $("#main").focus({ preventScroll: true }); refresh(); });
let resizeTimer;
window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(render, 150); });

// ---------- start ----------
(function start() {
  const match = location.hash.match(/token=([\w-]+)/);
  try {
    const landing = (location.hash.match(/&r=([\w/]+)/) || [])[1] || "/line";
    if (match) { sessionStorage.setItem("buildctl-token", match[1]); history.replaceState(null, "", location.pathname + "#" + landing); }
    S.token = match ? match[1] : sessionStorage.getItem("buildctl-token") || "";
  } catch { S.token = match ? match[1] : ""; if (match) history.replaceState(null, "", location.pathname + "#/line"); }
  parseRoute();
  refresh().then(() => {
    if (S.model && !S.model.bootstrapped && S.route.view === "line") location.hash = "#/setup";
  });
  setInterval(refresh, 2000);
  setInterval(tick, 1000);
})();
