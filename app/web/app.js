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
    if (e.code === "MFA_ENROLMENT_REQUIRED") { showMfaSetup(true); throw e; }
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
  if (S.me.mfa_enrolment_required) return showMfaSetup(true);
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
    try { const r = await api("/api/v1/auth/login", { method: "POST", quiet: true, json: { username: $("#l-user").value, password: $("#l-pw").value } });
          r.mfa_required ? showMfaStep(r.mfa_token) : boot(); }
    catch (e) { $("#l-err").textContent = e.message || "Sign-in failed"; }
  };
  $("#l-go").onclick = go; $("#l-pw").onkeydown = (e) => { if (e.key === "Enter") go(); };
  $("#pw-show") && ($("#pw-show").onclick = (e) => { e.preventDefault(); $("#pw-form").classList.remove("hidden"); e.target.remove(); });
  if (!pwHidden) $("#l-user").focus();
}
async function showMyAccount() {
  const [mine, opts, mfaHtml] = await Promise.all([api("/api/v1/me/identities"), api("/api/v1/auth/providers"), mfaSection()]);
  const linked = new Set(mine.map(x => x.provider_id));
  openModal(`<h2>My sign-in</h2><p class="small muted">Signed in as <b>${esc(S.me.username)}</b>. Link a company login to sign in with it next time.</p>
    <h3>Linked company logins</h3>${mine.length ? `<table>${mine.map(x => `<tr><td><b>${esc(x.provider)}</b><div class="small muted">${esc(x.email || "")}</div></td>
      <td class="small mono">${esc((x.last_used_at || x.linked_at).replace("T", " ").slice(0, 16))}</td><td><button class="danger" data-unlink="${x.id}">Unlink</button></td></tr>`).join("")}</table>`
      : `<p class="muted small">None yet.</p>`}
    ${opts.providers.filter(p => !linked.has(p.id)).map(p => `<button data-link="${p.id}">Link: ${esc(p.label)}</button> `).join("")}
    ${mfaHtml}
    <h3>Password</h3><button id="my-pw">Change password</button>`);
  bindMfaSection();
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
// ---------------------------------------------------------------- authenticator app (ADR-0007)
function showMfaStep(token) {
  $("#login-body").innerHTML = `<h2>Authenticator code</h2>
    <p class="hint" style="margin-top:0">Open your authenticator app and enter the 6-digit code for <b>Subcontract OS</b>.</p>
    <label>Code</label><input id="m-code" inputmode="numeric" autocomplete="one-time-code" maxlength="20" placeholder="123 456">
    <button class="primary" id="m-go">Verify</button>
    <div class="err" id="m-err"></div>
    <p class="hint">Lost your phone? Type one of your recovery codes (like <span class="mono">ABCDE-FGHJK</span>) instead. Each works once.</p>
    <a href="#" id="m-back" class="small">Back to sign-in</a>`;
  const go = async () => {
    try { const r = await api("/api/v1/auth/login/mfa", { method: "POST", quiet: true, json: { mfa_token: token, code: $("#m-code").value } });
          if (r.recovery_codes_left !== undefined && r.recovery_codes_left <= 3) toast(`Only ${r.recovery_codes_left} recovery codes left — make new ones in My sign-in.`, true);
          boot(); }
    catch (e) { if (e.code === "MFA_CHALLENGE_EXPIRED" || e.code === "ACCOUNT_LOCKED") return showLogin(e.message);
                $("#m-err").textContent = e.message || "Code not accepted"; $("#m-code").select(); }
  };
  $("#m-go").onclick = go; $("#m-code").onkeydown = (e) => { if (e.key === "Enter") go(); };
  $("#m-back").onclick = (e) => { e.preventDefault(); showLogin(); };
  $("#m-code").focus();
}
function showRecoveryCodes(codes, then) {
  const text = codes.join("\n");
  return `<h2>Recovery codes</h2><p class="hint" style="margin-top:0">Shown once. Each code signs you in once if your phone is lost. Print them or keep them in a password manager — not on the same phone.</p>
    <div class="codes mono">${codes.map(c => `<span>${esc(c)}</span>`).join("")}</div>
    <div class="row"><button id="rc-copy">Copy</button><button id="rc-dl">Download .txt</button></div>
    <label class="small" style="display:flex;gap:6px;align-items:center;margin-top:14px"><input type="checkbox" id="rc-ok" style="width:auto"> I have saved these codes</label>
    <button class="primary" id="rc-done" disabled>${then}</button>`;
}
function bindRecoveryCodes(codes, done) {
  const text = codes.join("\n");
  $("#rc-copy").onclick = async () => { try { await navigator.clipboard.writeText(text); toast("Copied"); } catch (e) { toast("Copy failed — select and copy them by hand", true); } };
  $("#rc-dl").onclick = () => { const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([`Subcontract OS recovery codes for ${S.me?.username || ""}\n\n${text}\n`], { type: "text/plain" }));
    a.download = "subcontract-os-recovery-codes.txt"; a.click(); URL.revokeObjectURL(a.href); };
  $("#rc-ok").onchange = (e) => { $("#rc-done").disabled = !e.target.checked; };
  $("#rc-done").onclick = done;
}
function showMfaSetup(forced) {
  $("#login").classList.remove("hidden");
  const panel = $("#login-body");
  panel.innerHTML = `<h2>Set up an authenticator app</h2>
    ${forced ? `<p class="hint" style="margin-top:0">Your organisation requires a second step at sign-in. This takes about a minute.</p>` : ""}
    <p class="hint" style="margin-top:0">You need an authenticator app on your phone — Microsoft Authenticator, Google Authenticator, Authy or a password manager. First confirm your password.</p>
    <label>Password</label><input id="s-pw" type="password" autocomplete="current-password">
    <div class="err" id="s-err"></div><button class="primary" id="s-go">Continue</button>
    ${forced ? `<a href="#" id="s-out" class="small">Sign out</a>` : `<button id="s-cancel">Cancel</button>`}`;
  const cancel = () => forced ? $("#logout-btn").click() : $("#login").classList.add("hidden");
  const back = $("#s-out") || $("#s-cancel"); back.onclick = (e) => { e.preventDefault(); cancel(); };
  const step1 = async () => {
    let r; try { r = await api("/api/v1/me/mfa/totp/setup", { method: "POST", quiet: true, json: { password: $("#s-pw").value } }); }
    catch (e) { return ($("#s-err").textContent = e.message || "Could not start set-up"); }
    panel.innerHTML = `<h2>Scan this code</h2>
      <p class="hint" style="margin-top:0">In the app choose <b>Add account</b> → <b>Scan a QR code</b>.</p>
      <div class="qr">${r.qr_svg || ""}</div>
      <p class="hint">Can't scan? Enter this key by hand (time-based, 6 digits):</p><p class="secret mono">${esc(r.secret)}</p>
      <label>Then type the 6-digit code the app shows</label><input id="s-code" inputmode="numeric" autocomplete="one-time-code" maxlength="7">
      <div class="err" id="s-err"></div><button class="primary" id="s-confirm">Turn on</button>
      <button id="s-cancel2">Cancel</button>`;
    $("#s-cancel2").onclick = cancel;
    const confirm2 = async () => {
      let res; try { res = await api("/api/v1/me/mfa/totp/confirm", { method: "POST", quiet: true, json: { code: $("#s-code").value } }); }
      catch (e) { return ($("#s-err").textContent = e.message || "Code not accepted"); }
      panel.innerHTML = showRecoveryCodes(res.recovery_codes, "Finish");
      bindRecoveryCodes(res.recovery_codes, () => { toast("Authenticator app turned on"); forced ? boot() : $("#login").classList.add("hidden"); });
    };
    $("#s-confirm").onclick = confirm2; $("#s-code").onkeydown = (e) => { if (e.key === "Enter") confirm2(); };
    $("#s-code").focus();
  };
  $("#s-go").onclick = step1; $("#s-pw").onkeydown = (e) => { if (e.key === "Enter") step1(); };
  $("#s-pw").focus();
}
async function mfaSection() {
  const m = await api("/api/v1/me/mfa");
  if (!m.available) return `<h3>Authenticator app</h3><p class="small muted">You sign in with a company login; its provider handles the second step.</p>`;
  return `<h3>Authenticator app</h3>${m.enabled
    ? `<p class="small">${pill("ON", "ok")} since ${esc(m.enabled_at.slice(0, 10))} · ${m.recovery_codes_left} recovery codes left${m.required ? " · required by your organisation" : ""}</p>
       <div class="row"><button id="mfa-codes">New recovery codes</button>${m.required ? "" : `<button class="danger" id="mfa-off">Turn off</button>`}</div>`
    : `<p class="small muted">Off. Adds a 6-digit code from your phone after your password.</p><button id="mfa-on">Set up authenticator app</button>`}`;
}
function bindMfaSection() {
  $("#mfa-on") && ($("#mfa-on").onclick = () => { closeModal(); showMfaSetup(false); });
  $("#mfa-codes") && ($("#mfa-codes").onclick = () => {
    openModal(`<h2>New recovery codes</h2><p class="small muted">Your current recovery codes will stop working.</p><div class="form">
      <label>Code from your app</label><input id="rc-code" inputmode="numeric" autocomplete="one-time-code"></div><button class="primary" id="rc-make">Make new codes</button>`);
    $("#rc-make").onclick = async () => { try { const r = await api("/api/v1/me/mfa/recovery-codes", { method: "POST", json: { code: $("#rc-code").value } });
      $("#modal-body").innerHTML = showRecoveryCodes(r.recovery_codes, "Done"); bindRecoveryCodes(r.recovery_codes, closeModal); } catch (e) {} };
  });
  $("#mfa-off") && ($("#mfa-off").onclick = () => {
    openModal(`<h2>Turn off the authenticator app</h2><p class="small muted">Sign-in will need only your password.</p><div class="form">
      <label>Password</label><input id="off-pw" type="password" autocomplete="current-password">
      <label>Code from your app (or a recovery code)</label><input id="off-code" autocomplete="one-time-code"></div><button class="danger" id="off-go">Turn off</button>`);
    $("#off-go").onclick = async () => { try { await api("/api/v1/me/mfa/totp/disable", { method: "POST", json: { password: $("#off-pw").value, code: $("#off-code").value } });
      toast("Authenticator app turned off"); showMyAccount(); } catch (e) {} };
  });
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
    <table><tr><th>User</th><th>Project roles</th><th></th></tr>${members.map(m => `<tr><td><b>${esc(m.username)}</b> <span class="small muted">${esc(m.full_name)}</span></td><td class="small">${esc(m.roles.join(", "))}${(m.directory_roles || []).map(r => ` <span class="pill info" title="From a directory group">${esc(r)} · directory</span>`).join("")}</td>
      <td>${isCoord ? `<button class="danger" data-rm="${esc(m.username)}">Remove</button>` : ""}</td></tr>`).join("")}</table>
    ${isCoord ? `<div class="row" style="margin-top:10px"><input id="m-user" placeholder="username"><select id="m-role">${["contracts_manager","legal_reviewer","contracts_approver","commercial_manager","qaqc_manager","hse_manager","construction_manager","project_controls","procurement_manager","auditor","registry_manager","project_coordinator"].map(r => `<option>${r}</option>`).join("")}</select><button class="primary" id="m-add">Add to project</button></div>` : ""}</div>
  ${d.variations ? `<div class="card"><div class="row"><h2>Variations</h2><span class="spacer"></span><a class="small" id="dash-var">Open register →</a></div>
    <p>${d.variations.open} open · ${d.variations.awaiting_approval} awaiting approval · open additions <b class="mono">${inr(d.variations.open_exposure)}</b> · open omissions <b class="mono">${inr(d.variations.open_omissions)}</b> · approved not incorporated <b class="mono">${inr(d.variations.approved_not_incorporated)}</b></p>
    ${d.variations.notice_alerts.length ? `<table><tr><th>No.</th><th>Variation</th><th>Notice</th></tr>${d.variations.notice_alerts.map(a => `<tr class="click" data-dvar="${a.id}"><td class="mono">${esc(a.number)}</td><td>${esc(a.title)}</td><td>${noticePill(a)} <span class="small">${esc(a.message)}</span></td></tr>`).join("")}</table>` : ""}</div>` : ""}
  <div class="card"><h2>Flow-down status</h2><p>${d.flowdown.total} source provisions mapped across subcontracts · ${d.flowdown.unresolved} without an approved disposition (${d.flowdown.unresolved_high} HIGH risk).</p>
  <p class="small muted">Obligations: ${d.obligations.total} total · ${d.obligations.open} open · ${d.obligations.closed} closed with accepted evidence. Every figure drills down to source records.</p></div>
  ${on(() => {
    $("#m-add") && ($("#m-add").onclick = async () => { try { await api(`/api/v1/projects/${S.project}/members`, { method: "POST", json: { username: $("#m-user").value.trim(), roles: [$("#m-role").value] } }); toast("Member added"); render(); } catch (e) {} });
    document.querySelectorAll("[data-rm]").forEach(b => b.onclick = async () => { if (!confirm(`Remove ${b.dataset.rm} from this project?`)) return; try { await api(`/api/v1/projects/${S.project}/members/${b.dataset.rm}`, { method: "DELETE" }); render(); } catch (e) {} });
    document.querySelectorAll("[data-ob]").forEach(r => r.onclick = () => go("obligations", { sub: r.dataset.sub, ob: r.dataset.ob }));
    $("#dash-var") && ($("#dash-var").onclick = () => go("variations"));
    document.querySelectorAll("[data-dvar]").forEach(r => r.onclick = () => go("variations", { vo: r.dataset.dvar, vsub: null }));
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

// ---------------------------------------------------------------- variations (ADR-0008)
const VAR_FLOW = ["IDENTIFIED","CLASSIFIED","NOTICE_REVIEW","ENTITLEMENT_ASSESSMENT","COST_AND_TIME_ASSESSMENT","COMMERCIAL_REVIEW","APPROVAL_PENDING","APPROVED","INCORPORATED","CLOSED"];
const VAR_LABEL = { IDENTIFIED: "Identified", CLASSIFIED: "Classified", NOTICE_REVIEW: "Notice", ENTITLEMENT_ASSESSMENT: "Entitlement",
  COST_AND_TIME_ASSESSMENT: "Cost & time", COMMERCIAL_REVIEW: "Commercial review", APPROVAL_PENDING: "Approval", APPROVED: "Approved",
  INCORPORATED: "Incorporated", CLOSED: "Closed", REJECTED: "Rejected", WITHDRAWN: "Withdrawn" };
const NOTICE_CLS = { GIVEN_ON_TIME: "ok", NOT_REQUIRED: "ok", PENDING: "info", GIVEN_LATE: "bad", MISSING_OVERDUE: "bad",
  NO_NOTICE_CLAUSE: "warn", CLOCK_NOT_STARTED: "warn", GIVEN_NO_EVIDENCE: "warn" };
const varCls = (s) => ({ INCORPORATED: "ok", APPROVED: "ok", CLOSED: "", REJECTED: "bad", WITHDRAWN: "", APPROVAL_PENDING: "warn" }[s] ?? "info");
const inr = (x) => x === null || x === undefined ? "—" : Number(x).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const noticePill = (n) => pill(n.status.replaceAll("_", " ") + (n.risk_decision ? " · decided" : ""), n.at_risk ? "bad" : NOTICE_CLS[n.status] || "");

VIEWS.variations = async () => {
  const p = await api(`/api/v1/projects/${S.project}`);
  const live = p.subcontracts.filter(s => ["EXECUTED", "ACTIVE"].includes(s.status));
  if (S.vsub && !live.find(s => s.id === S.vsub)) S.vsub = null;
  const d = await api(`/api/v1/projects/${S.project}/variations${S.vsub ? `?subcontract_id=${S.vsub}` : ""}`);
  const ref = d.reference;
  const pos = Object.entries(d.positions).filter(([k]) => !S.vsub || k === S.vsub);
  const k = (v, l, cls = "") => `<div class="kpi ${cls}"><div class="v ${String(v).length > 6 ? "money" : ""}">${v}</div><div class="l">${l}</div></div>`;
  const sumF = (f) => pos.reduce((a, [, x]) => a + Number(x[f]), 0);
  const detail = S.vo && d.variations.find(v => v.id === S.vo) ? await variationDetail(S.vo, ref) : "";
  const canRaise = (S.me.projects[S.project] || []).some(r => ["contracts_manager","commercial_manager","construction_manager","project_coordinator","project_controls"].includes(r));
  return `<h1>Variations</h1><p class="sub">From the event to the amendment: notice clock from the subcontract's own clause, entitlement with the clause cited, priced items, separate approval — and the contract value changes only on incorporation.</p>
  <div class="grid g5">
    ${k(d.summary.open, "Open variations")}
    ${k(d.summary.notice_at_risk, "Notice at risk (needs a decision)", d.summary.notice_at_risk ? "bad" : "")}
    ${k(d.summary.awaiting_approval, "Awaiting approval", d.summary.awaiting_approval ? "warn" : "")}
    ${k(inr(sumF("open_exposure")), "Open additions (exposure)", sumF("open_exposure") ? "warn" : "")}
    ${k(inr(sumF("open_omissions")), "Open omissions (not netted)")}
  </div><br>
  <div class="tabs"><a data-vsub="" class="${S.vsub ? "" : "on"}">All active subcontracts</a>${live.map(s => `<a data-vsub="${s.id}" class="${s.id === S.vsub ? "on" : ""}">${esc(s.subcontract_number)}</a>`).join("")}</div>
  ${pos.map(([, x]) => `<div class="card"><div class="row"><h2>Commercial position · ${esc(x.subcontract_number)}</h2><span class="spacer"></span><span class="small muted">${esc(x.currency)}</span></div>
    <div class="ladder">
      <div><span>Original (executed baseline)</span><b class="mono">${inr(x.original_value)}</b></div>
      <div><span>+ Other amendments</span><b class="mono">${inr(x.other_amendments)}</b></div>
      <div><span>+ Variations incorporated</span><b class="mono">${inr(x.variations_incorporated)}</b></div>
      <div class="tot"><span>= Current contract value</span><b class="mono">${inr(x.current_value)}</b></div>
      <div><span>+ Approved, not yet incorporated</span><b class="mono">${inr(x.approved_not_incorporated)}</b></div>
      <div class="tot"><span>= Forecast final value</span><b class="mono">${inr(x.forecast_final_value)}</b></div>
      <div class="aside"><span>Not in forecast: open additions / open omissions</span><b class="mono">${inr(x.open_exposure)} / ${inr(x.open_omissions)}</b></div>
      <div class="aside"><span>Time: days granted / days open</span><b class="mono">${x.time.days_granted} / ${x.time.days_open}</b></div>
    </div></div>`).join("")}
  <div class="grid" style="grid-template-columns:minmax(0,1fr) minmax(0,1.1fr)">
  <div class="card"><div class="row"><h2>Register</h2><span class="spacer"></span>${canRaise ? `<button id="ev-new">Record event</button><button class="primary" id="var-new">Raise variation</button>` : ""}</div>
    ${d.variations.length ? `<table><tr><th>No.</th><th>Variation · notice</th><th class="r">Latest estimate</th><th>Status</th></tr>
    ${d.variations.map(v => `<tr class="click" data-vo="${v.id}" style="${v.id === S.vo ? "background:#f2f9f9" : ""}"><td class="mono">${esc(v.number)}${S.vsub ? "" : `<div class="small muted">${esc(v.subcontract_number)}</div>`}</td>
      <td>${esc(v.title)}<div class="small muted">${esc((v.category || "unclassified").replaceAll("_", " ").toLowerCase())} · ${esc(v.initiated_by.toLowerCase())}</div><div style="margin-top:3px">${noticePill(v.notice)}</div></td><td class="mono r">${inr(v.approved_amount ?? v.estimate)}<div class="small muted">${esc(v.approved_amount ? "approved" : v.estimate_basis)}</div></td>
      <td>${pill(VAR_LABEL[v.status], varCls(v.status))}</td></tr>`).join("")}</table>` : `<p class="muted">No variations yet.</p>`}</div>
  <div>${detail || `<div class="card"><p class="muted">Select a variation to see its workflow, notice, valuation, evidence and history.</p></div>`}</div></div>
  ${on(() => {
    document.querySelectorAll("[data-vsub]").forEach(a => a.onclick = () => { S.vsub = a.dataset.vsub || null; S.vo = null; render(); });
    document.querySelectorAll("[data-vo]").forEach(r => r.onclick = () => { S.vo = r.dataset.vo; render(); });
    $("#var-new") && ($("#var-new").onclick = () => raiseVariationModal(live, ref));
    $("#ev-new") && ($("#ev-new").onclick = () => recordEventModal(live, ref));
    bindVariation();
  })}`;
};

async function recordEventModal(live, ref) {
  openModal(`<h2>Record a project event</h2><p class="small muted">Facts only — whether it gives entitlement is decided on a variation.</p><div class="form">
    <label>Type</label><select id="e-type">${ref.event_types.map(t => `<option>${t}</option>`).join("")}</select>
    <label>Date it occurred</label><input id="e-date" type="date" value="${new Date().toISOString().slice(0, 10)}">
    <label>Subcontract</label><select id="e-sub"><option value="">(project-wide)</option>${live.map(s => `<option value="${s.id}" ${s.id === S.vsub ? "selected" : ""}>${esc(s.subcontract_number)}</option>`).join("")}</select>
    <label>Location</label><input id="e-loc" placeholder="e.g. Pipe rack PR-3">
    <label>What happened</label><textarea id="e-desc" rows="3"></textarea></div>
    <button class="primary" id="e-go">Record event</button>`);
  $("#e-go").onclick = async () => { try { const e = await api(`/api/v1/projects/${S.project}/events`, { method: "POST", json: { event_type: $("#e-type").value, occurred_on: $("#e-date").value, subcontract_id: $("#e-sub").value || null, location: $("#e-loc").value, description: $("#e-desc").value } });
    toast(`${e.number} recorded`); closeModal(); } catch (er) {} };
}
async function raiseVariationModal(live, ref) {
  const events = await api(`/api/v1/projects/${S.project}/events`);
  openModal(`<h2>Raise a variation</h2><div class="form">
    <label>Subcontract</label><select id="v-sub">${live.map(s => `<option value="${s.id}" ${s.id === S.vsub ? "selected" : ""}>${esc(s.subcontract_number)} — ${esc(s.scope_summary)}</option>`).join("")}</select>
    <label>Title</label><input id="v-title" placeholder="e.g. Additional pipe supports on rack PR-2">
    <label>Origin event</label><select id="v-ev"><option value="">(none)</option>${events.map(e => `<option value="${e.id}" data-d="${e.occurred_on}">${esc(e.number)} · ${esc(e.type)} · ${esc(e.occurred_on)} — ${esc(e.description.slice(0, 60))}</option>`).join("")}</select>
    <label>Event date (starts notice clock)</label><input id="v-date" type="date">
    <label>Initiated by</label><select id="v-by"><option>EPC</option><option>SUBCONTRACTOR</option><option>EMPLOYER</option></select>
    <label>Category</label><select id="v-cat"><option value="">(classify later)</option>${ref.categories.map(c => `<option>${c}</option>`).join("")}</select>
    <label>Instruction / reference</label><input id="v-ins" placeholder="SI-017, RFI, drawing revision…">
    <label>Description</label><textarea id="v-desc" rows="3"></textarea>
    <label>Claimed amount</label><input id="v-amt" inputmode="decimal" placeholder="as claimed by the subcontractor (optional)">
    <label>Claimed days</label><input id="v-days" type="number" min="0"></div>
    <p class="small muted">The notice period is taken from this subcontract's executed notice clause.</p>
    <button class="primary" id="v-go">Raise</button>`);
  $("#v-ev").onchange = (e) => { const d = e.target.selectedOptions[0].dataset.d; if (d) $("#v-date").value = d; };
  $("#v-go").onclick = async () => { try {
    const v = await api(`/api/v1/subcontracts/${$("#v-sub").value}/variations`, { method: "POST", json: { title: $("#v-title").value, origin_event_id: $("#v-ev").value || null,
      event_date: $("#v-date").value || null, initiated_by: $("#v-by").value, category: $("#v-cat").value || null, instruction_reference: $("#v-ins").value,
      description: $("#v-desc").value, claimed_amount: $("#v-amt").value || null, claimed_days: $("#v-days").value ? Number($("#v-days").value) : null } });
    toast(`${v.number} raised — notice ${v.notice.message}`); closeModal(); S.vo = v.id; render(); } catch (e) {} };
}

let bindVariation = () => {};
async function variationDetail(vid, ref) {
  const v = await api(`/api/v1/variations/${vid}`);
  const A = new Set(v.actions), n = v.notice;
  const flow = v.status === "REJECTED" || v.status === "WITHDRAWN" ? [...VAR_FLOW.slice(0, VAR_FLOW.indexOf("APPROVAL_PENDING") + 1), v.status] : VAR_FLOW;
  const at = flow.indexOf(v.status);
  const stepper = `<div class="stepper">${flow.map((s, i) => `<span class="${i < at ? "done" : i === at ? "now" : ""} ${["REJECTED","WITHDRAWN"].includes(s) ? "stop" : ""}">${VAR_LABEL[s]}</span>`).join("")}</div>`;
  const moves = [...A].filter(a => a.startsWith("to:")).map(a => a.slice(3));
  const fwd = moves.filter(t => VAR_FLOW.indexOf(t) > VAR_FLOW.indexOf(v.status) || t === "CLOSED");
  const back = moves.filter(t => !fwd.includes(t) && !["WITHDRAWN", "REJECTED"].includes(t));
  const docs = A.has("evidence") || A.has("edit") ? await api(`/api/v1/projects/${S.project}/documents`) : [];
  const revs = docs.flatMap(d => d.revisions.map(r => ({ id: r.id, label: `${d.document_number} rev ${r.revision} — ${d.title}` })));
  const revSel = (id) => `<select id="${id}">${revs.map(r => `<option value="${r.id}">${esc(r.label)}</option>`).join("")}</select>`;
  const money3 = [["Claimed", v.claimed_amount, v.claimed_days], ["Assessed", v.assessed_amount, v.assessed_days],
                  ["Recommended", v.recommended_amount, v.recommended_days], ["Approved", v.approved_amount, v.approved_days]];
  bindVariation = () => {
    const post = (path, json, msg) => async () => { try { await api(`/api/v1/variations/${vid}/${path}`, { method: "POST", json: json() }); toast(msg); render(); } catch (e) {} };
    document.querySelectorAll("[data-vto]").forEach(b => b.onclick = async () => {
      const t = b.dataset.vto; let reason = null;
      if (["WITHDRAWN", "REJECTED"].includes(t)) { reason = prompt(`Reason for ${t.toLowerCase()}`); if (!reason) return; }
      try { await api(`/api/v1/variations/${vid}/transitions`, { method: "POST", json: { target: t, reason } }); toast(`Moved to ${VAR_LABEL[t]}`); render(); } catch (e) {} });
    $("#vf-save") && ($("#vf-save").onclick = async () => { try { await api(`/api/v1/variations/${vid}`, { method: "PATCH", json: Object.fromEntries(
      [["category", $("#vf-cat").value || null], ["description", $("#vf-desc").value], ["claimed_amount", $("#vf-amt").value || null],
       ["claimed_days", $("#vf-days").value ? Number($("#vf-days").value) : null]].filter(([, x]) => x !== undefined)) }); toast("Saved"); render(); } catch (e) {} });
    $("#vn-save") && ($("#vn-save").onclick = async () => { try { await api(`/api/v1/variations/${vid}`, { method: "PATCH", json: { notice_given_on: $("#vn-date").value || null, notice_revision_id: $("#vn-doc").value || null } }); toast("Notice recorded"); render(); } catch (e) {} });
    $("#vr-go") && ($("#vr-go").onclick = post("notice-risk", () => ({ decision: $("#vr-dec").value, reason: $("#vr-why").value }), "Notice decision recorded"));
    $("#ve-go") && ($("#ve-go").onclick = post("entitlement", () => ({ status: $("#ve-st").value, basis: $("#ve-basis").value, clause_references: $("#ve-cl").value.split(",").map(x => x.trim()).filter(Boolean) }), "Entitlement recorded"));
    $("#vi-add") && ($("#vi-add").onclick = () => { $("#vi-body").insertAdjacentHTML("beforeend", itemRow({}, ref)); });
    $("#vi-save") && ($("#vi-save").onclick = async () => { const items = [...document.querySelectorAll("#vi-body tr")].map(r => Object.fromEntries([...r.querySelectorAll("[data-f]")].map(i => [i.dataset.f, i.value])))
      .filter(i => i.description); try { await api(`/api/v1/variations/${vid}/valuation`, { method: "PUT", json: { items, assessed_days: $("#vi-days").value ? Number($("#vi-days").value) : null } }); toast("Valuation saved"); render(); } catch (e) {} });
    $("#vc-go") && ($("#vc-go").onclick = post("recommend", () => ({ amount: $("#vc-amt").value || null, days: $("#vc-days").value ? Number($("#vc-days").value) : null, comment: $("#vc-why").value }), "Recommended for approval"));
    document.querySelectorAll("[data-vdec]").forEach(b => b.onclick = post("decision", () => ({ decision: b.dataset.vdec, amount: $("#vd-amt").value || null, days: $("#vd-days").value ? Number($("#vd-days").value) : null, reason: $("#vd-why").value }), `Decision recorded: ${b.dataset.vdec.toLowerCase()}`));
    $("#vx-go") && ($("#vx-go").onclick = async () => { if (!confirm("Incorporate this approved variation? An amendment will change the subcontract value; the executed baseline stays as it is.")) return;
      try { const r = await api(`/api/v1/variations/${vid}/incorporate`, { method: "POST" }); toast(r.amendment ? `Amendment ${r.amendment.number}: ${inr(r.amendment.old_value)} → ${inr(r.amendment.new_value)}` : "Incorporated (no value change)"); render(); } catch (e) {} });
    $("#vv-go") && ($("#vv-go").onclick = post("evidence", () => ({ document_revision_id: $("#vv-doc").value, kind: $("#vv-kind").value, note: $("#vv-note").value }), "Evidence linked"));
    $("#vt-go") && ($("#vt-go").onclick = async () => { const t = await api(`/api/v1/variations/${vid}/trace`);
      openModal(`<h2>Chain of evidence · ${esc(v.number)}</h2><div class="chain">${t.edges.map(e => { const f = t.nodes.find(x => x.id === e.from), to = t.nodes.find(x => x.id === e.to);
        return `<div class="link"><div class="k">${esc(e.type.replaceAll("_", " "))}</div>${esc(f.kind)}: ${esc(f.label)} → ${esc(to.kind)}: ${esc(to.label)}</div>`; }).join("")}</div>`); });
  };
  const sec = (title, body) => `<div class="vsec"><h3>${title}</h3>${body}</div>`;
  return `<div class="card"><div class="row"><h2>${esc(v.number)} · ${esc(v.title)}</h2><span class="spacer"></span>${pill(VAR_LABEL[v.status], varCls(v.status))}</div>
    <p class="small muted">${esc(v.subcontract_number)} · ${esc((v.category || "unclassified").replaceAll("_", " ").toLowerCase())} · initiated by ${esc(v.initiated_by.toLowerCase())}${v.instruction_reference ? " · " + esc(v.instruction_reference) : ""} · raised by ${esc(v.created_by)}</p>
    ${stepper}
    <p>${esc(v.description)}</p>
    ${v.origin_event ? `<p class="small">Origin event <b class="mono">${esc(v.origin_event.number)}</b> · ${esc(v.origin_event.type)} · ${esc(v.origin_event.occurred_on)}${v.origin_event.location ? " · " + esc(v.origin_event.location) : ""} — ${esc(v.origin_event.description)}</p>` : ""}
    <div class="row">${fwd.map(t => `<button class="primary" data-vto="${t}">→ ${VAR_LABEL[t]}</button>`).join("")}${back.map(t => `<button data-vto="${t}">← ${VAR_LABEL[t]}</button>`).join("")}
      ${moves.includes("REJECTED") ? `<button class="danger" data-vto="REJECTED">Reject</button>` : ""}${moves.includes("WITHDRAWN") ? `<button class="danger" data-vto="WITHDRAWN">Withdraw</button>` : ""}
      <span class="spacer"></span><button id="vt-go">Chain of evidence</button></div>
    ${A.has("edit") && ["IDENTIFIED", "CLASSIFIED"].includes(v.status) ? sec("Classify", `<div class="form"><label>Category</label><select id="vf-cat"><option value="">(choose)</option>${ref.categories.map(c => `<option ${c === v.category ? "selected" : ""}>${c}</option>`).join("")}</select>
      <label>Description</label><textarea id="vf-desc" rows="2">${esc(v.description)}</textarea>
      <label>Claimed amount</label><input id="vf-amt" value="${esc(v.claimed_amount || "")}"><label>Claimed days</label><input id="vf-days" type="number" value="${v.claimed_days ?? ""}"></div><button id="vf-save">Save</button>`) : ""}
    ${sec("Notice", `<div class="notice ${n.at_risk ? "risk" : ""}">${noticePill(n)} <span>${esc(n.message)}</span></div>
      <p class="small muted">${v.notice_obligation ? `Clock from <b>${esc(v.notice_obligation.number)}</b> (${n.period_days} ${esc(n.day_basis.toLowerCase())} days): “${esc(v.notice_obligation.source_wording.slice(0, 220))}${v.notice_obligation.source_wording.length > 220 ? "…" : ""}”` : "No notice obligation was found on this subcontract."}</p>
      ${n.risk_decision ? `<p class="small">Decision: <b>${esc(n.risk_decision.decision.replaceAll("_", " ").toLowerCase())}</b> by ${esc(n.risk_decision.by)} — ${esc(n.risk_decision.reason)}</p>` : ""}
      ${A.has("edit") && ["IDENTIFIED", "CLASSIFIED", "NOTICE_REVIEW"].includes(v.status) ? `<div class="row"><label class="small">Notice given on <input id="vn-date" type="date" value="${esc(n.given_on || "")}"></label><label class="small">Notice document ${revSel("vn-doc")}</label><button id="vn-save">Record notice</button></div>` : ""}
      ${A.has("notice_risk") && n.at_risk ? `<div class="form" style="margin-top:8px"><label>Decision</label><select id="vr-dec"><option value="PROCEED_DESPITE_NOTICE_RISK">Proceed despite notice risk</option><option value="NOTICE_NOT_REQUIRED">Notice not required</option></select>
        <label>Reason (recorded)</label><textarea id="vr-why" rows="2" placeholder="Why proceeding is acceptable; this does not waive any right"></textarea></div><button class="primary" id="vr-go">Record decision</button>` : ""}
      ${A.has("notice_risk") && n.status === "PENDING" ? `<p class="small muted">Not expired yet. If no notice applies (e.g. an EPC-instructed omission) record that: <select id="vr-dec"><option value="NOTICE_NOT_REQUIRED">Notice not required</option></select> <input id="vr-why" placeholder="reason"> <button id="vr-go">Record</button></p>` : ""}`)}
    ${sec("Entitlement", `<p>${pill(v.entitlement_status.replaceAll("_", " "), { ENTITLED: "ok", PARTIALLY_ENTITLED: "warn", NOT_ENTITLED: "bad", DISPUTED: "bad" }[v.entitlement_status] || "")}
      ${v.entitlement_clauses.length ? ` clauses <span class="mono">${esc(v.entitlement_clauses.join(", "))}</span>` : ""}</p>${v.entitlement_basis ? `<p class="small">${esc(v.entitlement_basis)}</p>` : ""}
      ${A.has("entitlement") ? `<div class="form"><label>Status</label><select id="ve-st">${ref.entitlement.slice(1).map(e => `<option>${e}</option>`).join("")}</select>
        <label>Clauses (comma-separated)</label><input id="ve-cl" placeholder="e.g. 13.1" value="${esc(v.entitlement_clauses.join(", "))}">
        <label>Basis</label><textarea id="ve-basis" rows="2">${esc(v.entitlement_basis)}</textarea></div><button class="primary" id="ve-go">Record entitlement</button>` : ""}`)}
    ${sec("Valuation", `${A.has("valuation") ? `<table class="items"><thead><tr><th>Description</th><th>Cost type</th><th>Basis</th><th>Qty</th><th>Unit</th><th>Rate</th><th>Rate ref.</th></tr></thead><tbody id="vi-body">${(v.items.length ? v.items : [{}]).map(i => itemRow(i, ref)).join("")}</tbody></table>
        <div class="row"><button id="vi-add">+ Line</button><label class="small">Assessed days <input id="vi-days" type="number" min="0" style="width:80px" value="${v.assessed_days ?? ""}"></label><span class="spacer"></span><button class="primary" id="vi-save">Save valuation</button></div>
        <p class="small muted">Amounts are calculated by the system (quantity × rate, rounded to the paisa). Negative quantities only for omissions.</p>`
      : v.items.length ? `<table><tr><th>#</th><th>Description</th><th>Basis</th><th class="r">Qty</th><th>Unit</th><th class="r">Rate</th><th class="r">Amount</th></tr>${v.items.map(i => `<tr><td class="mono">${i.line_no}</td><td>${esc(i.description)}<div class="small muted">${esc(i.cost_type.toLowerCase())}${i.rate_reference ? " · " + esc(i.rate_reference) : ""}</div></td><td class="small">${esc(i.pricing_basis.replaceAll("_", " ").toLowerCase())}</td><td class="mono r">${esc(i.quantity)}</td><td>${esc(i.unit)}</td><td class="mono r">${inr(i.rate)}</td><td class="mono r">${inr(i.amount)}</td></tr>`).join("")}</table>` : `<p class="small muted">Not priced yet.</p>`}
      <table class="ladder2"><tr><th></th>${money3.map(([l]) => `<th class="r">${l}</th>`).join("")}</tr><tr><td class="small muted">Amount (${esc(v.currency)})</td>${money3.map(([, a]) => `<td class="mono r">${inr(a)}</td>`).join("")}</tr><tr><td class="small muted">Days</td>${money3.map(([, , dd]) => `<td class="mono r">${dd ?? "—"}</td>`).join("")}</tr></table>
      ${A.has("recommend") ? `<div class="form"><label>Recommend amount</label><input id="vc-amt" placeholder="${esc(v.assessed_amount || "")}"><label>Days</label><input id="vc-days" type="number" placeholder="${v.assessed_days ?? ""}"><label>Comment</label><textarea id="vc-why" rows="2" placeholder="required if different from the assessment"></textarea></div><button class="primary" id="vc-go">Recommend for approval</button>` : ""}
      ${A.has("decide") ? `<div class="form"><label>Approve amount</label><input id="vd-amt" placeholder="${esc(v.recommended_amount || "")}"><label>Days</label><input id="vd-days" type="number" placeholder="${v.recommended_days ?? ""}"><label>Reason</label><textarea id="vd-why" rows="2" placeholder="required to reject, return, or approve a different value"></textarea></div>
        <div class="row"><button class="primary" data-vdec="APPROVE">Approve</button><button data-vdec="RETURN">Return to commercial</button><button class="danger" data-vdec="REJECT">Reject</button></div>` : ""}
      ${v.decided_by ? `<p class="small">Decided by <b>${esc(v.decided_by)}</b> on ${esc((v.decided_at || "").slice(0, 10))}${v.decision_reason ? ` — ${esc(v.decision_reason)}` : ""}</p>` : ""}
      ${A.has("incorporate") ? `<button class="primary" id="vx-go">Incorporate by amendment</button>` : ""}`)}
    ${sec("Evidence", `${v.evidence.length ? `<table>${v.evidence.map(e => `<tr><td>${pill(e.kind)}</td><td class="mono small">${esc(e.document_number)} rev ${esc(e.revision)}</td><td class="small">${esc(e.title)}${e.note ? ` — ${esc(e.note)}` : ""}</td></tr>`).join("")}</table>` : `<p class="small muted">None linked.</p>`}
      ${A.has("evidence") ? `<div class="row">${revSel("vv-doc")}<select id="vv-kind">${ref.evidence_kinds.map(x => `<option>${x}</option>`).join("")}</select><input id="vv-note" placeholder="note"><button id="vv-go">Link</button></div>` : ""}`)}
    ${sec("History", `<table>${v.history.map(h => `<tr><td class="mono small" style="white-space:nowrap">${esc(h.at.replace("T", " ").slice(0, 16))}</td><td class="small">${esc(h.actor)}</td><td class="mono small">${esc(h.action.replace("variation.", ""))}${h.after?.status ? " → " + esc(h.after.status) : ""}</td><td class="small muted">${esc(h.reason || "")}</td></tr>`).join("")}</table>`)}
  </div>`;
}
function itemRow(i, ref) {
  const sel = (f, opts, cur) => `<select data-f="${f}">${opts.map(o => `<option ${o === cur ? "selected" : ""}>${o}</option>`).join("")}</select>`;
  return `<tr><td><input data-f="description" value="${esc(i.description || "")}"></td><td>${sel("cost_type", ref.cost_types, i.cost_type || "SUBCONTRACT")}</td><td>${sel("pricing_basis", ref.pricing_bases, i.pricing_basis || "CONTRACT_RATE")}</td>
    <td><input data-f="quantity" style="width:80px" value="${esc(i.quantity || "")}"></td><td><input data-f="unit" style="width:60px" value="${esc(i.unit || "")}"></td><td><input data-f="rate" style="width:110px" value="${esc(i.rate || "")}"></td><td><input data-f="rate_reference" style="width:110px" value="${esc(i.rate_reference || "")}"></td></tr>`;
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
    $("#kgo").onclick = ask; $("#kq").onkeydown = (e) => { if (e.key === "Enter") ask(); };
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
  ${d.users.map(u => `<tr><td><b>${esc(u.username)}</b><div class="small muted">${esc(u.full_name)}</div></td><td class="small">${esc(u.email || "—")} <a href="#" data-email="${esc(u.username)}" data-cur="${esc(u.email || "")}">edit</a></td><td class="small">${esc(u.roles.join(", ") || "—")}${(u.directory_roles || []).map(r => ` <span class="pill info" title="From a directory group">${esc(r)} · directory</span>`).join("")}</td><td>${esc(u.clearance)}</td>
    <td>${u.is_active ? pill("ACTIVE", "ok") : pill("DISABLED", "bad")} ${u.locked ? pill("LOCKED", "bad") : ""} ${u.must_change_password ? pill("TEMP PASSWORD", "warn") : ""} ${u.mfa_enabled ? pill("2-STEP", "ok") : ""}</td>
    <td class="mono small">${esc((u.last_login_at || "never").replace("T", " ").slice(0, 16))}</td>
    <td class="row" style="flex-wrap:nowrap"><button data-reset="${esc(u.username)}">Reset password</button>${u.mfa_enabled && u.username !== S.me.username ? `<button data-mfareset="${esc(u.username)}" title="For a lost phone">Reset 2-step</button>` : ""}${u.locked ? `<button data-unlock="${esc(u.username)}">Unlock</button>` : ""}
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
    document.querySelectorAll("[data-mfareset]").forEach(b => b.onclick = async () => { if (!confirm(`Remove ${b.dataset.mfareset}'s authenticator app? Use this only after confirming who is asking (for example a lost phone). They will be signed out everywhere.`)) return;
      try { await api(`/api/v1/users/${b.dataset.mfareset}/reset-mfa`, { method: "POST" }); toast("Authenticator removed"); render(); } catch (e) {} });
    document.querySelectorAll("[data-unlock]").forEach(b => b.onclick = async () => { try { await api(`/api/v1/users/${b.dataset.unlock}`, { method: "PATCH", json: { unlock: true } }); toast("Unlocked"); render(); } catch (e) {} });
    document.querySelectorAll("[data-active]").forEach(b => b.onclick = async () => { try { await api(`/api/v1/users/${b.dataset.active}`, { method: "PATCH", json: { is_active: b.dataset.to === "true" } }); render(); } catch (e) {} });
  })}`;
};

function idpCard(idp) {
  return `<div class="card"><div class="row"><h2>Sign-in providers</h2><span class="spacer"></span>
    <label class="small">Password sign-in <select id="pw-policy"><option value="all" ${idp.password_login === "all" ? "selected" : ""}>Everyone</option>
    <option value="admins_only" ${idp.password_login === "admins_only" ? "selected" : ""}>Administrators only</option></select></label>
    <label class="small">Authenticator code with password <select id="mfa-policy">${[["off", "Optional"], ["admins", "Required for administrators"], ["all", "Required for everyone"]]
      .map(([v, l]) => `<option value="${v}" ${idp.mfa_required === v ? "selected" : ""}>${l}</option>`).join("")}</select></label></div>
    <p class="small muted">Any OpenID Connect provider. Accounts and roles stay here; a provider only proves identity. Invite people by setting their work e-mail above — they are linked at first company sign-in. Redirect URI to register with the provider: <span class="mono">${esc(idp.redirect_uri)}</span></p>
    ${idp.providers.length ? `<table><tr><th>Name</th><th>Provider</th><th>Issuer</th><th>Client</th><th>Domains</th><th>New users</th><th>Linked</th><th></th></tr>
      ${idp.providers.map(p => `<tr><td><b>${esc(p.name)}</b>${p.enabled ? "" : " " + pill("DISABLED")}<div class="small muted">${esc(p.button_label)}</div></td><td class="small">${esc(p.provider_label)}</td>
        <td class="mono small">${esc(p.issuer)}</td><td class="small"><span class="mono">${esc(p.client_id)}</span><div class="muted">${esc(p.secret_source)}</div></td>
        <td class="small">${esc(p.allowed_domains.join(", ") || "any")}</td><td class="small">${p.auto_provision ? "auto-create (no roles)" : "invite only"}</td><td class="mono">${p.linked_accounts}</td>
        <td class="row" style="flex-wrap:nowrap"><button data-idgroups="${p.id}">Group roles${p.group_mappings ? ` (${p.group_mappings})` : ""}${p.sync_roles ? " ✓" : ""}</button><button data-idtest="${p.id}">Test</button><button data-idtoggle="${p.id}" data-en="${p.enabled}">${p.enabled ? "Disable" : "Enable"}</button><button class="danger" data-iddel="${p.id}">Remove</button></td></tr>`).join("")}</table>`
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
async function groupRolesModal(pid) {
  const d = await api(`/api/v1/auth/identity-providers/${pid}/group-mappings`);
  openModal(`<h2>Directory group → role mapping</h2><p class="small muted">${esc(d.provider)} · People in a mapped group get these roles at each company sign-in; leaving the group removes them at the next sign-in. Manually granted roles are never changed. <b>system_admin</b> can never come from a directory.</p>
    <div class="form">
      <label>Sync roles from groups</label><select id="gm-sync"><option value="0" ${d.sync_roles ? "" : "selected"}>Off</option><option value="1" ${d.sync_roles ? "selected" : ""}>On</option></select>
      <label>Groups claim in the token</label><input id="gm-claim" value="${esc(d.groups_claim)}" placeholder="groups  (Keycloak: realm_access.roles)">
      <label>Directory roles expire if no sign-in for</label><div class="row"><input id="gm-ttl" type="number" min="0" max="365" value="${d.grant_ttl_days}" style="width:90px"><span class="small muted">days (0 = never)</span></div>
    </div><button id="gm-save">Save settings</button>
    <h3>Mappings</h3>${d.mappings.length ? `<table><tr><th>Directory group</th><th>Scope</th><th>Roles</th><th>Users</th><th></th></tr>
      ${d.mappings.map(m => `<tr><td><b>${esc(m.label || m.group_value)}</b>${m.label && m.label !== m.group_value ? `<div class="mono small muted">${esc(m.group_value)}</div>` : ""}</td>
        <td>${m.project_code ? esc(m.project_code) : pill("GLOBAL", "warn")}</td><td class="small">${esc(m.roles.join(", "))}</td><td class="mono">${m.users_holding}</td>
        <td><button class="danger" data-gmdel="${m.id}">Remove</button></td></tr>`).join("")}</table>` : `<p class="muted small">No mappings yet.</p>`}
    <h3>Add a mapping</h3><div class="form">
      <label>Group as sent by the provider</label><input id="gm-group" placeholder="name, path or object ID (Entra ID sends object IDs)">
      <label>Description</label><input id="gm-label" placeholder="e.g. EPC Contracts team">
      <label>Scope</label><select id="gm-scope"><option value="">Global — all projects</option>${d.projects.map(x => `<option value="${x.id}">Project ${esc(x.code)} — ${esc(x.name)}</option>`).join("")}</select>
      <label>Roles</label><select id="gm-roles" multiple size="6">${d.assignable_roles.map(r => `<option>${r}</option>`).join("")}</select>
    </div><button class="primary" id="gm-add">Add mapping</button>
    <h3>Dry run</h3><div class="row"><input id="gm-try" style="flex:1" placeholder="groups a person is in, comma-separated"><button id="gm-preview">Show roles</button></div><div id="gm-out" class="small" style="margin-top:8px"></div>`);
  $("#gm-save").onclick = async () => { try { await api(`/api/v1/auth/identity-providers/${pid}`, { method: "PATCH", json: { sync_roles: $("#gm-sync").value === "1",
      groups_claim: $("#gm-claim").value.trim() || "groups", grant_ttl_days: Number($("#gm-ttl").value) } }); toast("Group sync settings saved"); groupRolesModal(pid); } catch (e) {} };
  $("#gm-add").onclick = async () => { try { await api(`/api/v1/auth/identity-providers/${pid}/group-mappings`, { method: "POST", json: { group_value: $("#gm-group").value,
      label: $("#gm-label").value, project_id: $("#gm-scope").value || null, roles: [...$("#gm-roles").selectedOptions].map(o => o.value) } }); toast("Mapping added"); groupRolesModal(pid); } catch (e) {} };
  document.querySelectorAll("[data-gmdel]").forEach(b => b.onclick = async () => { if (!confirm("Remove this mapping? Roles it granted are withdrawn immediately.")) return;
    try { const r = await api(`/api/v1/auth/group-mappings/${b.dataset.gmdel}`, { method: "DELETE" }); toast(`Removed; ${r.grants_withdrawn} role grant(s) withdrawn`); groupRolesModal(pid); } catch (e) {} });
  $("#gm-preview").onclick = async () => { const r = await api(`/api/v1/auth/identity-providers/${pid}/group-mappings/preview`, { method: "POST",
      json: { groups: $("#gm-try").value.split(",").map(x => x.trim()).filter(Boolean) } });
    $("#gm-out").innerHTML = r.result.length ? r.result.map(x => `${pill(x.role, "ok")} ${x.scope === "global" ? "global" : "on " + esc(x.scope)} <span class="muted">via ${esc(x.group)}</span>`).join("<br>") : `<span class="muted">No roles — no matching mapping.</span>`; };
}
function bindIdp(idp) {
  document.querySelectorAll("[data-idgroups]").forEach(b => b.onclick = () => groupRolesModal(b.dataset.idgroups));
  const fill = () => { const c = idp.catalogue.find(x => x.key === $("#ip-type").value); $("#ip-iss").value = c.issuer_template; $("#ip-btn").value = c.button_label; $("#ip-note").textContent = c.note; };
  $("#ip-type").onchange = fill; fill();
  $("#mfa-policy").onchange = async (e) => { try { await api("/api/v1/auth/policy", { method: "PUT", json: { mfa_required: e.target.value } }); toast("Sign-in policy saved"); if (!S.me.mfa_enabled && e.target.value !== "off") boot(); } catch (er) { render(); } };
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
