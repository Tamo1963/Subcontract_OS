// Subcontract OS - internal workspace (vanilla JS, no build step).
const S = { me: null, project: null, view: "dashboard", sub: null, ob: null };
const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const pill = (t, cls) => `<span class="pill ${esc(cls || t)}">${esc(t)}</span>`;
const LIFECYCLE = ["DRAFT","TECHNICAL_REVIEW","COMMERCIAL_REVIEW","LEGAL_REVIEW","APPROVAL_PENDING","APPROVED_FOR_EXECUTION","EXECUTED","ACTIVE"];
const DISPOSITIONS = ["DIRECT","ADAPTED","PROJECT_SPECIFIC","COMMERCIAL_DEVIATION","NOT_APPLICABLE","UNRESOLVED"];
const statusCls = (s) => ({CLOSED:"ok",APPROVED:"ok",ACCEPTED:"ok",PUBLISHED:"ok",ACTIVE:"ok",EXECUTED:"ok",
  OPEN:"info",EVIDENCE_SUBMITTED:"warn",REVIEW_REQUIRED:"warn",PROPOSED:"warn",UNRESOLVED:"bad",CONFLICT:"bad",
  REJECTED:"bad",SUPERSEDED:"",APPROVAL_PENDING:"warn"}[s] || "");

async function api(path, opts = {}) {
  const headers = { "X-SCOS-CSRF": "1", ...(opts.headers || {}) };   // anti-forgery header on every call
  if (opts.json) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  const r = await fetch(path, { ...opts, headers, credentials: "same-origin" });
  const body = r.headers.get("content-type")?.includes("json") ? await r.json() : await r.text();
  if (!r.ok) {
    const e = body?.error || { message: body };
    if (r.status === 401 && !opts.quiet) { showLogin("Your session has ended. Please sign in again."); throw e; }
    if (e.code === "PASSWORD_CHANGE_REQUIRED") { showChangePassword(true); throw e; }
    if (!opts.quiet) toast(`${e.code || r.status}: ${e.message}`, true);
    throw e;
  }
  return body;
}
function toast(msg, err) { const d = document.createElement("div"); d.className = "t" + (err ? " err" : "");
  d.textContent = msg; $("#toast").append(d); setTimeout(() => d.remove(), err ? 7000 : 3500); }
function openModal(html) { $("#modal-body").innerHTML = html; $("#modal").classList.remove("hidden"); }
function closeModal() { $("#modal").classList.add("hidden"); }

async function boot() {
  $("#project").onchange = (e) => { S.project = e.target.value; S.sub = null; render(); };
  document.querySelectorAll("#nav a").forEach(a => a.onclick = () => go(a.dataset.view));
  $("#logout-btn").onclick = async () => { await api("/api/v1/auth/logout", { method: "POST", quiet: true }).catch(() => {}); showLogin("Signed out."); };
  $("#pw-btn").onclick = () => showMyAccount();
  try { S.me = await api("/api/v1/me", { quiet: true }); } catch (e) { return showLogin(); }
  if (S.me.must_change_password) return showChangePassword(true);
  $("#login").classList.add("hidden");
  if (location.hash === "#linked") { history.replaceState(null, "", "/"); toast("Company login linked to your account"); }
  const err = hashMessage(); if (err) toast(err, true);
  $("#nav-users").classList.toggle("hidden", !S.me.roles.includes("system_admin"));
  await loadProjects();
}
const SIGNIN_ERRORS = {
  NO_ACCOUNT: "No Subcontract OS account is set up for that login. Ask an administrator to invite you.",
  ACCOUNT_DISABLED: "This account is disabled.", ALREADY_LINKED: "That company login is already linked to another account.",
  BROWSER_MISMATCH: "Sign-in was started in another browser or tab. Please try again.",
  INVALID_STATE: "That sign-in link has expired or was already used. Please try again.", STATE_EXPIRED: "Sign-in took too long. Please try again.",
  INVALID_TOKEN: "The sign-in provider's response failed security checks.", UNKNOWN_SIGNING_KEY: "The sign-in provider's response failed security checks.",
  TOKEN_EXCHANGE_FAILED: "The sign-in provider refused the request (check client ID/secret).", PROVIDER_DISABLED: "That sign-in option is turned off.",
  PROVIDER_ACCESS_DENIED: "Sign-in was cancelled at the provider.", DISCOVERY_FAILED: "The sign-in provider could not be reached." };
function hashMessage() {
  const m = location.hash.match(/signin-error=([A-Z_]+)/);
  if (m) { history.replaceState(null, "", "/"); return SIGNIN_ERRORS[m[1]] || `Sign-in failed (${m[1]}).`; }
  return "";
}
async function showLogin(msg = "") {
  $("#login").classList.remove("hidden");
  msg = hashMessage() || msg;
  let opts = { providers: [], password_login: "all" };
  try { opts = await api("/api/v1/auth/providers", { quiet: true }); } catch (e) {}
  const pwHidden = opts.password_login === "admins_only";
  $("#login-body").innerHTML = `<h2>Sign in</h2>
    ${opts.providers.map(p => `<a class="sso-btn" href="/api/v1/auth/oidc/${encodeURIComponent(p.id)}/start">${esc(p.label)}</a>`).join("")}
    ${opts.providers.length ? `<div class="or"><span>${pwHidden ? "administrators" : "or with username"}</span></div>` : ""}
    <div id="pw-form" class="${pwHidden ? "hidden" : ""}">
    <label>Username</label><input id="l-user" autocomplete="username">
    <label>Password</label><input id="l-pw" type="password" autocomplete="current-password">
    <button class="primary" id="l-go">Sign in</button></div>
    ${pwHidden ? `<a href="#" id="pw-show" class="small">Administrator sign-in with password</a>` : ""}
    <div class="err" id="l-err">${esc(msg)}</div>
    <p class="hint">First sign-in with a password uses the temporary password issued by your administrator; you will be asked to set your own.</p>`;
  const go = async () => {
    try { await api("/api/v1/auth/login", { method: "POST", quiet: true, json: { username: $("#l-user").value, password: $("#l-pw").value } }); boot(); }
    catch (e) { $("#l-err").textContent = e.message || "Sign-in failed"; }
  };
  $("#l-go").onclick = go; $("#l-pw").onkeydown = (e) => e.key === "Enter" && go();
  $("#pw-show") && ($("#pw-show").onclick = (e) => { e.preventDefault(); $("#pw-form").classList.remove("hidden"); e.target.remove(); });
  if (!pwHidden) $("#l-user").focus();
}
async function showMyAccount() {
  const [mine, opts] = await Promise.all([api("/api/v1/me/identities"), api("/api/v1/auth/providers")]);
  const linked = new Set(mine.map(x => x.provider_id));
  openModal(`<h2>My sign-in</h2><p class="small muted">Signed in as <b>${esc(S.me.username)}</b>. Link a company login to sign in with it next time.</p>
    <h3>Linked company logins</h3>${mine.length ? `<table>${mine.map(x => `<tr><td><b>${esc(x.provider)}</b><div class="small muted">${esc(x.email || "")}</div></td>
      <td class="small mono">${esc((x.last_used_at || x.linked_at).replace("T", " ").slice(0, 16))}</td><td><button class="danger" data-unlink="${x.id}">Unlink</button></td></tr>`).join("")}</table>`
      : `<p class="muted small">None yet.</p>`}
    ${opts.providers.filter(p => !linked.has(p.id)).map(p => `<button data-link="${p.id}">Link: ${esc(p.label)}</button> `).join("")}
    <h3>Password</h3><button id="my-pw">Change password</button>`);
  document.querySelectorAll("[data-link]").forEach(b => b.onclick = async () => {
    try { const r = await api(`/api/v1/auth/oidc/${b.dataset.link}/link`, { method: "POST" }); location.href = r.authorization_url; } catch (e) {} });
  document.querySelectorAll("[data-unlink]").forEach(b => b.onclick = async () => {
    try { await api(`/api/v1/me/identities/${b.dataset.unlink}`, { method: "DELETE" }); toast("Unlinked"); showMyAccount(); } catch (e) {} });
  $("#my-pw").onclick = () => { closeModal(); showChangePassword(false); };
}
function showChangePassword(forced) {
  $("#login").classList.remove("hidden");
  $("#login-body").innerHTML = `<h2>${forced ? "Set your password" : "Change password"}</h2>
    ${forced ? `<p class="hint" style="margin-top:0">You signed in with a temporary password. Choose your own to continue.</p>` : ""}
    <label>Current (temporary) password</label><input id="c-old" type="password" autocomplete="current-password">
    <label>New password</label><input id="c-new" type="password" autocomplete="new-password">
    <label>Repeat new password</label><input id="c-new2" type="password" autocomplete="new-password">
    <p class="hint">At least 12 characters with upper- and lower-case letters and a digit; must not contain your username.</p>
    <div class="err" id="c-err"></div><button class="primary" id="c-go">Save password</button>
    ${forced ? "" : `<button id="c-cancel">Cancel</button>`}`;
  $("#c-go").onclick = async () => {
    if ($("#c-new").value !== $("#c-new2").value) return ($("#c-err").textContent = "The new passwords do not match.");
    try { await api("/api/v1/auth/change-password", { method: "POST", quiet: true, json: { current_password: $("#c-old").value, new_password: $("#c-new").value } });
          toast("Password changed"); boot(); }
    catch (e) { $("#c-err").textContent = e.message || "Could not change password"; }
  };
  $("#c-cancel") && ($("#c-cancel").onclick = () => $("#login").classList.add("hidden"));
}
async function loadProjects() {
  const me = S.me = await api("/api/v1/me");
  $("#whoami").innerHTML = `<b>${esc(me.username)}</b> · roles: ${esc([...new Set([...me.roles, ...Object.values(me.projects).flat()])].join(", ") || "none")} · clearance ${esc(me.clearance)}`;
  const ps = await api("/api/v1/projects");
  $("#project").innerHTML = ps.length ? ps.map(p => `<option value="${p.id}">${esc(p.project_code)} — ${esc(p.name)}</option>`).join("")
                                      : `<option value="">(no project access)</option>`;
  S.project = ps.find(p => p.id === S.project) ? S.project : ps[0]?.id || null;
  if (S.project) $("#project").value = S.project;
  render();
}
function go(view, extra = {}) { Object.assign(S, extra); S.view = view;
  document.querySelectorAll("#nav a").forEach(a => a.classList.toggle("on", a.dataset.view === view)); render(); }
