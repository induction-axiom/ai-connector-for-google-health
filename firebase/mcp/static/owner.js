import {initializeApp} from "https://www.gstatic.com/firebasejs/12.3.0/firebase-app.js";
import {getAuth, GoogleAuthProvider, signInWithPopup, browserSessionPersistence,
  setPersistence, onAuthStateChanged, signOut}
  from "https://www.gstatic.com/firebasejs/12.3.0/firebase-auth.js";

const el = id => document.getElementById(id);
const VIEWS = ["overview", "developer"];
let auth, user, status, busy = false;

// ---------- Small helpers ----------

class OwnerSessionExpired extends Error {}

async function api(path, body = {}) {
  const response = await fetch(path, {method: "POST", credentials: "same-origin",
    headers: {"Authorization": "Bearer " + await user.getIdToken(),
      "Content-Type": "application/json"}, body: JSON.stringify(body)});
  const data = await response.json().catch(() => ({}));
  if (response.status === 403 && data.error === "owner_login_required") throw new OwnerSessionExpired();
  if (!response.ok && !data.result) throw new Error(data.error || "request_failed");
  return data;
}

function ago(value) {
  if (!value) return null;
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  if (Number.isNaN(date.getTime())) return null;
  const seconds = (date.getTime() - Date.now()) / 1000;
  const [size, unit] = [[86400, "day"], [3600, "hour"], [60, "minute"], [1, "second"]]
    .find(([size]) => Math.abs(seconds) >= size) || [1, "second"];
  return new Intl.RelativeTimeFormat(undefined, {numeric: "auto"}).format(Math.round(seconds / size), unit);
}

function monthDay(value) {
  const date = typeof value === "number" ? new Date(value * 1000) : null;
  if (!date || Number.isNaN(date.getTime())) return null;
  const year = date.getFullYear() === new Date().getFullYear() ? undefined : "numeric";
  return new Intl.DateTimeFormat(undefined, {month: "short", day: "numeric", year}).format(date);
}

function say(id, text, tone = "neutral") {
  el(id).textContent = text;
  el(id).dataset.tone = tone;
}

// ---------- Plain-language copy for result codes ----------

// A Google failure older than this is no longer worth showing.
const PROBLEM_DAYS = 7;

// What each connection state means for the owner.
const GOOGLE_COPY = {
  connected: "Answering",
  not_connected: "Not connected yet",
  google_health_disconnected: "Google no longer accepts the saved access",
  client_invalid: "Google doesn't accept the saved OAuth client",
  permission_denied: "Google Health refused access",
  google_unavailable: "Google isn't answering",
  google_rejected: "Google rejected the request",
  rate_limited: "Too many requests to Google",
};

// What comes back from Google's consent page, and from saving the client.
const CONNECT_COPY = {
  connected: ["Connected. Your AI apps can read your Google Health data now.", "success"],
  connected_partly: ["Connected, but not to everything: some data wasn't shared. Connect again and allow all of it.", "warning"],
  denied: ["Nothing was connected: access wasn't allowed on Google's page.", "warning"],
  expired: ["That connection attempt expired. Start again from this page.", "warning"],
  no_refresh_token: ["Google didn't give lasting access. Remove this connector's access at myaccount.google.com/connections, then connect again.", "warning"],
  google_rejected: ["Google didn't accept the connection. Try again.", "danger"],
  client_invalid: ["Google didn't accept this project's OAuth client. Run the setup again.", "danger"],
  google_unavailable: ["Google isn't answering right now. Nothing was saved; try again later.", "warning"],
  disconnected: ["Disconnected. Google no longer gives this connector access; your data in Google Health is unchanged.", "success"],
  no_client: ["The setup hasn't finished: run it again.", "warning"],
};

// ---------- Views and routing ----------

function currentView() {
  const name = location.hash.slice(1);
  return VIEWS.includes(name) ? name : "overview";
}

function showView() {
  const name = currentView();
  for (const view of VIEWS) el(view + "-view").classList.toggle("hidden", view !== name);
  for (const tab of el("tabs").querySelectorAll("a"))
    tab.setAttribute("aria-current", tab.dataset.view === name ? "page" : "false");
  if (name === "developer" && user) preview(previewTarget);
  window.scrollTo({top: 0});
}

