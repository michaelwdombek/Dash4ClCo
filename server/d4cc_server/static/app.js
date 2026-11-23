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
    ...alerts.map(a => ({ id: a.id, kind: a.kind, acked: !!a.acked_at, note: a.note,
      text: ALERT_TEXT[a.kind] ? ALERT_TEXT[a.kind]({ since: a.kind === "stuck" ? a._since : a.opened_at }) : a.kind })),
    ...extra,
  ];
  if (!items.length) return null;
  return h("ul", { class: "alerts" }, items.map(i =>
    h("li", { class: `alert alert-${i.kind}${i.acked ? " acked" : ""}` },
      h("span", { class: "alert-text" }, i.text,
        i.acked ? h("span", { class: "alert-ack" }, i.note ? `acknowledged · ${i.note}` : "acknowledged") : null),
      i.id && !i.acked ? h("button", { class: "mini", "data-action": "ack", "data-alert": i.id,
        title: "Acknowledge this alert" }, "Ack") : null,
    )));
}

function renderMarker(s) {
  if (!s.marker_since) return null;
  return h("div", { class: "marker", title: "You marked this session as being worked on" },
    h("span", { class: "marker-label" }, "On it"),
    s.marker_note ? h("span", { class: "marker-note" }, s.marker_note) : null,
    h("span", { class: "marker-since" }, ago(s.marker_since)),
  );
}

