"use strict";

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== null && v !== undefined && v !== false) e.setAttribute(k, v);
  for (const c of children.flat()) if (c !== null && c !== undefined) e.append(c instanceof Node ? c : String(c));
  return e;
}

async function api(method, url, body) {
  const res = await fetch(url, {
    method, credentials: "same-origin",
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401) { location.href = "/login"; throw new Error("login required"); }
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.detail || res.statusText);
  }
  return res;
}

async function download(res) {
  const name = /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") || "")?.[1] || "bundle.tar.gz";
  const blob = await res.blob();
  const a = el("a", { href: URL.createObjectURL(blob), download: name });
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 10000);
  return name;
}

function ago(ts) {
  if (!ts) return "never";
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts));
  if (s < 60) return `${s}s ago`;
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 172800) return `${Math.floor(s / 3600)}h ago`;
  return `${Math.floor(s / 86400)}d ago`;
}

let publicUrl = "";

async function loadHosts() {
  const data = await (await api("GET", "/api/deploy/hosts")).json();
  publicUrl = data.public_url;
  const urlInput = document.getElementById("f-url");
  if (!urlInput.value) urlInput.value = publicUrl;
  const tbody = document.getElementById("hosts");
  if (!data.hosts.length) {
    tbody.replaceChildren(el("tr", {}, el("td", { colspan: 7, class: "muted" }, "No hosts yet.")));
    return;
  }
  tbody.replaceChildren(...data.hosts.map(h => {
    const revoked = !!h.revoked_at;
    const offline = (h.alerts || []).some(a => a.kind === "offline");
    const status = revoked ? "revoked" : offline ? "offline" : h.last_heartbeat_at ? "online" : "no heartbeat";
    const d = h.deploy || {};
    const target = [d.ansible_user ? `${d.ansible_user}@` : "", d.ansible_host || h.name,
                    d.claude_user ? ` → ${d.claude_user}` : "", ` · ${d.claude_version || "latest"}`].join("");
    const bundleBtn = el("button", { class: "mini" }, revoked ? "Re-enable (new bundle)" : "New bundle");
    bundleBtn.addEventListener("click", async () => {
      if (!confirm(`Issue a new token for ${h.name}? The current token stops working until the new bundle is deployed.`)) return;
      try {
        const name = await download(await api("POST", `/api/deploy/hosts/${h.id}/bundle`,
          { url: document.getElementById("f-url").value }));
        setStatus(`Downloaded ${name}.`, true);
        loadHosts();
      } catch (err) { setStatus(err.message, false); }
    });
    const revokeBtn = revoked ? null : el("button", { class: "mini danger" }, "Revoke");
    revokeBtn?.addEventListener("click", async () => {
      if (!confirm(`Revoke ${h.name}? It can no longer report until it gets a new bundle.`)) return;
      try { await api("POST", `/api/deploy/hosts/${h.id}/revoke`, {}); loadHosts(); }
      catch (err) { setStatus(err.message, false); }
    });
    return el("tr", { class: revoked ? "revoked" : "" },
      el("td", {}, el("strong", {}, h.name)),
      el("td", {}, el("span", { class: `pill pill-${status.replace(" ", "-")}` }, status),
        el("span", { class: "muted small" }, ` ${ago(h.last_heartbeat_at || h.last_seen_at)}`)),
      el("td", {}, h.os_family || "–"),
      el("td", {}, h.claude_installed_version || "–"),
      el("td", {}, h.client_version || "–"),
      el("td", { class: "mono small" }, target),
      el("td", { class: "row-actions" }, bundleBtn, revokeBtn),
    );
  }));
}

function setStatus(msg, ok) {
  const s = document.getElementById("add-status");
  s.textContent = msg;
  s.className = `status ${ok ? "ok" : "error"}`;
}

document.getElementById("add-form").addEventListener("submit", async ev => {
  ev.preventDefault();
  const body = Object.fromEntries(new FormData(ev.target).entries());
  try {
    const name = await download(await api("POST", "/api/deploy/hosts", body));
    setStatus(`Host ${body.name} registered — downloaded ${name}.`, true);
    ev.target.reset();
    document.getElementById("f-url").value = body.url;
    loadHosts();
  } catch (err) { setStatus(`Not created: ${err.message}`, false); }
});

document.getElementById("update-bundle").addEventListener("click", ev => {
  ev.currentTarget.href = `/api/deploy/update-bundle?url=${encodeURIComponent(document.getElementById("f-url").value)}`;
});

loadHosts();