function showSignedIn(signedIn) {
  el("signin-view").classList.toggle("hidden", signedIn);
  el("tabs").classList.toggle("hidden", !signedIn);
  el("account").classList.toggle("hidden", !signedIn);
  if (signedIn) showView();
  else for (const view of VIEWS) el(view + "-view").classList.add("hidden");
}

// ---------- Overview ----------

// "Gemini", "Gemini and Claude", "3 apps"
function appNames(apps) {
  const names = [...new Set(apps.map(appName))];
  return names.length > 2 ? names.length + " apps" : names.join(" and ");
}

// The page answers one question first: can my AI read my health data?
function overallState(google, apps) {
  if (google.state === "no_client") return {
    tone: "warning", label: "Setup", title: "Finish the setup.", action: "update",
    summary: "This project's Google client isn't saved yet. Run the setup again: it picks up where it stopped.",
  };
  if (google.state === "not_connected") return {
    tone: "neutral", label: "Setup", title: "Connect Google Health.", action: "connect",
    summary: "Allow this connector to read your Google Health data. Google asks you on its own page.",
  };
  if (google.state === "google_health_disconnected") return {
    tone: "danger", label: "Action required", title: "Connect Google Health again.", action: "connect",
    summary: GOOGLE_COPY[google.state] + ": it was revoked, unused for 6 months, or the account's password changed.",
  };
  if (google.state === "client_invalid") return {
    tone: "danger", label: "Action required", title: "Run the setup again.", action: "update",
    summary: GOOGLE_COPY[google.state] + ". The setup saves this project's current client again.",
  };
  if (google.state !== "connected") return {
    tone: "warning", label: "Needs attention", title: "Google Health isn't answering right now.", action: "check",
    summary: (GOOGLE_COPY[google.state] || "Something went wrong") + (google.detail ? `: ${google.detail}` : "") + ".",
  };
  if (!apps.length) return {
    tone: "neutral", label: "Almost ready", title: "Connect an AI app.", action: "guide",
    summary: "Google Health is connected. Add this connector to an AI app to start asking.",
  };
  const used = apps.map(app => app.last_used_at).filter(Boolean).sort().at(-1);
  return {
    tone: "success", label: "Ready", title: `${appNames(apps)} can read your Google Health data.`, action: "check",
    summary: (google.email || "Connected") + (used ? `. Last used ${ago(used)}.` : "."),
  };
}

function renderStatus() {
  const google = status.google || {};
  const failure = status.last_failure;
  const apps = status.apps || [];
  const state = overallState(google, apps);
  el("overview-hero").dataset.tone = state.tone;
  el("status-label").textContent = state.label;
  el("status-title").textContent = state.title;
  el("status-summary").textContent = state.summary;
  el("primary-action").disabled = busy;
  el("primary-action").dataset.action = state.action;
  el("primary-action").textContent = {guide: "Connect an AI app", update: "How to run the setup"}[state.action] || "Check again";
  // While not connected, the panel's Connect button is the action.
  el("primary-action").classList.toggle("hidden", state.action === "connect");
  renderGoogle(google, state);
  // Only problems are shown: Google not answering now, or a failure in the last week.
  const connected = google.state === "connected";
  const down = google.state && !connected && !["no_client", "not_connected"].includes(google.state);
  const recent = failure && Date.now() / 1000 - failure.at < PROBLEM_DAYS * 86400;
  el("fact-live").textContent = GOOGLE_COPY[google.state] || google.state;
  el("fact-live-row").classList.toggle("hidden", !down);
  el("fact-last-error").textContent = recent ? `${GOOGLE_COPY[failure.code] || failure.code}, ${ago(failure.at)}` : "";
  el("fact-last-error-row").classList.toggle("hidden", !recent);
  el("google-facts").classList.toggle("hidden", !down && !recent);
  el("apps-list").replaceChildren(...(apps.length ? apps.map(appRow) : [emptyRow("No apps are connected yet.")]));

  const diagnostics = status.diagnostics || {};
  el("mcp-url").textContent = diagnostics.mcp_endpoint || "—";
  // With several deployments, the project ID is how you tell this one apart.
  if (diagnostics.project_id) {
    el("project-id").textContent = diagnostics.project_id;
    el("project").classList.remove("hidden");
    document.title = diagnostics.project_id + " · AI connector for Google Health";
    renderResources(diagnostics);
  }
  el("fact-version").textContent = [diagnostics.connector_version, diagnostics.commit?.slice(0, 7),
    diagnostics.committed_on].filter(Boolean).join(" · ") || "—";
  if (diagnostics.repository) {
    const github = `https://github.com/${diagnostics.repository}`;
    el("source").href = diagnostics.commit ? `${github}/tree/${diagnostics.commit}` : github;
    el("source").classList.remove("hidden");
    el("fact-source").replaceChildren(external(github, diagnostics.repository + " ↗"));
  }
  checkForUpdate(diagnostics.connector_version, diagnostics.repository);
  el("fact-state").firstElementChild.textContent = google.state || "—";
}

