"use strict";

// Board state: sessions keyed by id, hosts keyed by id.
const state = {
  sessions: new Map(), hosts: new Map(), skew: 0,
  filter: "all", showEnded: false, minVersion: null,
};

const STATE_ORDER = { needs_you: 0, working: 1, idle: 2, ended: 3 };
const STATE_LABEL = { needs_you: "Needs you", working: "Working", idle: "Idle", ended: "Ended" };
const ALERT_TEXT = {
  stuck: a => `No activity for ${ago(a.since)} while working — possibly stuck`,
  dead: () => "Process gone without SessionEnd — investigate",
  offline: a => `No heartbeat for ${ago(a.since)}`,
};

// --- helpers ---------------------------------------------------------------

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "title") el.title = v;
    else el.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const now = () => Date.now() / 1000 + state.skew;

function ago(ts) {
  if (!ts) return "–";
  let s = Math.max(0, Math.round(now() - ts));
  if (s < 60) return `${s}s`;
  let m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  let hrs = Math.floor(m / 60);
  if (hrs < 48) return `${hrs}h ${m % 60}m`;
  return `${Math.floor(hrs / 24)}d ${hrs % 24}h`;
}

function basename(path) {
  if (!path) return "?";
  const parts = path.replace(/\/+$/, "").split("/");
  return parts[parts.length - 1] || "/";
}

// "2.1.330" -> [2, 1, 330]
function versionKey(v) {
  if (!v) return null;
  const parts = String(v).split("-")[0].match(/\d+/g);
  return parts ? parts.map(Number) : null;
}

function cmpVersion(a, b) {
  for (let i = 0; i < Math.max(a.length, b.length); i++) {
    const d = (a[i] || 0) - (b[i] || 0);
    if (d) return d;
  }
  return 0;
}

// Reference version: configured minimum, else newest version seen anywhere.
function referenceVersion() {
  if (state.minVersion) return state.minVersion;
  let best = null;
  const consider = v => { const k = versionKey(v); if (k && (!best || cmpVersion(k, best.k) > 0)) best = { k, v }; };
  for (const host of state.hosts.values()) consider(host.claude_installed_version);
  for (const s of state.sessions.values()) if (s.state !== "ended") consider(s.claude_version);
  return best ? best.v : null;
}

function hostOffline(host) {
  return !!host && (host.alerts || []).some(a => a.kind === "offline");
}

function hostOutdated(host, ref) {
  const k = versionKey(host.claude_installed_version), r = versionKey(ref);
  return !!(k && r && cmpVersion(k, r) < 0);
}

function restartPending(s) {
  const host = state.hosts.get(s.host_id);
  const run = versionKey(s.claude_version), inst = versionKey(host && host.claude_installed_version);
  return s.state !== "ended" && !!(run && inst && cmpVersion(run, inst) < 0);
}

function needsAttention(s) {
  return s.state === "needs_you" || (s.alerts || []).length > 0 ||
    (s.state !== "ended" && hostOffline(state.hosts.get(s.host_id)));
}

function matchesFilter(s) {
  if (s.state === "ended" && !state.showEnded) return false;
  switch (state.filter) {
    case "attention": return needsAttention(s);
    case "working": return s.state === "working";
    case "idle": return s.state === "idle";
    default: return true;
  }
}

// --- rendering -------------------------------------------------------------

function renderSubagents(s) {
  if (!s.subagents || !s.subagents.length) return null;
  return h("ul", { class: "subagents" }, s.subagents.map(a => {
    const running = !a.ended_at;
    return h("li", { class: running ? "running" : "done" },
      h("span", { class: "sa-icon", "aria-hidden": "true" }, running ? "◐" : "✓"),
      h("span", { class: "sa-type" }, a.agent_type || "agent"),
      a.description ? h("span", { class: "sa-desc" }, a.description) : null,
      running && a.activity ? h("span", { class: "sa-act" }, a.activity) : null,
      h("span", { class: "sa-time" }, running ? ago(a.started_at) : "done"),
    );
  }));
}

