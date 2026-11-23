"use strict";

// Board state: sessions keyed by id, hosts keyed by id.
const state = { sessions: new Map(), hosts: new Map(), skew: 0, filter: "all", showEnded: false };

const STATE_ORDER = { needs_you: 0, working: 1, idle: 2, ended: 3 };
const STATE_LABEL = { needs_you: "Needs you", working: "Working", idle: "Idle", ended: "Ended" };

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

function matchesFilter(s) {
  if (s.state === "ended" && !state.showEnded) return false;
  switch (state.filter) {
    case "attention": return s.state === "needs_you";
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

function renderCard(s) {
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
  return h("article", { class: `card state-${s.state}`, "data-id": s.id },
    h("div", { class: "card-head" },
      h("span", { class: "badge" }, STATE_LABEL[s.state] || s.state),
      h("span", { class: "since", title: "Time in this state" }, ago(s.state_since)),
    ),
    h("h3", { class: "project", title: s.cwd || "" }, basename(s.cwd)),
    h("p", { class: "activity", title: detail }, detail),
    renderSubagents(s),
    h("dl", { class: "meta" },
      h("div", {}, h("dt", {}, "pane"), h("dd", {}, pane)),
      s.model ? h("div", {}, h("dt", {}, "model"), h("dd", {}, s.model)) : null,
      s.claude_version ? h("div", {}, h("dt", {}, "claude"), h("dd", {}, s.claude_version)) : null,
      h("div", {}, h("dt", {}, "last event"), h("dd", {}, ago(s.last_event_at))),
    ),
  );
}

function renderSummary(sessions) {
  const counts = { needs_you: 0, working: 0, idle: 0 };
  for (const s of sessions) if (s.state in counts) counts[s.state]++;
  const el = document.getElementById("summary");
  el.replaceChildren(
    h("span", { class: "sum needs_you" }, `${counts.needs_you} need you`),
    h("span", { class: "sum working" }, `${counts.working} working`),
    h("span", { class: "sum idle" }, `${counts.idle} idle`),
  );
  document.title = counts.needs_you ? `(${counts.needs_you}) Dash4ClCo` : "Dash4ClCo";
}

function render() {
  const all = [...state.sessions.values()];
  renderSummary(all);
  const visible = all.filter(matchesFilter);
  const byHost = new Map();
  for (const s of visible) {
    if (!byHost.has(s.host_id)) byHost.set(s.host_id, []);
    byHost.get(s.host_id).push(s);
  }
  const board = document.getElementById("board");
  if (!visible.length) {
    board.replaceChildren(h("p", { class: "empty" },
      state.sessions.size ? "No sessions match this filter." :
        "No sessions yet. Start Claude Code on a host with the d4cc hook installed."));
    return;
  }
  const groups = [...byHost.entries()].sort((a, b) => {
    const ha = state.hosts.get(a[0])?.name || "", hb = state.hosts.get(b[0])?.name || "";
    return ha.localeCompare(hb);
  });
  board.replaceChildren(...groups.map(([hostId, list]) => {
    const host = state.hosts.get(hostId) || { name: `host #${hostId}` };
    list.sort((a, b) => (STATE_ORDER[a.state] - STATE_ORDER[b.state]) || (a.started_at - b.started_at));
    return h("section", { class: "host" },
      h("header", { class: "host-head" },
        h("h2", {}, host.name),
        h("span", { class: "host-meta" },
          host.last_seen_at ? `last seen ${ago(host.last_seen_at)} ago` : "never seen",
          host.client_version ? ` · client ${host.client_version}` : ""),
      ),
      h("div", { class: "cards" }, list.map(renderCard)),
    );
  }));
}

// --- data flow -------------------------------------------------------------

async function loadBoard() {
  const res = await fetch("/api/board", { credentials: "same-origin" });
  if (res.status === 401) { location.href = "/login"; return; }
  const data = await res.json();
  state.skew = data.now - Date.now() / 1000;
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
connect();