async function loadStatus() {
  status = await api("/owner/status");
  renderStatus();
}

// One operation at a time: these controls stay disabled until it ends.
function setBusy(value) {
  busy = value;
  for (const id of ["primary-action", "google-connect"]) el(id).disabled = value;
}

el("primary-action").onclick = async () => {
  if (el("primary-action").dataset.action === "guide") return el("apps").scrollIntoView({behavior: "smooth"});
  if (el("primary-action").dataset.action === "update") return openGuide("setup");
  setBusy(true);
  say("message", "Checking Google Health…");
  try {
    await loadStatus();
    say("message", "");
  } catch (error) {
    if (error instanceof OwnerSessionExpired) return expireSession();
    say("message", "Your connector's status couldn't be loaded. Try again.", "danger");
  } finally {
    setBusy(false);
  }
};

// ---------- Google Health ----------

function textRow(title, detail, action) {
  const row = document.createElement("li");
  row.className = "row";
  const text = document.createElement("div");
  const strong = document.createElement("strong");
  strong.textContent = title;
  const span = document.createElement("span");
  span.textContent = detail;
  text.append(strong, span);
  row.append(text);
  if (action) row.append(action);
  return row;
}

// Two-step: the first click asks, the second one acts.
function confirmButton(label, confirm, act) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "link-button danger";
  button.textContent = label;
  button.onclick = async () => {
    if (!button.dataset.confirm) {
      button.dataset.confirm = "1";
      button.textContent = confirm;
      return;
    }
    button.disabled = true;
    try {
      await act();
    } catch (error) {
      if (error instanceof OwnerSessionExpired) return expireSession();
      button.textContent = "Try again";
      button.disabled = false;
    }
  };
  return button;
}

const SCOPE_NAMES = {sleep: "Sleep", health_metrics_and_measurements: "Health metrics (heart rate, HRV, SpO2…)",
  activity_and_fitness: "Activity and fitness", profile: "Profile", settings: "Settings and devices",
  ecg: "ECG", irn: "Irregular rhythm notifications", mindfulness: "Moods", logged_symptoms: "Symptoms",
  reproductive_health: "Reproductive health", nutrition: "Food and hydration"};
const scopeName = scope => SCOPE_NAMES[scope.split(".").at(-2)] || scope;

function renderGoogle(google, state) {
  const hasClient = google.state !== "no_client";
  const tokenSaved = !["no_client", "not_connected"].includes(google.state);
  const rows = [];
  if (!hasClient) rows.push(textRow("Setup not finished", "Run the setup again to save this project's Google client."));
  else if (!tokenSaved) rows.push(textRow("Not connected", "Your AI apps can't read Google Health yet."));
  else {
    const since = monthDay(google.connected_at);
    rows.push(textRow(google.email || "Google account", [GOOGLE_COPY[google.state] || google.state,
      since && "connected " + since].filter(Boolean).join(" · "),
      confirmButton("Disconnect", "Confirm disconnect", async () => {
        const {result} = await api("/owner/google/disconnect");
        await loadStatus();
        say("message", ...CONNECT_COPY[result]);
      })));
    if (google.missing_scopes?.length)
      rows.push(textRow("Not shared", google.missing_scopes.map(scopeName).join(", ") + ". Connect again and allow it."));
  }
  el("google-rows").replaceChildren(...rows);
  const reconnect = tokenSaved && (state.action === "connect" || google.missing_scopes?.length);
  el("connect-actions").classList.toggle("hidden", !(google.state === "not_connected" || reconnect));
  el("google-connect").textContent = reconnect ? "Connect again" : "Connect Google Health";
  el("connect-note").classList.toggle("hidden", google.state !== "not_connected");
}

