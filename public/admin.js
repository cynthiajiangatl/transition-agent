// admin.js — delegated-access administration (grant / revoke).
const esc = (s) =>
  String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

let subjects = []; // [{slug, displayName, upn}]

function flash(msg, isErr) {
  const el = document.getElementById("flash");
  el.textContent = msg;
  el.className = "flash show" + (isErr ? " err" : "");
  setTimeout(() => (el.className = "flash"), 2400);
}

async function initAuth() {
  let me;
  try {
    me = await (await fetch("/api/me")).json();
  } catch {
    return true; // don't hard-block if /api/me is unreachable
  }
  if (me.authEnabled && !me.authenticated) {
    window.location.href = "/login";
    return false;
  }
  if (me.authEnabled && !me.isAdmin) {
    // Not an administrator — the APIs would 403 anyway; send them home.
    window.location.href = "/";
    return false;
  }
  if (me.user) {
    const badge = document.getElementById("userBadge");
    const logout = document.getElementById("logoutLink");
    if (badge) {
      badge.textContent = me.user.name || me.user.upn || "Signed in";
      badge.style.display = "inline-block";
    }
    if (logout) logout.style.display = "inline-block";
  }
  return true;
}

async function loadSubjects() {
  const { subjects: rows } = await (await fetch("/api/admin/subjects")).json();
  subjects = rows || [];
  const opts = subjects
    .map((s) => `<option value="${esc(s.slug)}">${esc(s.displayName || s.upn || s.slug)}</option>`)
    .join("");
  document.getElementById("subject").innerHTML =
    opts || `<option value="" disabled selected>No handovers yet</option>`;
  document.getElementById("filter").innerHTML =
    `<option value="">All employees</option>` + opts;
}

function statusOf(g) {
  if (g.revoked) return { label: "Revoked", cls: "high" };
  if (g.expiresAt && new Date(g.expiresAt) <= new Date()) return { label: "Expired", cls: "medium" };
  return { label: "Active", cls: "low" };
}

function subjectName(slug) {
  const s = subjects.find((x) => x.slug === slug);
  return s ? s.displayName || s.upn || slug : slug;
}

async function loadGrants() {
  const filter = document.getElementById("filter").value;
  const url = filter ? `/api/admin/grants?user=${encodeURIComponent(filter)}` : "/api/admin/grants";
  const { grants } = await (await fetch(url)).json();
  const tbody = document.getElementById("grantRows");
  if (!grants || !grants.length) {
    tbody.innerHTML = `<tr><td colspan="6" class="muted">No grants yet.</td></tr>`;
    return;
  }
  tbody.innerHTML = grants
    .map((g) => {
      const st = statusOf(g);
      const expires = g.expiresAt ? new Date(g.expiresAt).toLocaleDateString() : "—";
      const canRevoke = !g.revoked;
      const revoke = canRevoke
        ? `<button class="btn ghost del" data-id="${esc(g.id)}" data-subject="${esc(g.subjectSlug)}">Revoke</button>`
        : "";
      return `<tr>
        <td>${esc(g.granteeUpn)}</td>
        <td>${esc(subjectName(g.subjectSlug))}</td>
        <td><span class="badge ${st.cls}">${st.label}</span></td>
        <td class="meta">${esc(g.grantedBy || "—")}</td>
        <td class="meta">${esc(expires)}</td>
        <td class="rowtools">${revoke}</td>
      </tr>`;
    })
    .join("");
}

async function createGrant(ev) {
  ev.preventDefault();
  const subject = document.getElementById("subject").value;
  const granteeUpn = document.getElementById("grantee").value.trim();
  const days = Number(document.getElementById("days").value) || undefined;
  const msg = document.getElementById("formMsg");
  if (!subject || !granteeUpn) {
    msg.textContent = "Pick an employee and enter the successor's email.";
    msg.className = "form-msg err";
    return;
  }
  const res = await fetch("/api/admin/grants", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ subject, granteeUpn, days }),
  });
  const out = await res.json();
  if (!res.ok || out.ok === false) {
    msg.textContent = "Could not grant access: " + (out.error || res.statusText);
    msg.className = "form-msg err";
    return;
  }
  msg.textContent = `Granted ${granteeUpn} access to ${subjectName(subject)}.`;
  msg.className = "form-msg ok";
  document.getElementById("grantee").value = "";
  flash("Access granted ✓");
  await loadGrants();
}

async function onRowClick(ev) {
  const btn = ev.target.closest("button.del");
  if (!btn) return;
  const { id, subject } = btn.dataset;
  if (!confirm("Revoke this access grant?")) return;
  const res = await fetch("/api/admin/grants/revoke", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id, subject }),
  });
  const out = await res.json();
  if (!res.ok || out.ok === false) {
    flash("Revoke failed: " + (out.error || res.statusText), true);
    return;
  }
  flash("Grant revoked ✓");
  await loadGrants();
}

window.addEventListener("DOMContentLoaded", async () => {
  if (!(await initAuth())) return;
  document.getElementById("grantForm").addEventListener("submit", createGrant);
  document.getElementById("filter").addEventListener("change", loadGrants);
  document.getElementById("grantRows").addEventListener("click", onRowClick);
  try {
    await loadSubjects();
    await loadGrants();
  } catch (e) {
    flash("Failed to load: " + e.message, true);
  }
});