async function render() {
  const v = $("#view");
  if (!S.project && !["ai", "users"].includes(S.view)) { v.innerHTML = `<h1>No project access</h1><p class="sub">This user is not a member of any project. Access is enforced by the API, not the UI.</p>`; return; }
  v.innerHTML = `<p class="muted">Loading…</p>`;
  try { v.innerHTML = await VIEWS[S.view](); bindView(); }
  catch (e) { v.innerHTML = `<div class="card"><h2>Not available</h2><p class="muted">${esc(e.code || "")} ${esc(e.message || e)}</p></div>`; }
}
let binders = [];
const on = (fn) => { binders.push(fn); return ""; };
function bindView() { const b = binders; binders = []; b.forEach(f => f()); }

// ---------------------------------------------------------------- views
const VIEWS = {};
VIEWS.dashboard = async () => {
  const [d, p, members] = await Promise.all([api(`/api/v1/projects/${S.project}/dashboard`), api(`/api/v1/projects/${S.project}`),
                                             api(`/api/v1/projects/${S.project}/members`)]);
  const isCoord = (S.me?.roles || []).includes("project_coordinator") || (S.me?.projects?.[S.project] || []).includes("project_coordinator");
  const k = (v, l, cls = "") => `<div class="kpi ${cls}"><div class="v">${v}</div><div class="l">${l}</div></div>`;
  const obRow = (o) => `<tr class="click" data-ob="${o.id}" data-sub="${o.subcontract_id}"><td class="mono">${esc(o.number)}</td><td>${esc(o.statement)}</td><td class="mono">${esc(o.due_at)}</td><td>${pill(o.risk_level)}</td><td>${esc(o.owner_role)}</td></tr>`;
  return `<h1>Portfolio control</h1><p class="sub">${esc(p.project_code)} · ${esc(p.name)} · ${esc(p.location)} · sector ${esc(p.sector_code)}</p>
  <div class="grid g5">
    ${k(d.subcontracts.active + " / " + d.subcontracts.total, "Executed / total subcontracts")}
    ${k(d.flowdown.unresolved_high, "Unresolved HIGH-risk flow-downs", d.flowdown.unresolved_high ? "bad" : "")}
    ${k(d.obligations.overdue, "Overdue obligations", d.obligations.overdue ? "bad" : "")}
    ${k(d.obligations.awaiting_evidence_review, "Evidence awaiting review", d.obligations.awaiting_evidence_review ? "warn" : "")}
    ${k(d.documents.pending_assertions + d.documents.conflicts, "Extractions / conflicts to review", d.documents.pending_assertions + d.documents.conflicts ? "warn" : "")}
  </div><br>
  <div class="grid g2">
    <div class="card"><h2>Overdue obligations</h2>${d.overdue_obligations.length ? `<table><tr><th>No.</th><th>Obligation</th><th>Due</th><th>Risk</th><th>Owner</th></tr>${d.overdue_obligations.map(obRow).join("")}</table>` : `<p class="muted">None.</p>`}</div>
    <div class="card"><h2>Due in the next 14 days</h2>${d.upcoming_obligations.length ? `<table><tr><th>No.</th><th>Obligation</th><th>Due</th><th>Risk</th><th>Owner</th></tr>${d.upcoming_obligations.map(obRow).join("")}</table>` : `<p class="muted">None.</p>`}</div>
  </div>
  <div class="card"><h2>Subcontracts</h2><table><tr><th>Number</th><th>Scope</th><th>Discipline</th><th>Value</th><th>Status</th></tr>
  ${p.subcontracts.map(s => `<tr class="click" data-sub-open="${s.id}"><td class="mono">${esc(s.subcontract_number)}</td><td>${esc(s.scope_summary)}</td><td>${esc(s.discipline)}</td><td class="mono">${esc(s.currency)} ${Number(s.contract_value).toLocaleString("en-IN")}</td><td>${pill(s.status, statusCls(s.status))}</td></tr>`).join("")}</table></div>
  <div class="card"><div class="row"><h2>Project team</h2><span class="spacer"></span><span class="small muted">Project roles are granted here by the project coordinator.</span></div>
    <table><tr><th>User</th><th>Project roles</th><th></th></tr>${members.map(m => `<tr><td><b>${esc(m.username)}</b> <span class="small muted">${esc(m.full_name)}</span></td><td class="small">${esc(m.roles.join(", "))}</td>
      <td>${isCoord ? `<button class="danger" data-rm="${esc(m.username)}">Remove</button>` : ""}</td></tr>`).join("")}</table>
    ${isCoord ? `<div class="row" style="margin-top:10px"><input id="m-user" placeholder="username"><select id="m-role">${["contracts_manager","legal_reviewer","contracts_approver","commercial_manager","qaqc_manager","hse_manager","construction_manager","project_controls","procurement_manager","auditor","registry_manager","project_coordinator"].map(r => `<option>${r}</option>`).join("")}</select><button class="primary" id="m-add">Add to project</button></div>` : ""}</div>
  <div class="card"><h2>Flow-down status</h2><p>${d.flowdown.total} source provisions mapped across subcontracts · ${d.flowdown.unresolved} without an approved disposition (${d.flowdown.unresolved_high} HIGH risk).</p>
  <p class="small muted">Obligations: ${d.obligations.total} total · ${d.obligations.open} open · ${d.obligations.closed} closed with accepted evidence. Every figure drills down to source records.</p></div>
  ${on(() => {
    $("#m-add") && ($("#m-add").onclick = async () => { try { await api(`/api/v1/projects/${S.project}/members`, { method: "POST", json: { username: $("#m-user").value.trim(), roles: [$("#m-role").value] } }); toast("Member added"); render(); } catch (e) {} });
    document.querySelectorAll("[data-rm]").forEach(b => b.onclick = async () => { if (!confirm(`Remove ${b.dataset.rm} from this project?`)) return; try { await api(`/api/v1/projects/${S.project}/members/${b.dataset.rm}`, { method: "DELETE" }); render(); } catch (e) {} });
    document.querySelectorAll("[data-ob]").forEach(r => r.onclick = () => go("obligations", { sub: r.dataset.sub, ob: r.dataset.ob }));
               document.querySelectorAll("[data-sub-open]").forEach(r => r.onclick = () => go("subcontracts", { sub: r.dataset.subOpen })); })}`;
};