function renderAlerts(alerts, extra = []) {
  const items = [
    ...alerts.map(a => ({ kind: a.kind, text: ALERT_TEXT[a.kind]
      ? ALERT_TEXT[a.kind]({ since: a.kind === "stuck" ? a._since : a.opened_at }) : a.kind, acked: !!a.acked_at })),
    ...extra,
  ];
  if (!items.length) return null;
  return h("ul", { class: "alerts" }, items.map(i =>
    h("li", { class: `alert alert-${i.kind}${i.acked ? " acked" : ""}` }, i.text)));
}

function renderCard(s) {
  const host = state.hosts.get(s.host_id);
  const offline = s.state !== "ended" && hostOffline(host);
  const pane = s.tmux_target || s.tmux_pane || "no tmux";
  let detail;
  if (s.state === "ended") {
    detail = `Ended ${ago(s.ended_at)} ago` + (s.end_reason ? ` · ${s.end_reason}` : "");
  } else if (s.state === "needs_you") {
    detail = s.activity ? `Waiting for permission · ${s.activity}` : "Waiting for permission";
  } else if (s.state === "idle") {
    detail = s.bg_tasks ? `Waiting for input · ${s.bg_tasks} background task(s)` : "Waiting for input";
  } else {
    detail = s.activity || "Working";
  }
  const alerts = (s.alerts || []).map(a => ({ ...a, _since: s.last_event_at }));
  const extra = offline ? [{ kind: "offline", text: "Host offline — state unknown" }] : [];
  const classes = ["card", `state-${s.state}`];
  if (alerts.length) classes.push("has-alert");
  if (offline) classes.push("host-offline");
  return h("article", { class: classes.join(" "), "data-id": s.id },
    h("div", { class: "card-head" },
      h("span", { class: "badge" }, STATE_LABEL[s.state] || s.state),
      h("span", { class: "since", title: "Time in this state" }, ago(s.state_since)),
    ),
    h("h3", { class: "project", title: s.cwd || "" }, basename(s.cwd)),
    h("p", { class: "activity", title: detail }, detail),
    renderAlerts(alerts, extra),
    renderSubagents(s),
    h("dl", { class: "meta" },
      h("div", {}, h("dt", {}, "pane"), h("dd", {}, pane)),
      s.model ? h("div", {}, h("dt", {}, "model"), h("dd", {}, s.model)) : null,
      s.claude_version ? h("div", {}, h("dt", {}, "claude"), h("dd", {}, s.claude_version,
        restartPending(s) ? h("span", { class: "flag flag-restart",
          title: `Host has ${host.claude_installed_version} installed; restart the session to use it` }, "restart pending") : null))
        : null,
      h("div", {}, h("dt", {}, "last event"), h("dd", {}, ago(s.last_event_at))),
    ),
  );
}

function renderHostHead(host, ref) {
  const offline = hostOffline(host);
  let status, statusClass;
  if (offline) { status = `offline · last heartbeat ${ago(host.last_heartbeat_at)} ago`; statusClass = "offline"; }
  else if (host.last_heartbeat_at) { status = `online · heartbeat ${ago(host.last_heartbeat_at)} ago`; statusClass = "online"; }
  else { status = host.last_seen_at ? `no heartbeat · last event ${ago(host.last_seen_at)} ago` : "never seen"; statusClass = "unknown"; }
  const outdated = hostOutdated(host, ref);
  return h("header", { class: "host-head" },
    h("span", { class: `host-dot ${statusClass}`, "aria-hidden": "true" }),
    h("h2", {}, host.name),
    h("span", { class: "host-meta" }, status),
    host.claude_installed_version ? h("span", { class: "host-meta" }, `claude ${host.claude_installed_version}`,
      outdated ? h("span", { class: "flag flag-outdated", title: `Newest in fleet: ${ref}` }, `outdated · ${ref} available`) : null)
      : null,
    host.os_family ? h("span", { class: "host-meta" }, host.os_family) : null,
    host.client_version ? h("span", { class: "host-meta" }, `client ${host.client_version}`) : null,
  );
}