// The connect page on Firebase Hosting asks Google, then sends the owner back here with ?google=<outcome>.
el("google-connect").onclick = async () => {
  setBusy(true);
  say("message", "Opening Google…");
  try {
    const reply = await api("/owner/google/start");
    if (reply.result === "redirect") return location.assign(reply.url);
    say("message", ...(CONNECT_COPY[reply.result] || CONNECT_COPY.google_unavailable));
  } catch (error) {
    if (error instanceof OwnerSessionExpired) return expireSession();
    say("message", "Google couldn't be opened. Try again.", "danger");
  }
  setBusy(false);
};

// The outcome Google's page came back with, shown once.
const returned = new URLSearchParams(location.search).get("google");
if (returned) history.replaceState(null, "", location.pathname + location.hash);

// ---------- AI apps ----------

function appName(app) {
  // Named by callback host, never by the name an app registers with: any app can register and call itself "Claude".
  const host = app.redirect_host;
  if (host === "oauth-redirect.googleusercontent.com") return "Gemini";
  if (host === "chatgpt.com") return "ChatGPT";
  if (host === "claude.ai" || host === "claude.com") return "Claude";
  if (host === "agent.meta.ai") return "Muse";
  return host || "Unknown app";
}

function emptyRow(text) {
  const row = document.createElement("li");
  row.className = "row muted";
  row.textContent = text;
  return row;
}

function appRow(app) {
  const row = document.createElement("li");
  row.className = "row";
  const name = document.createElement("div");
  const title = document.createElement("strong");
  title.textContent = appName(app);
  const detail = document.createElement("span");
  const connected = monthDay(app.connected_at);
  detail.textContent = [connected ? "Connected " + connected : "Connected",
    app.last_used_at && "Last used " + ago(app.last_used_at)].filter(Boolean).join(" · ");
  name.append(title, detail);
  const button = document.createElement("button");
  button.type = "button";
  button.className = "link-button danger";
  button.textContent = "Disconnect";
  // Two-step: the first click asks, the second one disconnects.
  button.onclick = async () => {
    if (!button.dataset.confirm) {
      button.dataset.confirm = "1";
      button.textContent = "Confirm disconnect";
      return;
    }
    button.disabled = true;
    try {
      await api("/owner/apps/disconnect", {client_id: app.client_id});
      await loadStatus();
    } catch (error) {
      if (error instanceof OwnerSessionExpired) return expireSession();
      button.textContent = "Try again";
      button.disabled = false;
    }
  };
  row.append(name, button);
  return row;
}

async function copyText(value, button) {
  try {
    await navigator.clipboard.writeText(value);
    button.textContent = "Copied";
  } catch {
    button.textContent = "Select and copy";
  }
  setTimeout(() => { button.textContent = "Copy"; }, 2000);
}
el("copy-url").onclick = event => copyText(el("mcp-url").textContent, event.currentTarget);

function copyField(value) {
  const field = document.createElement("div");
  field.className = "copy-field";
  const code = document.createElement("code");
  code.textContent = value;
  const button = document.createElement("button");
  button.type = "button";
  button.className = "pill small";
  button.textContent = "Copy";
  button.onclick = () => copyText(value, button);
  field.append(code, button);
  return field;
}

function external(href, text, className = "") {
  const link = document.createElement("a");
  link.href = href;
  link.target = "_blank";
  link.rel = "noopener";
  link.className = className;
  link.textContent = text;
  return link;
}

// Each app's own guide to adding a custom MCP server, checked October 2026. Their menus change
// often and these pages follow them, so the steps below say only what is particular to this connector.
const GUIDES = {
  chatgpt: {name: "ChatGPT", adds: "a custom MCP server", doc: "https://help.openai.com/en/articles/12584461"},
  claude: {name: "Claude", adds: "a custom connector", doc: "https://support.claude.com/en/articles/11175166"},
  gemini: {name: "Gemini", adds: "a custom app", doc: "https://support.google.com/gemini/answer/17209137"},
};
const SIGN_IN = "When a sign-in window opens, choose this Google account and allow access.";

function connectGuide(name) {
  // Muse adds a connector itself when given its address in a chat.
  if (name === "muse") return {steps: [
    "In a Muse chat, paste the address above and ask Muse to add it as a connector named Google Health.",
    SIGN_IN,
  ]};
  const app = GUIDES[name];
  return {steps: [
    app ? {text: ["Follow ", {href: app.doc, label: `${app.name}'s guide`}, ` to add ${app.adds}. Name it Google Health.`]}
      : "In your AI app, find where it adds a custom MCP server, connector or app. Name it Google Health.",
    "When it asks for the server URL, paste the address above. If it asks how to sign in, choose OAuth, and leave any client ID and secret empty: this connector registers the app itself.",
    SIGN_IN,
  ], note: app ? null : "The app must reach this address over the internet and sign in with OAuth. Once connected, it's listed above by its web address."};
}