VIEWS.registry = async () => {
  const list = await api("/api/v1/subcontractors");
  return `<h1>Enterprise registry</h1><p class="sub">One permanent identity per legal entity. Project eligibility is held separately per project.</p>
  <div class="card"><div class="row"><h2>Subcontractors</h2><span class="spacer"></span><button class="primary" id="new-sc">Register legal entity</button></div>
  <table><tr><th>Legal name</th><th>Registration</th><th>Country</th><th>Disciplines</th><th>Status</th></tr>
  ${list.map(s => `<tr class="click" data-sc="${s.id}"><td><b>${esc(s.legal_name)}</b></td><td class="mono">${esc(s.registration_number)}</td><td>${esc(s.country_code)}</td><td>${esc(s.disciplines.join(", "))}</td><td>${pill(s.entity_status, "ok")}</td></tr>`).join("")}</table></div>
  ${on(() => {
    $("#new-sc").onclick = () => { openModal(`<h2>Register legal entity</h2><div class="form">
      <label>Legal name</label><input id="f-name"><label>Registration no.</label><input id="f-reg"><label>Country (ISO-2)</label><input id="f-cc" value="IN">
      <label>Disciplines</label><input id="f-disc" placeholder="PIPING, CIVIL"></div><br><button class="primary" id="f-go">Register</button>`);
      $("#f-go").onclick = async () => { try { await api("/api/v1/subcontractors", { method: "POST", json: { legal_name: $("#f-name").value,
        registration_number: $("#f-reg").value, country_code: $("#f-cc").value, disciplines: $("#f-disc").value.split(",").map(x => x.trim()).filter(Boolean) } });
        closeModal(); toast("Registered"); render(); } catch (e) {} }; };
    document.querySelectorAll("[data-sc]").forEach(r => r.onclick = async () => { const s = await api(`/api/v1/subcontractors/${r.dataset.sc}`);
      openModal(`<h2>${esc(s.legal_name)}</h2><p class="mono">${esc(s.country_code)} · ${esc(s.registration_number)}</p><h3>Project relationships visible to you</h3>
      ${s.project_relationships.length ? `<table><tr><th>Project</th><th>Package</th><th>Eligibility</th></tr>${s.project_relationships.map(l => `<tr><td class="mono">${esc(l.project_id.slice(0,8))}</td><td>${esc(l.package)}</td><td>${pill(l.eligibility_status,"ok")}</td></tr>`).join("")}</table>` : `<p class="muted">None in your projects.</p>`}
      <p class="small muted">Relationships in projects you cannot access are not disclosed.</p>`); });
  })}`;
};