function renderSummary(sessions) {
  const counts = { needs_you: 0, working: 0, idle: 0 };
  for (const s of sessions) if (s.state in counts) counts[s.state]++;
  let alerts = 0;
  for (const s of sessions) alerts += (s.alerts || []).length;
  for (const host of state.hosts.values()) alerts += (host.alerts || []).length;
  const el = document.getElementById("summary");
  el.replaceChildren(
    h("span", { class: "sum needs_you" }, `${counts.needs_you} need you`),
    h("span", { class: "sum working" }, `${counts.working} working`),
    h("span", { class: "sum idle" }, `${counts.idle} idle`),
    alerts ? h("span", { class: "sum sum-alerts" }, `${alerts} alert${alerts > 1 ? "s" : ""}`) : null,
  );
  const attention = counts.needs_you + alerts;
  document.title = attention ? `(${attention}) Dash4ClCo` : "Dash4ClCo";
}

function render() {
  const all = [...state.sessions.values()];
  renderSummary(all);
  const ref = referenceVersion();
  const visible = all.filter(matchesFilter);
  const byHost = new Map();
  for (const s of visible) {
    if (!byHost.has(s.host_id)) byHost.set(s.host_id, []);
    byHost.get(s.host_id).push(s);
  }
  // Hosts with their own alerts are always shown; on "All" every host is shown.
  for (const host of state.hosts.values()) {
    const show = state.filter === "all" || (state.filter === "attention" && hostOffline(host));
    if (show && !byHost.has(host.id)) byHost.set(host.id, []);
  }
  const board = document.getElementById("board");
  if (!byHost.size) {
    board.replaceChildren(h("p", { class: "empty" },
      state.sessions.size || state.hosts.size ? "Nothing matches this filter." :
        "No hosts yet. Register one with d4cc-admin add-host and install the client."));
    return;
  }
  const groups = [...byHost.entries()].sort((a, b) => {
    const ha = state.hosts.get(a[0]), hb = state.hosts.get(b[0]);
    const oa = hostOffline(ha) ? 0 : 1, ob = hostOffline(hb) ? 0 : 1;
    return (oa - ob) || (ha?.name || "").localeCompare(hb?.name || "");
  });
  board.replaceChildren(...groups.map(([hostId, list]) => {
    const host = state.hosts.get(hostId) || { id: hostId, name: `host #${hostId}`, alerts: [] };
    list.sort((a, b) => (needsAttention(b) - needsAttention(a)) ||
      (STATE_ORDER[a.state] - STATE_ORDER[b.state]) || (a.started_at - b.started_at));
    return h("section", { class: `host${hostOffline(host) ? " offline" : ""}` },
      renderHostHead(host, ref),
      list.length ? h("div", { class: "cards" }, list.map(renderCard))
        : h("p", { class: "host-empty" }, "No sessions"),
    );
  }));
}

// --- data flow -------------------------------------------------------------

async function loadBoard() {
  const res = await fetch("/api/board", { credentials: "same-origin" });
  if (res.status === 401) { location.href = "/login"; return; }
  const data = await res.json();
  state.skew = data.now - Date.now() / 1000;
  state.minVersion = data.min_claude_version;
  state.hosts = new Map(data.hosts.map(x => [x.id, x]));
  state.sessions = new Map(data.sessions.map(x => [x.id, x]));
  render();
}

function setConn(ok, label) {
  const el = document.getElementById("conn");
  el.classList.toggle("ok", ok);
  el.querySelector(".label").textContent = label;
}

function connect() {
  const es = new EventSource("/api/stream");
  es.addEventListener("open", () => { setConn(true, "live"); loadBoard(); });
  es.addEventListener("error", () => setConn(false, "reconnecting"));
  es.addEventListener("session", ev => {
    const s = JSON.parse(ev.data);
    state.sessions.set(s.id, s);
    if (!state.hosts.has(s.host_id)) loadBoard(); else render();
  });
  es.addEventListener("host", ev => {
    const host = JSON.parse(ev.data);
    state.hosts.set(host.id, host);
    render();
  });
  es.addEventListener("resync", () => loadBoard());
}

document.getElementById("filters").addEventListener("click", ev => {
  const btn = ev.target.closest("button[data-filter]");
  if (!btn) return;
  state.filter = btn.dataset.filter;
  for (const b of document.querySelectorAll("#filters button")) b.classList.toggle("active", b === btn);
  render();
});
document.getElementById("show-ended").addEventListener("change", ev => {
  state.showEnded = ev.target.checked;
  render();
});

setInterval(render, 5000);  // keep durations fresh
loadBoard();
connect();