// A step is text, optionally with a value to copy or a link to open. Its text may hold
// links within the sentence, as {href, label}.
function guides() {
  const d = status?.diagnostics || {};
  const setup = [
    {text: "Open Cloud Shell with the latest code. Sign in with the Google account that owns this project if asked.",
      // The stable branch always holds the latest release, never unreleased work on main.
      href: `https://shell.cloud.google.com/cloudshell/editor?cloudshell_git_repo=https://github.com/${d.repository}&cloudshell_git_branch=stable&show=terminal`,
      label: "Open Cloud Shell"},
    {text: "Paste this into the terminal and press Enter:",
      copy: `firebase/scripts/bootstrap.sh ${d.project_id} ${user?.email}`},
  ];
  return {
    setup: {title: "Run the setup again", steps: [...setup,
      "Type y when asked. It reuses everything already there, and finishes what's missing."]},
    update: {title: `Update to ${latestRelease?.version}`, steps: [...setup,
      "Type y when asked. It takes a few minutes. Your Google Health connection and AI app connections stay as they are.",
    ], note: "Updating only deploys new code into your own project. You can read every change first under What's new."},
  };
}

function guideBody(guide) {
  const list = document.createElement("ol");
  list.className = "steps";
  for (const step of guide.steps) {
    const {text, copy, href, label} = typeof step === "string" ? {text: step} : step;
    const item = document.createElement("li");
    for (const part of [text].flat())
      item.append(typeof part === "string" ? part : external(part.href, part.label, "inline-link"));
    if (copy) item.append(copyField(copy));
    if (href) item.append(external(href, label, "pill small step-link"));
    list.append(item);
  }
  if (!guide.note) return [list];
  const note = document.createElement("p");
  note.className = "panel-note";
  note.textContent = guide.note;
  return [list, note];
}

function openGuide(name) {
  const guide = guides()[name];
  el("guide-title").textContent = guide.title;
  el("guide-body").replaceChildren(...guideBody(guide));
  el("guide").showModal();
}

// Clicking the dimmed backdrop closes the sheet.
el("guide").onclick = event => { if (event.target === el("guide")) el("guide").close(); };

// Choosing an app shows its steps below the chips; choosing it again hides them.
function showConnectSteps(name) {
  const chosen = name && document.querySelector(`[data-connect="${name}"]`)?.getAttribute("aria-pressed") !== "true";
  for (const button of document.querySelectorAll("[data-connect]"))
    button.setAttribute("aria-pressed", String(chosen && button.dataset.connect === name));
  el("connect-steps").replaceChildren(...(chosen ? guideBody(connectGuide(name)) : []));
  el("connect-steps").classList.toggle("hidden", !chosen);
}

for (const button of document.querySelectorAll("[data-connect]"))
  button.onclick = () => showConnectSteps(button.dataset.connect);

// ---------- Updates ----------

let latestRelease = null;

function isNewer(candidate, current) {
  const [a, b] = [candidate, current].map(v => v.split(".").map(Number));
  for (let i = 0; i < 3; i++) if (a[i] !== b[i]) return a[i] > b[i];
  return false;
}

// Asks GitHub from your browser, once per page load; nothing is sent about you or your data.
async function checkForUpdate(current, repository) {
  if (latestRelease || !repository || !/^\d+\.\d+\.\d+$/.test(current || "")) return;
  try {
    const response = await fetch(`https://api.github.com/repos/${repository}/releases/latest`);
    const release = response.ok ? await response.json() : null;
    const version = release?.tag_name?.replace(/^v/, "");
    if (!version || !/^\d+\.\d+\.\d+$/.test(version) || !isNewer(version, current)) return;
    latestRelease = {version, url: release.html_url};
    el("update-text").textContent = `Version ${version} is available. You're on ${current}.`;
    el("update-notes").href = release.html_url;
    el("update").classList.remove("hidden");
  } catch {
    // Offline or rate-limited: simply no banner.
  }
}
el("update-how").onclick = () => openGuide("update");

// ---------- Your Firebase project ----------