function renderActions(s, offline) {
  const dead = (s.alerts || []).some(a => a.kind === "dead");
  const btn = (action, label, title) => h("button", { class: "mini", "data-action": action, title }, label);
  return h("div", { class: "card-actions" },
    s.state !== "ended" ? (s.marker_since
      ? btn("marker-clear", "Clear “on it”", "Remove the working-on-it marker")
      : btn("marker", "On it", "Mark that you are working on this")) : null,
    btn("note", "Note", "Add a note to the history"),
    btn("history", "History", "Show the session timeline"),
    s.state !== "ended" && (dead || offline)
      ? btn("end", "Mark ended", "The session is gone: end the card manually") : null,
    s.state === "ended" ? btn("archive", "Archive", "Hide this card (history is kept)") : null,
  );
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
  if (s.marker_since) classes.push("marked");
  return h("article", { class: classes.join(" "), "data-id": s.id },
    h("div", { class: "card-head" },
      h("span", { class: "badge" }, STATE_LABEL[s.state] || s.state),
      h("span", { class: "since", title: "Time in this state" }, ago(s.state_since)),
    ),
    h("h3", { class: "project", title: s.cwd || "" }, basename(s.cwd)),
    h("p", { class: "activity", title: detail }, detail),
    renderMarker(s),
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
    renderActions(s, offline),
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
    ...(host.alerts || []).filter(a => !a.acked_at).map(a =>
      h("button", { class: "mini", "data-action": "ack", "data-alert": a.id, title: "Acknowledge offline alert" }, "Ack")),
    ...(host.alerts || []).filter(a => a.acked_at).map(a =>
      h("span", { class: "host-meta acked" }, a.note ? `acknowledged · ${a.note}` : "acknowledged")),
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
  // Acknowledged alerts and sessions marked "on it" are handled: not counted in the title.
  const unhandledNeedsYou = sessions.filter(s => s.state === "needs_you" && !s.marker_since).length;
  let alerts = 0;
  for (const s of sessions) if (!s.marker_since) alerts += (s.alerts || []).filter(a => !a.acked_at).length;
  for (const host of state.hosts.values()) alerts += (host.alerts || []).filter(a => !a.acked_at).length;
  const el = document.getElementById("summary");
  el.replaceChildren(
    h("span", { class: "sum needs_you" }, `${counts.needs_you} need you`),
    h("span", { class: "sum working" }, `${counts.working} working`),
    h("span", { class: "sum idle" }, `${counts.idle} idle`),
    ...(alerts ? [h("span", { class: "sum sum-alerts" }, `${alerts} alert${alerts > 1 ? "s" : ""}`)] : []),
  );
  const attention = unhandledNeedsYou + alerts;
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
    const prio = s => needsAttention(s) ? (s.marker_since ? 1 : 2) : 0;
    list.sort((a, b) => (prio(b) - prio(a)) ||
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

// --- actions ---------------------------------------------------------------

async function api(method, url, body) {
  const res = await fetch(url, {
    method, credentials: "same-origin",
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) { location.href = "/login"; throw new Error("login required"); }
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (_) { /* not JSON */ }
    throw new Error(msg);
  }
  return res.json();
}

// Small promise-based text prompt in a <dialog>. Resolves to the text, or null on cancel.
function askText({ title, label, placeholder = "", confirm = "Save", required = false }) {
  const dlg = document.getElementById("ask");
  dlg.querySelector("h2").textContent = title;
  dlg.querySelector("label").textContent = label;
  const input = dlg.querySelector("textarea");
  input.value = "";
  input.placeholder = placeholder;
  input.required = required;
  dlg.querySelector("button[value=ok]").textContent = confirm;
  dlg.returnValue = "";
  dlg.showModal();
  input.focus();
  return new Promise(resolve => {
    dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok" ? input.value.trim() : null), { once: true });
  });
}

function toast(msg) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.classList.add("show");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => el.classList.remove("show"), 4000);
}

const ACTIONS = {
  async marker(sid) {
    const note = await askText({ title: "Working on it", label: "What are you doing? (optional)",
      placeholder: "e.g. checking the stuck molecule run", confirm: "Mark" });
    if (note !== null) await api("POST", `/api/sessions/${sid}/marker`, { note });
  },
  async "marker-clear"(sid) { await api("DELETE", `/api/sessions/${sid}/marker`); },
  async note(sid) {
    const text = await askText({ title: "Add note", label: "Note (kept in the session history)", required: true });
    if (text) await api("POST", `/api/sessions/${sid}/notes`, { text });
  },
  async end(sid) {
    const note = await askText({ title: "Mark session ended", label: "Reason (optional)",
      placeholder: "e.g. pane was killed, machine rebooted", confirm: "Mark ended" });
    if (note !== null) await api("POST", `/api/sessions/${sid}/end`, { note });
  },
  async archive(sid) {
    await api("POST", `/api/sessions/${sid}/archive`, {});
    state.sessions.delete(sid);
    render();
  },
  async ack(_sid, btn) {
    const note = await askText({ title: "Acknowledge alert", label: "Note (optional)",
      placeholder: "e.g. VPN gateway maintenance", confirm: "Acknowledge" });
    if (note !== null) await api("POST", `/api/alerts/${btn.dataset.alert}/ack`, { note });
  },
  async history(sid) { await openHistory(sid); },
};

document.getElementById("board").addEventListener("click", async ev => {
  const btn = ev.target.closest("button[data-action]");
  if (!btn) return;
  const card = btn.closest("[data-id]");
  try {
    await ACTIONS[btn.dataset.action](card ? card.dataset.id : null, btn);
  } catch (err) {
    toast(`Action failed: ${err.message}`);
  }
});

// --- history drawer ----------------------------------------------------------

function fmtTime(ts) {
  const d = new Date((ts - state.skew) * 1000);
  const today = new Date().toDateString() === d.toDateString();
  const time = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  return today ? time : `${d.toLocaleDateString()} ${time}`;
}

const ALERT_NAMES = { stuck: "Stuck", dead: "Dead", offline: "Host offline" };

function describe(item) {
  const who = item.agent_type ? `[${item.agent_type}] ` : "";
  switch (item.type) {
    case "SessionStart": return ["start", `Session started (${item.source || "unknown"})`];
    case "SessionEnd": return ["end", `Session ended (${item.reason || "unknown"})`];
    case "UserPromptSubmit":
      return ["prompt", item.is_agent_message ? "Subagent result handed back" :
        (item.prompt_excerpt ? `Prompt: ${item.prompt_excerpt}` : "Prompt submitted")];
    case "PreToolUse":
      return ["tool", `${who}${item.tool_name || "tool"}${item.tool_summary ? ": " + item.tool_summary : ""}`];
    case "PostToolUse": return ["tool-done", `${who}${item.tool_name || "tool"} finished`];
    case "Notification":
      return ["notify", item.notification_type === "permission_prompt" ? "Waiting for permission" :
        item.notification_type === "idle_prompt" ? "Idle, waiting for input" : `Notification (${item.notification_type})`];
    case "Stop":
      return ["stop", item.background_task_count
        ? `Turn finished · ${item.background_task_count} background task(s)` : "Turn finished"];
    case "SubagentStart": return ["agent", `Subagent started: ${item.agent_type}`];
    case "SubagentStop": return ["agent", `Subagent finished${item.agent_type ? ": " + item.agent_type : ""}`];
    case "alert_opened": return ["alert", `Alert: ${ALERT_NAMES[item.kind] || item.kind}`];
    case "alert_resolved":
      return ["resolved", `Alert resolved: ${ALERT_NAMES[item.kind] || item.kind} (${item.resolution})`];
    case "marker_set": return ["you", `You: on it${item.text ? " — " + item.text : ""}`];
    case "marker_cleared": return ["you", "You: cleared “on it”"];
    case "note": return ["you", `Note: ${item.text}`];
    case "ack": return ["you", `You acknowledged an alert${item.text ? " — " + item.text : ""}`];
    case "ended": return ["you", `You marked the session ended${item.text ? " — " + item.text : ""}`];
    case "archived": return ["you", "You archived the session"];
    case "unarchived": return ["you", "You restored the session"];
    default: return ["other", item.type];
  }
}

async function openHistory(sid) {
  const dlg = document.getElementById("history");
  const list = dlg.querySelector(".timeline");
  const showTools = dlg.querySelector("#show-tool-results");
  list.replaceChildren(h("li", { class: "muted" }, "Loading…"));
  dlg.showModal();
  const data = await api("GET", `/api/sessions/${sid}/history`);
  const s = data.session;
  dlg.querySelector("h2").textContent = basename(s.cwd);
  dlg.querySelector(".history-meta").textContent =
    `${state.hosts.get(s.host_id)?.name || ""} · ${s.tmux_target || s.tmux_pane || "no tmux"} · started ${fmtTime(s.started_at)}`;
  const draw = () => {
    const items = data.items.filter(i => showTools.checked || i.type !== "PostToolUse");
    list.replaceChildren(...items.map(item => {
      const [cls, text] = describe(item);
      return h("li", { class: `tl tl-${cls}${item.agent_id ? " tl-sub" : ""}` },
        h("time", {}, fmtTime(item.at)), h("span", {}, text));
    }));
    if (data.truncated) list.append(h("li", { class: "muted" }, "Older events not shown."));
  };
  showTools.onchange = draw;
  draw();
}

document.querySelectorAll("dialog [data-close]").forEach(b =>
  b.addEventListener("click", () => b.closest("dialog").close()));

setInterval(render, 5000);  // keep durations fresh
loadBoard();
connect();
