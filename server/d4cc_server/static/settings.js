"use strict";

async function api(method, url, body) {
  const res = await fetch(url, {
    method, credentials: "same-origin",
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) { location.href = "/login"; throw new Error("login required"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined && v !== false) e.setAttribute(k, v);
  for (const c of children.flat()) if (c !== null && c !== undefined) e.append(c instanceof Node ? c : String(c));
  return e;
}

async function loadSettings() {
  const { values, fields } = await api("GET", "/api/settings");
  const form = document.getElementById("settings-form");
  form.replaceChildren(
    ...fields.map(f => {
      const isVersion = f.key === "min_claude_version";
      return el("div", { class: "field" },
        el("label", { for: `f-${f.key}` }, f.label),
        el("input", isVersion
          ? { id: `f-${f.key}`, name: f.key, type: "text", value: values[f.key], placeholder: "e.g. 2.1.330",
              pattern: "\\d+(\\.\\d+){0,3}" }
          : { id: `f-${f.key}`, name: f.key, type: "number", value: values[f.key], min: f.min, max: f.max,
              step: f.integer ? 1 : "any", required: true }),
        el("span", { class: "unit" }, f.unit),
      );
    }),
    el("div", { class: "form-actions" }, el("button", { type: "submit", class: "primary" }, "Save")),
  );
}

document.getElementById("settings-form").addEventListener("submit", async ev => {
  ev.preventDefault();
  const status = document.getElementById("settings-status");
  const body = {};
  for (const input of ev.target.querySelectorAll("input")) {
    body[input.name] = input.type === "number" ? Number(input.value) : input.value;
  }
  try {
    await api("PUT", "/api/settings", body);
    status.textContent = "Saved.";
    status.className = "status ok";
  } catch (err) {
    status.textContent = `Not saved: ${err.message}`;
    status.className = "status error";
  }
});

function basename(path) {
  if (!path) return "?";
  const parts = path.replace(/\/+$/, "").split("/");
  return parts[parts.length - 1] || "/";
}

async function loadArchived() {
  const { sessions } = await api("GET", "/api/archived");
  const list = document.getElementById("archived");
  if (!sessions.length) { list.replaceChildren(el("li", { class: "muted" }, "No archived sessions.")); return; }
  list.replaceChildren(...sessions.map(s => {
    const btn = el("button", { class: "mini" }, "Restore");
    btn.addEventListener("click", async () => {
      await api("POST", `/api/sessions/${s.id}/unarchive`, {});
      loadArchived();
    });
    const when = new Date(s.archived_at * 1000).toLocaleString();
    return el("li", {}, el("strong", {}, basename(s.cwd)),
      el("span", { class: "muted" }, ` ${s.tmux_target || ""} · ended ${s.end_reason || ""} · archived ${when}`), btn);
  }));
}

loadSettings();
loadArchived();
