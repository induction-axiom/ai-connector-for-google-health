// Runs on <project>.firebaseapp.com, the one origin Firebase's Google client already allows,
// so Google's popup can hand this page an authorization code without any OAuth setup.
// The page sends the code to the connector, which exchanges it, then returns to the dashboard.
const el = id => document.getElementById(id);
const status = (text, tone = "neutral") => {
  el("status").textContent = text;
  el("status").dataset.tone = tone;
};

// Written by the setup with this deployment's values; never taken from the link.
const config = await fetch("/google-connect.json").then(response => response.json());
const back = outcome => location.assign(`${config.connector}/owner?google=${encodeURIComponent(outcome)}`);
el("back").href = config.connector + "/owner";
el("back").classList.remove("hidden");

// The dashboard links here with a one-time state, and the scopes to ask for.
const params = new URLSearchParams(location.hash.slice(1));
history.replaceState(null, "", location.pathname);
const state = params.get("state");
const scope = params.get("scope");

async function exchange(code) {
  status("Saving access…");
  try {
    const response = await fetch(config.connector + "/google/exchange", {
      method: "POST", headers: {"Content-Type": "application/json", "X-Requested-With": "XmlHttpRequest"},
      body: JSON.stringify({state, code})});
    const {outcome} = await response.json();
    back(outcome || "google_unavailable");
  } catch {
    status("Your connector couldn't be reached. Go back to your dashboard and try again.", "danger");
  }
}

function waitForGoogle() {
  return new Promise(resolve => {
    const check = () => window.google?.accounts?.oauth2 ? resolve() : setTimeout(check, 50);
    check();
  });
}

if (!state || !scope) {
  status("Start from your dashboard: choose Connect Google Health there.", "warning");
} else {
  await waitForGoogle();
  // Opening Google's window needs the click itself, or the browser blocks it.
  const client = google.accounts.oauth2.initCodeClient({
    client_id: config.client_id, scope, ux_mode: "popup", select_account: true,
    include_granted_scopes: false,
    callback: response => response.code ? exchange(response.code) : back("denied"),
    error_callback: () => {
      el("continue").disabled = false;
      status("Google's window was closed or blocked. Choose Continue with Google to try again.", "warning");
    },
  });
  el("continue").onclick = () => {
    el("continue").disabled = true;
    status("Waiting for Google…");
    client.requestCode();
  };
  el("continue").disabled = false;
  status("");
}