VIEWS.subcontracts = async () => {
  const p = await api(`/api/v1/projects/${S.project}`);
  if (!S.sub || !p.subcontracts.find(s => s.id === S.sub)) S.sub = p.subcontracts[0]?.id;
  const tabs = `<div class="tabs">${p.subcontracts.map(s => `<a data-pick="${s.id}" class="${s.id === S.sub ? "on" : ""}">${esc(s.subcontract_number)}</a>`).join("")}<a id="new-sub">+ New draft</a></div>`;
  const newSub = on(() => { $("#new-sub").onclick = async () => {
    const subs = await api("/api/v1/subcontractors");
    openModal(`<h2>New subcontract draft</h2><div class="form">
      <label>Main contract</label><select id="n-mc">${p.main_contracts.map(m => `<option value="${m.id}">${esc(m.contract_reference)}</option>`).join("")}</select>
      <label>Subcontractor</label><select id="n-sc">${p.eligible_subcontractors.map(e => `<option value="${e.subcontractor_id}">${esc(subs.find(s => s.id === e.subcontractor_id)?.legal_name)} — ${esc(e.package)}</option>`).join("")}</select>
      <label>Number</label><input id="n-no" value="SC-101-0${p.subcontracts.length + 1}"><label>Scope</label><input id="n-scope">
      <label>Discipline</label><select id="n-disc"><option>PIPING</option><option>CIVIL</option><option>ELECTRICAL</option><option>MECHANICAL</option><option>INSTRUMENTATION</option></select>
      <label>Value (INR)</label><input id="n-val" value="10000000.00"></div><br><button class="primary" id="n-go">Create draft</button>`);
    $("#n-go").onclick = async () => { try { const s = await api("/api/v1/subcontracts", { method: "POST", headers: { "Idempotency-Key": crypto.randomUUID() },
      json: { project_id: S.project, main_contract_id: $("#n-mc").value, subcontractor_id: $("#n-sc").value, subcontract_number: $("#n-no").value,
              scope_summary: $("#n-scope").value, discipline: $("#n-disc").value, contract_value: $("#n-val").value } });
      closeModal(); S.sub = s.id; toast("Draft created"); render(); } catch (e) {} }; };
    document.querySelectorAll("[data-pick]").forEach(a => a.onclick = () => { S.sub = a.dataset.pick; render(); }); });
  if (!S.sub) return `<h1>Subcontracts</h1>${tabs}<p class="muted">No subcontracts yet.</p>${newSub}`;
  const [s, rows, ams] = await Promise.all([api(`/api/v1/subcontracts/${S.sub}`), api(`/api/v1/subcontracts/${S.sub}/flowdown-mappings`),
                                           api(`/api/v1/subcontracts/${S.sub}/amendments`)]);
  const idx = LIFECYCLE.indexOf(s.status), g = s.release_gate, locked = ["EXECUTED","ACTIVE","CLOSED_OUT"].includes(s.status);
  return `<h1>${esc(s.subcontract_number)} · ${esc(s.subcontractor)}</h1><p class="sub">${esc(s.scope_summary)} · ${esc(s.discipline)} · ${esc(s.currency)} ${Number(s.contract_value).toLocaleString("en-IN")}</p>
  ${tabs}
  <div class="card"><h2>Lifecycle</h2><div class="stepper">${LIFECYCLE.map((st, i) => `<span class="step ${i < idx ? "done" : i === idx ? "cur" : ""}">${st.replaceAll("_", " ")}</span>`).join("")}</div>
    <div class="row" style="margin-top:10px">${s.allowed_transitions.map(t => `<button class="${t === "DRAFT" || t === "WITHDRAWN" ? "danger" : "primary"}" data-tr="${t}">→ ${t.replaceAll("_", " ")}</button>`).join("") || `<span class="muted">No further transitions.</span>`}</div></div>
  <div class="grid g2">
    <div class="card"><h2>Release gate</h2><div class="gate ${g.passed ? "" : "blocked"}"><b>${g.passed ? "PASSED" : "BLOCKED"}</b> — ${g.mapped} provisions mapped · ${g.blocking_mapping_ids.length} blocking HIGH-risk · ${g.warning_mapping_ids.length} medium/low unreviewed · ${g.clauses_without_mapping} unmapped</div>
      ${g.exception ? `<p class="small">Risk exception accepted by ${esc(g.exception.accepted_by.slice(0,8))}: “${esc(g.exception.reason)}”</p>` : ""}
      ${!g.passed && g.blocking_mapping_ids.length && !locked ? `<p class="small muted">Release is blocked until each HIGH-risk row has an approved disposition, or a contracts approver records a risk exception.</p><button id="exc">Record risk exception…</button>` : ""}</div>
    <div class="card"><h2>Executed baseline</h2>${s.baseline ? `<p class="small">Frozen ${esc(s.baseline.executed_at)} · value ${esc(s.baseline.contract_value)} · ${s.baseline.incorporated_documents.length} incorporated document(s)</p>
      <table>${s.baseline.incorporated_documents.map(d => `<tr><td class="mono">${esc(d.document_number)} rev ${esc(d.revision)}</td><td class="mono small">sha256 ${esc(d.sha256.slice(0,16))}…</td></tr>`).join("")}</table>
      <h3>Amendments</h3>${ams.length ? `<table>${ams.map(a => `<tr><td>#${a.number}</td><td>${esc(a.field)}: <span class="mono">${esc(a.old_value)} → ${esc(a.new_value)}</span><br><span class="small muted">${esc(a.reason)}</span></td><td>${pill(a.status, statusCls(a.status))}</td></tr>`).join("")}</table>` : `<p class="muted small">None.</p>`}`
      : `<p class="muted">Not executed. The baseline is frozen on execution; afterwards changes go only through amendments.</p>`}</div>
  </div>
  <div class="card"><div class="row"><h2>Flow-down matrix</h2><span class="spacer"></span>${!locked ? `<button id="gen">Generate / refresh from governing main-contract revisions</button>` : ""}</div>
    <p class="small muted">Proposals come from the <span class="mono">${esc(rows[0]?.proposal_method || "rules-v1")}</span> proposer and are never decisions. A reviewer records each disposition with a rationale.</p>
    <table><tr><th>Source</th><th>Provision</th><th>Risk</th><th>Proposal</th><th>Decision</th><th></th></tr>
    ${rows.map(m => `<tr><td class="mono small">${esc(m.source_document)}<br>rev ${esc(m.source_revision)} · cl. ${esc(m.clause_reference)}</td>
      <td><b>${esc(m.heading)}</b> <span class="muted small">${esc(m.clause_type)}</span><div class="small muted">${esc(m.source_text.slice(0, 180))}${m.source_text.length > 180 ? "…" : ""}</div></td>
      <td>${pill(m.risk_level)}</td><td>${pill(m.proposed_disposition, "info")}<div class="small muted">${esc(m.proposed_rationale)}</div></td>
      <td>${m.disposition ? pill(m.disposition, m.disposition === "UNRESOLVED" ? "bad" : "ok") + `<div class="small">${esc(m.rationale)}</div>${m.subcontract_treatment ? `<div class="small muted">Treatment: ${esc(m.subcontract_treatment)}</div>` : ""}` : pill("PENDING", "warn")}</td>
      <td>${!locked ? `<button data-dec="${m.id}">Decide</button>` : ""}</td></tr>`).join("")}</table></div>
  ${newSub}
  ${on(() => {
    document.querySelectorAll("[data-tr]").forEach(b => b.onclick = async () => { try { await api(`/api/v1/subcontracts/${S.sub}/transitions`, { method: "POST", json: { target: b.dataset.tr } }); toast(`Moved to ${b.dataset.tr}`); render(); } catch (e) {} });
    $("#gen") && ($("#gen").onclick = async () => { try { await api(`/api/v1/subcontracts/${S.sub}/flowdown-mappings/generate`, { method: "POST" }); toast("Matrix generated"); render(); } catch (e) {} });
    $("#exc") && ($("#exc").onclick = () => { openModal(`<h2>Accept release risk</h2><p class="small">Only a contracts approver who did not draft this subcontract can accept the remaining HIGH-risk items. The reason is recorded in the audit trail.</p><textarea id="exr" placeholder="Reason for accepting the unresolved risk"></textarea><br><br><button class="primary" id="exgo">Record exception</button>`);
      $("#exgo").onclick = async () => { try { await api(`/api/v1/subcontracts/${S.sub}/release-exception`, { method: "POST", json: { reason: $("#exr").value } }); closeModal(); toast("Exception recorded"); render(); } catch (e) {} }; });
    document.querySelectorAll("[data-dec]").forEach(b => b.onclick = () => { const m = rows.find(r => r.id === b.dataset.dec);
      openModal(`<h2>cl. ${esc(m.clause_reference)} ${esc(m.heading)}</h2><p class="small muted">${esc(m.source_document)} rev ${esc(m.source_revision)}</p><div class="quote">${esc(m.source_text)}</div>
        <div class="form"><label>Disposition</label><select id="d-disp">${DISPOSITIONS.map(d => `<option ${d === (m.disposition || m.proposed_disposition) ? "selected" : ""}>${d}</option>`).join("")}</select>
        <label>Rationale</label><textarea id="d-why">${esc(m.rationale || m.proposed_rationale)}</textarea>
        <label>Subcontract treatment</label><textarea id="d-tr" placeholder="Proposed subcontract clause wording">${esc(m.subcontract_treatment || "")}</textarea></div><br><button class="primary" id="d-go">Record decision</button>`);
      $("#d-go").onclick = async () => { try { await api(`/api/v1/flowdown-mappings/${m.id}/decision`, { method: "POST", json: { disposition: $("#d-disp").value, rationale: $("#d-why").value, subcontract_treatment: $("#d-tr").value || null } }); closeModal(); toast("Decision recorded"); render(); } catch (e) {} }; });
  })}`;
};

VIEWS.documents = async () => {
  const [docs, p] = await Promise.all([api(`/api/v1/projects/${S.project}/documents`), api(`/api/v1/projects/${S.project}`)]);
  return `<h1>Documents</h1><p class="sub">Originals are hash-verified. The latest upload is never assumed to govern; a reviewer approves the governing revision.</p>
  <div class="card"><div class="row"><h2>Register a document revision</h2></div>
    <div class="form"><label>File</label><input type="file" id="u-file">
    <label>Document number</label><input id="u-no" placeholder="e.g. EPC-101-GCC"><label>Revision</label><input id="u-rev" value="0">
    <label>Type</label><select id="u-type">${["GCC","SPECIAL_CONDITIONS","SPECIFICATION","BOQ","SUBCONTRACT","CORRESPONDENCE","TEST_REPORT","HSE_PLAN","EVIDENCE"].map(t => `<option>${t}</option>`).join("")}</select>
    <label>Links to</label><select id="u-link"><option value="">— project only —</option>${p.main_contracts.map(m => `<option value="mc:${m.id}">Main contract ${esc(m.contract_reference)}</option>`).join("")}${p.subcontracts.map(s => `<option value="sc:${s.id}">Subcontract ${esc(s.subcontract_number)}</option>`).join("")}</select>
    <label>Classification</label><select id="u-cls"><option>INTERNAL</option><option selected>CONFIDENTIAL</option><option>RESTRICTED</option></select></div>
    <br><button class="primary" id="u-go">Upload &amp; process</button> <span id="u-out" class="small"></span></div>
  <div class="card"><h2>Register</h2><table><tr><th>Number</th><th>Title</th><th>Type</th><th>Revisions (state · hash)</th><th></th></tr>
  ${docs.map(d => `<tr><td class="mono">${esc(d.document_number)}</td><td>${esc(d.title)}<div class="small muted">${esc(d.security_classification)} · ${esc(d.source_party)}</div></td><td>${esc(d.document_type)}</td>
    <td>${d.revisions.map(r => `<div class="row small" style="gap:6px;margin-bottom:4px"><b>rev ${esc(r.revision)}</b>${r.governing ? pill("GOVERNING", "ok") : ""}${pill(r.ingestion_state, statusCls(r.ingestion_state))}<span class="mono muted">${esc(r.sha256.slice(0,10))}…</span>
      <a href="#" data-pass="${r.id}">passages</a>${!r.governing && !["CONFLICT","SUPERSEDED"].includes(r.ingestion_state) ? `<a href="#" data-appr="${r.id}">approve as governing</a>` : ""}</div>
      ${r.conflict_note ? `<div class="small" style="color:var(--high)">${esc(r.conflict_note)}</div>` : ""}`).join("")}</td><td></td></tr>`).join("")}</table></div>
  ${on(() => {
    $("#u-go").onclick = async () => { const f = $("#u-file").files[0]; if (!f) return toast("Choose a file", true);
      const fd = new FormData(); fd.append("file", f); fd.append("project_id", S.project); fd.append("document_number", $("#u-no").value);
      fd.append("revision", $("#u-rev").value); fd.append("document_type", $("#u-type").value); fd.append("security_classification", $("#u-cls").value);
      const l = $("#u-link").value; if (l.startsWith("mc:")) fd.append("main_contract_id", l.slice(3)); if (l.startsWith("sc:")) fd.append("subcontract_id", l.slice(3));
      try { const r = await api("/api/v1/documents", { method: "POST", body: fd }); toast(`${r.outcome}: ${r.message}`); render(); } catch (e) {} };
    document.querySelectorAll("[data-appr]").forEach(a => a.onclick = async (ev) => { ev.preventDefault(); try { await api(`/api/v1/revisions/${a.dataset.appr}/approve`, { method: "POST", json: {} }); toast("Governing revision set"); render(); } catch (e) {} });
    document.querySelectorAll("[data-pass]").forEach(a => a.onclick = async (ev) => { ev.preventDefault(); const ps = await api(`/api/v1/revisions/${a.dataset.pass}/passages`);
      openModal(`<h2>Extracted passages</h2><p class="small muted">${ps.length} passages with page / clause / cell locators.</p>${ps.map(x => `<div style="margin-bottom:10px"><div class="small mono muted">#${x.seq} ${x.page ? "p." + x.page : ""} ${x.section ? "cl. " + esc(x.section) : ""} ${x.cell ? esc(x.cell) : ""} ${esc(x.type)} ${x.flags.map(f => pill(f, "bad")).join("")}</div><div class="quote">${esc(x.text)}</div>${x.assertions.map(s => `<span class="pill info">${esc(s.field)} = ${esc(s.value)} (${s.confidence})</span> `).join("")}</div>`).join("")}`); });
  })}`;
};

VIEWS.review = async () => {
  const q = await api(`/api/v1/projects/${S.project}/review-queue`);
  return `<h1>Review queue</h1><p class="sub">Machine extractions stay proposals until a reviewer accepts, edits or rejects them.</p>
  ${q.revision_conflicts.length ? `<div class="card"><h2>Revision conflicts</h2>${q.revision_conflicts.map(c => `<p>${pill("CONFLICT","bad")} <b class="mono">${esc(c.document_number)}</b> — ${esc(c.note)}</p>`).join("")}</div>` : ""}
  ${q.flagged_passages.length ? `<div class="card"><h2>Untrusted content flagged</h2>${q.flagged_passages.map(f => `<p>${f.flags.map(x => pill(x, "bad")).join("")} <b class="mono">${esc(f.document_number)}</b></p><div class="quote">${esc(f.text)}</div>`).join("")}<p class="small muted">Embedded instructions are stored as data. They never change permissions or trigger actions.</p></div>` : ""}
  <div class="card"><h2>Extracted values (${q.assertions.length})</h2><table><tr><th>Source</th><th>Field</th><th>Proposed</th><th>Evidence</th><th>Conf.</th><th></th></tr>
  ${q.assertions.map(a => `<tr><td class="mono small">${esc(a.document_number)} rev ${esc(a.revision)}${a.section ? " · cl. " + esc(a.section) : ""}</td><td>${esc(a.field)}</td><td class="mono">${esc(a.proposed_value)}</td><td class="small">${esc(a.raw_value)}</td><td class="mono">${a.confidence}</td>
    <td class="row" style="flex-wrap:nowrap"><button data-rv="${a.id}" data-d="ACCEPT">Accept</button><button data-rv="${a.id}" data-d="EDIT">Edit</button><button class="danger" data-rv="${a.id}" data-d="REJECT">Reject</button></td></tr>`).join("")}</table></div>
  ${on(() => document.querySelectorAll("[data-rv]").forEach(b => b.onclick = async () => { let value = null;
    if (b.dataset.d === "EDIT") { value = prompt("Corrected normalised value"); if (!value) return; }
    try { await api(`/api/v1/assertions/${b.dataset.rv}/review`, { method: "POST", json: { decision: b.dataset.d, value } }); toast(`${b.dataset.d} recorded`); render(); } catch (e) {} }))}`;
};

VIEWS.obligations = async () => {
  const p = await api(`/api/v1/projects/${S.project}`);
  if (!S.sub || !p.subcontracts.find(s => s.id === S.sub)) S.sub = p.subcontracts.find(s => s.status === "ACTIVE")?.id || p.subcontracts[0]?.id;
  const tabs = `<div class="tabs">${p.subcontracts.map(s => `<a data-pick="${s.id}" class="${s.id === S.sub ? "on" : ""}">${esc(s.subcontract_number)}</a>`).join("")}</div>`;
  const obs = S.sub ? await api(`/api/v1/subcontracts/${S.sub}/obligations`) : [];
  let trace = "";
  if (S.ob && obs.find(o => o.id === S.ob)) trace = await traceView(S.ob, p);
  return `<h1>Obligations &amp; evidence</h1><p class="sub">Each obligation traces to its source clause and revision. Contractual duties and internal controls are kept apart.</p>${tabs}
  <div class="grid" style="grid-template-columns:minmax(0,1.15fr) minmax(0,1fr)">
  <div class="card"><h2>Obligation register</h2>${obs.length ? `<table><tr><th>No.</th><th>Obligation</th><th>Period</th><th>Due</th><th>Status</th></tr>
    ${obs.map(o => `<tr class="click" data-ob="${o.id}" style="${o.id === S.ob ? "background:#f2f9f9" : ""}"><td class="mono">${esc(o.number)}</td><td>${esc(o.statement)}<div class="small muted">${esc(o.origin_kind === "INTERNAL_CONTROL" ? "Internal control: " + o.internal_control_origin : o.trigger)}</div></td>
      <td class="mono small">${o.notice_period_days ? o.notice_period_days + " " + o.day_basis.toLowerCase() + " d" : "—"}</td><td class="mono small">${esc(o.due_at || "event-driven")}</td>
      <td>${pill(o.status, statusCls(o.status))}${o.overdue ? " " + pill("OVERDUE", "bad") : ""}${pill(o.risk_level)}</td></tr>`).join("")}</table>` : `<p class="muted">Obligations are generated when the subcontract is executed.</p>`}</div>
  <div>${trace || `<div class="card"><p class="muted">Select an obligation to see its chain of evidence.</p></div>`}</div></div>
  ${on(() => { document.querySelectorAll("[data-pick]").forEach(a => a.onclick = () => { S.sub = a.dataset.pick; S.ob = null; render(); });
    document.querySelectorAll("[data-ob]").forEach(r => r.onclick = () => { S.ob = r.dataset.ob; render(); }); bindTrace(); })}`;
};
let bindTrace = () => {};
async function traceView(oid, p) {
  const t = await api(`/api/v1/obligations/${oid}/trace`), o = t.obligation;
  const docs = await api(`/api/v1/projects/${S.project}/documents`);
  const revs = docs.flatMap(d => d.revisions.map(r => ({ id: r.id, label: `${d.document_number} rev ${r.revision}` })));
  bindTrace = () => {
    $("#ev-go") && ($("#ev-go").onclick = async () => { try { await api(`/api/v1/obligations/${oid}/evidence`, { method: "POST", json: { document_revision_id: $("#ev-rev").value, note: $("#ev-note").value } }); toast("Evidence submitted"); render(); } catch (e) {} });
    document.querySelectorAll("[data-evr]").forEach(b => b.onclick = async () => { const comment = prompt(`${b.dataset.d} — reviewer comment`); if (!comment) return;
      try { await api(`/api/v1/evidence/${b.dataset.evr}/review`, { method: "POST", json: { decision: b.dataset.d, comment } }); toast(`Evidence ${b.dataset.d.toLowerCase()}ed`); render(); } catch (e) {} });
    $("#tg-go") && ($("#tg-go").onclick = async () => { try { await api(`/api/v1/obligations/${oid}/trigger`, { method: "POST", json: { occurred_on: $("#tg-d").value, note: "Trigger recorded in UI" } }); toast("Deadline computed"); render(); } catch (e) {} });
  };
  const link = (k, body) => `<div class="link"><div class="k">${k}</div>${body}</div>`;
  const parts = [];
  if (t.source_revision) {
    parts.push(link("Source document revision", `<b class="mono">${esc(t.source_revision.document_number)} rev ${esc(t.source_revision.revision)}</b> ${t.source_revision.governing ? pill("GOVERNING", "ok") : pill(t.source_revision.status)}<div class="mono small muted">sha256 ${esc(t.source_revision.sha256)}</div>`));
    parts.push(link(`Source clause ${esc(t.source_clause.reference)} · ${esc(t.source_clause.type)}`, `<b>${esc(t.source_clause.heading)}</b><div class="quote">${esc(t.source_passage.text)}</div><div class="small muted">passage ${esc(t.source_passage.id.slice(0,8))}${t.source_passage.page ? " · page " + t.source_passage.page : ""}</div>`));
    parts.push(link("Flow-down decision (human)", `${pill(t.flowdown.disposition, "ok")} <span class="small muted">proposed ${esc(t.flowdown.proposal)} by ${esc(t.flowdown.proposal_method)}</span><div class="small">${esc(t.flowdown.rationale)}</div>${t.flowdown.treatment ? `<div class="small"><b>Subcontract treatment:</b> ${esc(t.flowdown.treatment)}</div>` : ""}`));
  }
  parts.push(link(`Obligation ${esc(o.number)}`, `<div>${esc(o.statement)}</div><div class="small muted">Owner ${esc(o.owner_role)} · trigger: ${esc(o.trigger)} · ${o.notice_period_days ? o.notice_period_days + " " + o.day_basis.toLowerCase() + " days" : "no fixed period"} · due ${esc(o.due_at || "on trigger")} · ${esc(o.timezone)}</div>
    <div class="small"><b>Evidence required:</b> ${esc(o.evidence_required || "—")}</div>
    ${o.notice_period_days && !o.due_at ? `<div class="row small" style="margin-top:8px">Trigger event date <input type="date" id="tg-d"><button id="tg-go">Start clock</button></div>` : ""}`));
  parts.push(link("Evidence & closure", `${t.evidence.length ? t.evidence.map(e => `<div style="margin:4px 0"><b class="mono">${esc(e.document_number)} rev ${esc(e.revision)}</b> ${pill(e.review_status, statusCls(e.review_status))}<div class="small muted">${esc(e.note)}${e.review_comment ? " · reviewer: " + esc(e.review_comment) : ""}</div>
      ${e.review_status === "SUBMITTED" ? `<div class="row"><button data-evr="${e.id}" data-d="ACCEPT">Accept &amp; close</button><button class="danger" data-evr="${e.id}" data-d="REJECT">Reject</button></div>` : ""}</div>`).join("") : `<p class="muted small">No evidence yet.</p>`}
    ${o.status !== "CLOSED" ? `<div class="row small" style="margin-top:8px"><select id="ev-rev">${revs.map(r => `<option value="${r.id}">${esc(r.label)}</option>`).join("")}</select><input id="ev-note" placeholder="note"><button class="primary" id="ev-go">Submit evidence</button></div>` : ""}`));
  return `<div class="card"><h2>Chain of evidence</h2><div class="chain">${parts.join(`<div class="arrow"></div>`)}</div></div>`;
}

VIEWS.knowledge = async () => {
  const p = await api(`/api/v1/projects/${S.project}`);
  return `<h1>Knowledge query</h1><p class="sub">Evidence-grounded retrieval over governing revisions you are authorised to read. Answers quote sources and never invent clause references.</p>
  <div class="card"><div class="row"><input id="kq" style="flex:1" placeholder="e.g. What notice period applies to delay events?" value="What notice period applies to delay events?">
    <select id="ksub"><option value="">All project documents</option>${p.subcontracts.map(s => `<option value="${s.id}">${esc(s.subcontract_number)}</option>`).join("")}</select>
    <label class="small"><input type="checkbox" id="ksup"> include superseded</label><label class="small"><input type="checkbox" id="kllm"> draft answer with assigned model</label><button class="primary" id="kgo">Ask</button></div>
    <div class="row small muted" style="margin-top:8px">Try: ${["What does clause 9.2 say?", "What is the rate of liquidated damages?", "Retention release", "Who won the cricket world cup?"].map(x => `<a href="#" data-try="${esc(x)}">${esc(x)}</a>`).join(" · ")}</div></div>
  <div id="kans"></div>
  ${on(() => { const ask = async () => { const r = await api("/api/v1/knowledge/queries", { method: "POST", json: { projectId: S.project, question: $("#kq").value, subcontractId: $("#ksub").value || null, includeSupersededEvidence: $("#ksup").checked, draftWithModel: $("#kllm").checked } });
      $("#kans").innerHTML = `<div class="card"><div class="row"><h2>Answer</h2>${pill(r.status, r.status === "SUPPORTED" ? "ok" : r.status === "INSUFFICIENT_EVIDENCE" ? "bad" : "warn")}${r.requiresProfessionalReview ? pill("REQUIRES PROFESSIONAL REVIEW", "info") : ""}<span class="spacer"></span><span class="small muted mono">${esc(r.method)}</span></div>
        ${r.answer ? `<p>${esc(r.answer)}</p>` : `<p><b>Insufficient evidence.</b> ${esc(r.missingEvidence.join(" "))}</p>`}
        ${r.answerCitations?.length ? `<p class="small muted">Model draft verified against retrieved sources: ${r.answerCitations.map(c => `${esc(c.label)} = ${esc(c.documentNumber)}${c.clauseReference ? " cl. " + esc(c.clauseReference) : ""}`).join(" · ")}</p>` : ""}
        ${r.contradictions.map(c => `<div class="gate blocked"><b>Contradiction — clause ${esc(c.clause)}</b>: ${Object.entries(c.values).map(([v, src]) => `${esc(v)} days in ${esc(src.join(", "))}`).join(" vs ")}. ${esc(c.note)}</div>`).join("")}
        ${r.warnings.map(w => `<div class="gate blocked">${esc(w)}</div>`).join("")}
        <h3>Sources</h3>${r.sources.map((s, i) => `<div style="margin-bottom:10px"><div class="small mono">[S${i + 1}] ${esc(s.documentNumber)} rev ${esc(s.revision)} ${s.governing ? pill("GOVERNING", "ok") : s.approval === "NOT_APPROVED" ? pill("NOT APPROVED", "warn") : pill("SUPERSEDED")} ${s.clauseReference ? "cl. " + esc(s.clauseReference) : ""} ${s.page ? "p." + s.page : ""} ${s.cell ? esc(s.cell) : ""} · score ${s.score}</div><div class="quote">${esc(s.excerpt)}</div></div>`).join("") || `<p class="muted">None.</p>`}</div>`; };
    $("#kgo").onclick = ask; $("#kq").onkeydown = (e) => e.key === "Enter" && ask();
    document.querySelectorAll("[data-try]").forEach(a => a.onclick = (e) => { e.preventDefault(); $("#kq").value = a.dataset.try; ask(); }); })}`;
};

VIEWS.audit = async () => {
  const ev = await api(`/api/v1/audit-events?project_id=${S.project}`);
  return `<h1>Audit trail</h1><p class="sub">Append-only record of who did what, when, to which record, and why.</p>
  <div class="card"><table><tr><th>#</th><th>When (UTC)</th><th>Actor</th><th>Action</th><th>Object</th><th>Change</th></tr>
  ${ev.map(e => `<tr><td class="mono">${e.id}</td><td class="mono small">${esc(e.at.replace("T", " ").slice(0, 19))}</td><td>${esc(e.actor)}</td><td class="mono small">${esc(e.action)}</td><td class="small">${esc(e.object_type)} <span class="mono muted">${esc(e.object_id.slice(0,8))}</span></td>
    <td class="small">${e.before ? `<span class="muted">before</span> <span class="mono">${esc(JSON.stringify(e.before).slice(0, 140))}</span><br>` : ""}${e.after ? `<span class="muted">after</span> <span class="mono">${esc(JSON.stringify(e.after).slice(0, 160))}</span>` : ""}${e.reason ? `<br><span class="muted">reason</span> ${esc(e.reason)}` : ""}</td></tr>`).join("")}</table></div>`;
};

VIEWS.ai = async () => {
  const [cat, provs, asg, me] = await Promise.all([api("/api/v1/llm/catalogue"), api("/api/v1/llm/providers"),
                                                    api("/api/v1/llm/assignments"), api("/api/v1/me")]);
  const admin = me.roles.includes("system_admin");
  const calls = admin ? await api("/api/v1/llm/calls?limit=25") : [];
  const opt = (sel) => `<option value="">Built-in rules / extractive (no LLM)</option>` + provs.filter(p => p.enabled)
    .map(p => `<option value="${p.id}" ${p.id === sel ? "selected" : ""}>${esc(p.name)} — ${esc(p.provider_label)} · ${esc(p.model)} · ${esc(p.data_zone)}</option>`).join("");
  return `<h1>AI providers</h1><p class="sub">The OS is LLM-agnostic. Register any provider by its endpoint, then choose which one serves each purpose. With none assigned, the built-in rules and extractive retrieval are used. Model output is always a proposal that a person reviews.</p>
  ${admin ? "" : `<div class="gate blocked" style="margin-bottom:14px">Read-only: only a system administrator can register providers or change assignments.</div>`}
  <div class="card"><h2>Assignments</h2><div class="form">
    ${Object.entries(cat.purposes).map(([k, label]) => `<label>${esc(label)}</label><select data-asg="${k}" ${admin ? "" : "disabled"}>${opt(asg[k])}</select>`).join("")}</div>
    <p class="small muted">Data-zone policy: documents classified above <b>${esc(cat.cloud_max_classification)}</b> are never sent to CLOUD-zone providers; the rules proposer is used and the reason is recorded on the proposal.</p>
    ${admin ? `<button class="primary" id="asg-save">Save assignments</button>` : ""}</div>
  <div class="card"><h2>Registered providers</h2>${provs.length ? `<table><tr><th>Name</th><th>Provider</th><th>Endpoint</th><th>Model</th><th>Zone</th><th>Key</th><th>Assigned</th><th></th></tr>
    ${provs.map(p => `<tr><td><b>${esc(p.name)}</b>${p.enabled ? "" : " " + pill("DISABLED")}</td><td>${esc(p.provider_label)}<div class="small muted mono">${esc(p.protocol)}</div></td>
      <td class="mono small">${esc(p.base_url)}</td><td class="mono small">${esc(p.model)}</td><td>${pill(p.data_zone, p.data_zone === "PRIVATE" ? "ok" : "info")}</td>
      <td class="small">${esc(p.key_source)} ${p.key_hint ? `<span class="mono">${esc(p.key_hint)}</span>` : ""}</td><td class="small">${esc(p.assigned_to.join(", ") || "—")}</td>
      <td class="row" style="flex-wrap:nowrap">${admin ? `<button data-test="${p.id}">Test</button><button data-toggle="${p.id}" data-en="${p.enabled}">${p.enabled ? "Disable" : "Enable"}</button><button class="danger" data-del="${p.id}">Remove</button>` : ""}</td></tr>`).join("")}</table>`
    : `<p class="muted">No providers registered yet.</p>`}</div>
  ${admin ? `<div class="card"><h2>Register a provider</h2><div class="form">
    <label>Provider</label><select id="p-type">${cat.providers.map(c => `<option value="${c.key}">${esc(c.label)}</option>`).join("")}</select>
    <label>Display name</label><input id="p-name" placeholder="e.g. Private Llama on GPU node">
    <label>Endpoint (base URL)</label><input id="p-url">
    <label>Model / deployment</label><input id="p-model" placeholder="model identifier exactly as the endpoint expects">
    <label>API key</label><input id="p-key" type="password" placeholder="stored encrypted, never shown again">
    <label>…or key from env var</label><input id="p-env" placeholder="e.g. SCOS_OPENAI_KEY (preferred in production)">
    <label>Data zone</label><select id="p-zone"><option>CLOUD</option><option>PRIVATE</option></select>
    <label>Temperature / max tokens</label><div class="row"><input id="p-temp" value="0" style="width:80px"><input id="p-max" value="800" style="width:100px"></div>
    <label>API version (Azure)</label><input id="p-ver" placeholder="only for Azure OpenAI">
    <label>Extra headers (JSON)</label><input id="p-hdr" placeholder='{"HTTP-Referer": "https://your.org"}'>
    </div><p class="small muted" id="p-note"></p><button class="primary" id="p-go">Register provider</button></div>
  <div class="card"><h2>Recent model calls</h2><p class="small muted">Metadata only. Prompt and response text are never logged.</p>${calls.length ? `<table><tr><th>When</th><th>Provider</th><th>Purpose</th><th>Result</th><th>Latency</th><th>Chars in/out</th></tr>
    ${calls.map(c => `<tr><td class="mono small">${esc(c.at.replace("T", " ").slice(0, 19))}</td><td>${esc(c.provider)} <span class="mono small muted">${esc(c.model)}</span></td><td>${esc(c.purpose)}</td><td>${pill(c.ok ? "OK" : "FAILED", c.ok ? "ok" : "bad")} <span class="small muted">${c.ok ? "" : esc(c.outcome)}</span></td><td class="mono">${c.latency_ms} ms</td><td class="mono small">${c.prompt_chars}/${c.response_chars}</td></tr>`).join("")}</table>` : `<p class="muted">No calls yet.</p>`}</div>` : ""}
  ${on(() => {
    if (!admin) return;
    const fill = () => { const c = cat.providers.find(x => x.key === $("#p-type").value); $("#p-url").value = c.default_base_url; $("#p-zone").value = c.default_zone;
      $("#p-note").textContent = `${c.protocol} protocol · ${c.requires_key ? "API key required" : "key optional"}${c.note ? " · " + c.note : ""}`; };
    $("#p-type").onchange = fill; fill();
    $("#p-go").onclick = async () => { let headers = {};
      try { headers = $("#p-hdr").value ? JSON.parse($("#p-hdr").value) : {}; } catch (e) { return toast("Extra headers must be JSON", true); }
      const extra = {}; if ($("#p-ver").value) extra.api_version = $("#p-ver").value; if (Object.keys(headers).length) extra.headers = headers;
      try { await api("/api/v1/llm/providers", { method: "POST", json: { name: $("#p-name").value, provider_type: $("#p-type").value, base_url: $("#p-url").value,
        model: $("#p-model").value, api_key: $("#p-key").value || null, api_key_env: $("#p-env").value || null, data_zone: $("#p-zone").value,
        temperature: Number($("#p-temp").value), max_tokens: Number($("#p-max").value), extra } }); toast("Provider registered"); render(); } catch (e) {} };
    $("#asg-save").onclick = async () => { const body = {}; document.querySelectorAll("[data-asg]").forEach(s => body[s.dataset.asg] = s.value || null);
      try { await api("/api/v1/llm/assignments", { method: "PUT", json: body }); toast("Assignments saved"); render(); } catch (e) {} };
    document.querySelectorAll("[data-test]").forEach(b => b.onclick = async () => { b.disabled = true; b.textContent = "Testing…";
      const r = await api(`/api/v1/llm/providers/${b.dataset.test}/test`, { method: "POST" }).catch(() => ({ ok: false, error: "request failed" }));
      toast(r.ok ? `Connected in ${r.latency_ms} ms: ${r.sample}` : `Test failed: ${r.error}`, !r.ok); render(); });
    document.querySelectorAll("[data-toggle]").forEach(b => b.onclick = async () => { try { await api(`/api/v1/llm/providers/${b.dataset.toggle}`, { method: "PATCH", json: { enabled: b.dataset.en !== "true" } }); render(); } catch (e) {} });
    document.querySelectorAll("[data-del]").forEach(b => b.onclick = async () => { if (!confirm("Remove this provider?")) return; try { await api(`/api/v1/llm/providers/${b.dataset.del}`, { method: "DELETE" }); toast("Removed"); render(); } catch (e) {} });
  })}`;
};

VIEWS.users = async () => {
  const [d, log, idp] = await Promise.all([api("/api/v1/users"), api("/api/v1/users/audit"), api("/api/v1/auth/identity-providers")]);
  return `<h1>Users &amp; access</h1><p class="sub">Create accounts with one-time temporary passwords, reset passwords, unlock and deactivate users. Project access is granted per project by its coordinator.</p>
  <div class="card"><h2>Accounts</h2><table><tr><th>User</th><th>E-mail (company sign-in)</th><th>Global roles</th><th>Clearance</th><th>Status</th><th>Last sign-in</th><th></th></tr>
  ${d.users.map(u => `<tr><td><b>${esc(u.username)}</b><div class="small muted">${esc(u.full_name)}</div></td><td class="small">${esc(u.email || "—")} <a href="#" data-email="${esc(u.username)}" data-cur="${esc(u.email || "")}">edit</a></td><td class="small">${esc(u.roles.join(", ") || "—")}</td><td>${esc(u.clearance)}</td>
    <td>${u.is_active ? pill("ACTIVE", "ok") : pill("DISABLED", "bad")} ${u.locked ? pill("LOCKED", "bad") : ""} ${u.must_change_password ? pill("TEMP PASSWORD", "warn") : ""}</td>
    <td class="mono small">${esc((u.last_login_at || "never").replace("T", " ").slice(0, 16))}</td>
    <td class="row" style="flex-wrap:nowrap"><button data-reset="${esc(u.username)}">Reset password</button>${u.locked ? `<button data-unlock="${esc(u.username)}">Unlock</button>` : ""}
      <button class="${u.is_active ? "danger" : ""}" data-active="${esc(u.username)}" data-to="${!u.is_active}">${u.is_active ? "Disable" : "Enable"}</button></td></tr>`).join("")}</table></div>
  <div class="card"><h2>New user</h2><div class="form">
    <label>Username</label><input id="nu-name" placeholder="firstname.lastname">
    <label>Full name</label><input id="nu-full">
    <label>Work e-mail</label><input id="nu-email" placeholder="invites the user to company sign-in (optional)">
    <label>Global roles</label><select id="nu-roles" multiple size="5">${d.roles.map(r => `<option>${r}</option>`).join("")}</select>
    <label>Clearance</label><select id="nu-clr">${d.clearances.map(c => `<option ${c === "CONFIDENTIAL" ? "selected" : ""}>${c}</option>`).join("")}</select></div>
    <p class="small muted">Most roles are granted per project by its coordinator (Portfolio control → Project team). Global roles are for registry, coordination and administration.</p>
    <button class="primary" id="nu-go">Create user</button></div>
  ${idpCard(idp)}
  <div class="card"><h2>Sign-in and account events</h2><table><tr><th>When (UTC)</th><th>Actor</th><th>Event</th><th>Account</th></tr>
  ${log.map(e => `<tr><td class="mono small">${esc(e.at.replace("T", " ").slice(0, 19))}</td><td>${esc(e.actor)}</td><td class="mono small">${esc(e.action)}</td><td>${esc(e.subject)}</td></tr>`).join("")}</table></div>
  ${on(() => {
    const showTemp = (r) => openModal(`<h2>Temporary password for ${esc(r.username)}</h2><p>Shown once. Give it to the user through a secure channel; they must change it at first sign-in.</p><p class="secret">${esc(r.temporary_password)}</p>`);
    $("#nu-go").onclick = async () => { try { const r = await api("/api/v1/users", { method: "POST", json: { username: $("#nu-name").value, full_name: $("#nu-full").value,
      roles: [...$("#nu-roles").selectedOptions].map(o => o.value), clearance: $("#nu-clr").value, email: $("#nu-email").value || null } }); render(); showTemp(r); } catch (e) {} };
    document.querySelectorAll("[data-email]").forEach(a => a.onclick = async (ev) => { ev.preventDefault(); const v = prompt(`Work e-mail for ${a.dataset.email} (empty to clear)`, a.dataset.cur); if (v === null) return;
      try { await api(`/api/v1/users/${a.dataset.email}`, { method: "PATCH", json: { email: v } }); toast("E-mail saved"); render(); } catch (e) {} });
    bindIdp(idp);
    document.querySelectorAll("[data-reset]").forEach(b => b.onclick = async () => { if (!confirm(`Issue a new temporary password for ${b.dataset.reset}? Their sessions will be signed out.`)) return;
      try { const r = await api(`/api/v1/users/${b.dataset.reset}/reset-password`, { method: "POST" }); render(); showTemp(r); } catch (e) {} });
    document.querySelectorAll("[data-unlock]").forEach(b => b.onclick = async () => { try { await api(`/api/v1/users/${b.dataset.unlock}`, { method: "PATCH", json: { unlock: true } }); toast("Unlocked"); render(); } catch (e) {} });
    document.querySelectorAll("[data-active]").forEach(b => b.onclick = async () => { try { await api(`/api/v1/users/${b.dataset.active}`, { method: "PATCH", json: { is_active: b.dataset.to === "true" } }); render(); } catch (e) {} });
  })}`;
};

function idpCard(idp) {
  return `<div class="card"><div class="row"><h2>Sign-in providers</h2><span class="spacer"></span>
    <label class="small">Password sign-in <select id="pw-policy"><option value="all" ${idp.password_login === "all" ? "selected" : ""}>Everyone</option>
    <option value="admins_only" ${idp.password_login === "admins_only" ? "selected" : ""}>Administrators only</option></select></label></div>
    <p class="small muted">Any OpenID Connect provider. Accounts and roles stay here; a provider only proves identity. Invite people by setting their work e-mail above — they are linked at first company sign-in. Redirect URI to register with the provider: <span class="mono">${esc(idp.redirect_uri)}</span></p>
    ${idp.providers.length ? `<table><tr><th>Name</th><th>Provider</th><th>Issuer</th><th>Client</th><th>Domains</th><th>New users</th><th>Linked</th><th></th></tr>
      ${idp.providers.map(p => `<tr><td><b>${esc(p.name)}</b>${p.enabled ? "" : " " + pill("DISABLED")}<div class="small muted">${esc(p.button_label)}</div></td><td class="small">${esc(p.provider_label)}</td>
        <td class="mono small">${esc(p.issuer)}</td><td class="small"><span class="mono">${esc(p.client_id)}</span><div class="muted">${esc(p.secret_source)}</div></td>
        <td class="small">${esc(p.allowed_domains.join(", ") || "any")}</td><td class="small">${p.auto_provision ? "auto-create (no roles)" : "invite only"}</td><td class="mono">${p.linked_accounts}</td>
        <td class="row" style="flex-wrap:nowrap"><button data-idtest="${p.id}">Test</button><button data-idtoggle="${p.id}" data-en="${p.enabled}">${p.enabled ? "Disable" : "Enable"}</button><button class="danger" data-iddel="${p.id}">Remove</button></td></tr>`).join("")}</table>`
      : `<p class="muted">No providers yet — everyone signs in with username and password.</p>`}
    <h3>Register a provider</h3><div class="form">
      <label>Provider</label><select id="ip-type">${idp.catalogue.map(c => `<option value="${c.key}">${esc(c.label)}</option>`).join("")}</select>
      <label>Display name</label><input id="ip-name" placeholder="e.g. Company Microsoft 365">
      <label>Issuer URL</label><input id="ip-iss">
      <label>Client ID</label><input id="ip-cid">
      <label>Client secret</label><input id="ip-sec" type="password" placeholder="stored encrypted, never shown again">
      <label>…or secret from env var</label><input id="ip-env" placeholder="e.g. SCOS_SSO_SECRET (preferred in production)">
      <label>Button label</label><input id="ip-btn">
      <label>Allowed e-mail domains</label><input id="ip-dom" placeholder="yourcompany.com, subsidiary.in">
      <label>New people</label><select id="ip-auto"><option value="0">Invite only (recommended)</option><option value="1">Create account automatically — no roles until granted</option></select>
    </div><p class="small muted" id="ip-note"></p><button class="primary" id="ip-go">Register provider</button></div>`;
}
function bindIdp(idp) {
  const fill = () => { const c = idp.catalogue.find(x => x.key === $("#ip-type").value); $("#ip-iss").value = c.issuer_template; $("#ip-btn").value = c.button_label; $("#ip-note").textContent = c.note; };
  $("#ip-type").onchange = fill; fill();
  $("#pw-policy").onchange = async (e) => { try { await api("/api/v1/auth/policy", { method: "PUT", json: { password_login: e.target.value } }); toast("Sign-in policy saved"); } catch (er) { render(); } };
  $("#ip-go").onclick = async () => { try { await api("/api/v1/auth/identity-providers", { method: "POST", json: { name: $("#ip-name").value, provider_type: $("#ip-type").value,
      issuer: $("#ip-iss").value.trim(), client_id: $("#ip-cid").value.trim(), client_secret: $("#ip-sec").value || null, client_secret_env: $("#ip-env").value || null,
      button_label: $("#ip-btn").value, allowed_domains: $("#ip-dom").value.split(",").map(x => x.trim()).filter(Boolean), auto_provision: $("#ip-auto").value === "1" } });
      toast("Provider registered — click Test"); render(); } catch (e) {} };
  document.querySelectorAll("[data-idtest]").forEach(b => b.onclick = async () => { b.disabled = true; b.textContent = "Testing…";
    const r = await api(`/api/v1/auth/identity-providers/${b.dataset.idtest}/test`, { method: "POST" }).catch(() => ({ ok: false, error: "request failed" }));
    r.ok ? openModal(`<h2>Connection OK</h2><p>Issuer <span class="mono">${esc(r.issuer)}</span> · ${r.signing_keys} signing key(s) · PKCE ${r.pkce_s256 ? "supported" : "NOT advertised"}</p><p class="small">Register this redirect URI with the provider:</p><p class="secret">${esc(r.redirect_uri_to_register)}</p>`)
         : toast(`Test failed: ${r.error}`, true); b.disabled = false; b.textContent = "Test"; });
  document.querySelectorAll("[data-idtoggle]").forEach(b => b.onclick = async () => { try { await api(`/api/v1/auth/identity-providers/${b.dataset.idtoggle}`, { method: "PATCH", json: { enabled: b.dataset.en !== "true" } }); render(); } catch (e) {} });
  document.querySelectorAll("[data-iddel]").forEach(b => b.onclick = async () => { if (!confirm("Remove this provider and unlink every account linked to it?")) return;
    try { await api(`/api/v1/auth/identity-providers/${b.dataset.iddel}`, { method: "DELETE" }); toast("Removed"); render(); } catch (e) {} });
}

boot();