function renderResources(d) {
  const project = encodeURIComponent(d.project_id);
  const firebase = `https://console.firebase.google.com/project/${project}`;
  const cloud = path => `https://console.cloud.google.com/${path}?project=${project}`;
  const database = id => `${firebase}/firestore/databases/${id === "(default)" ? "-default-" : id}/data`;
  // The Cloud Run address is <service>-<project number>.<region>.run.app.
  const [label, region] = new URL(d.mcp_endpoint).hostname.split(".");
  const service = label.replace(/-\d+$/, "");
  const rows = [
    ["This dashboard and MCP", "The Cloud Run service your AI apps talk to", cloud(`run/detail/${region}/${service}/metrics`)],
    ["Google Health access", "Secret Manager: the OAuth client and refresh token; only the connector can read them", cloud(`security/secret-manager/secret/${d.connection_secret}/versions`)],
    ["OAuth client and consent", "Google Auth Platform: the client, its scopes, and whether it's published", cloud("auth/overview")],
    ["AI app sign-ins", "Firestore: the AI apps you allowed", database(d.auth_database)],
    ["Logs", "What the connector has been doing", cloud("logs/query")],
    ["Usage and billing", "What this project costs on the Blaze plan", `${firebase}/usage`],
  ];
  el("resources").replaceChildren(...rows.map(([title, detail, href]) => {
    const row = document.createElement("li");
    const link = external(href, "");
    const strong = document.createElement("strong");
    strong.textContent = title;
    const span = document.createElement("span");
    span.textContent = detail;
    link.append(strong, span);
    row.append(link);
    return row;
  }));
  el("project").href = `${firebase}/overview`;
}

// ---------- Developer ----------

let previewTarget = "reconcile_data_points";

async function preview(target) {
  previewTarget = target;
  for (const button of document.querySelectorAll("[data-preview]"))
    button.setAttribute("aria-pressed", String(button.dataset.preview === target));
  const output = el("preview-output");
  output.textContent = "Loading…";
  try {
    output.textContent = JSON.stringify(await api("/owner/preview", {target}), null, 2);
  } catch (error) {
    if (error instanceof OwnerSessionExpired) return expireSession();
    output.textContent = "The preview couldn't be loaded.";
  }
}

for (const button of document.querySelectorAll("[data-preview]"))
  button.onclick = () => preview(button.dataset.preview);

// ---------- Sign-in ----------

async function signedIn(value) {
  user = value;
  el("identity").textContent = user.email;
  // The dashboard stays hidden until the first request shows this is the owner.
  say("signin-message", "Checking your account…");
  try {
    await loadStatus();
  } catch (error) {
    // The first request is also where a non-owner Google account is turned away.
    if (error instanceof OwnerSessionExpired) return expireSession(
      `${user.email} doesn't own this deployment. Sign in with the account that does.`, "danger");
    say("message", "Your connector's status couldn't be loaded. Try reloading.", "danger");
  }
  say("signin-message", "");
  showSignedIn(true);
  if (returned) say("message", ...(CONNECT_COPY[returned] || CONNECT_COPY.expired));
}

async function expireSession(text = "Your sign-in expired. Sign in again to continue.", tone = "neutral") {
  await signOut(auth);
  say("signin-message", text, tone);
}

function signedOut() {
  user = status = null;
  el("project").classList.add("hidden");
  showSignedIn(false);
  el("signin").disabled = false;
}

try {
  const config = await fetch("/firebase-config").then(response => {
    if (!response.ok) throw new Error();
    return response.json();
  });
  auth = getAuth(initializeApp(config));
  // Session persistence: survives a reload of this tab, cleared when the tab closes.
  await setPersistence(auth, browserSessionPersistence);
  onAuthStateChanged(auth, value => {
    if (value) signedIn(value);
    else {
      signedOut();
      if (el("signin-message").textContent === "Loading…") say("signin-message", "");
    }
  });
} catch {
  say("signin-message", "Sign-in is unavailable. Check this deployment's Firebase setup.", "danger");
}

el("signin").onclick = async () => {
  el("signin").disabled = true;
  say("signin-message", "Opening Google sign-in…");
  try {
    const provider = new GoogleAuthProvider();
    provider.setCustomParameters({prompt: "select_account"});
    await signInWithPopup(auth, provider);
  } catch {
    say("signin-message", "Sign-in wasn't completed.");
    el("signin").disabled = false;
  }
};

el("signout").onclick = () => signOut(auth).then(() => say("signin-message", "Signed out."));
window.addEventListener("hashchange", () => { if (user) showView(); });

