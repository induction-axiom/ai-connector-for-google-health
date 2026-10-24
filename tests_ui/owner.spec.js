// Synthetic data only: the page is served from firebase/mcp/static, with Google sign-in
// and every /owner API replaced, so no deployment or account is touched.
import {readFileSync} from "node:fs";
import {test, expect} from "@playwright/test";

const STATIC = new URL("../firebase/mcp/static/", import.meta.url);
const BASE = "https://owner.test";

const FIREBASE_APP = "export const initializeApp = config => ({config});";
const OWNER = "owner@example.com";
// Signed in as `email` on load; signing out tells the page, as Firebase does.
const firebaseAuth = email => `
  let listener;
  export const getAuth = app => ({app});
  export class GoogleAuthProvider { setCustomParameters() {} }
  export const browserSessionPersistence = "session";
  export const setPersistence = async () => {};
  export const signInWithPopup = async () => {};
  export const signOut = async () => listener(null);
  export const onAuthStateChanged = (auth, callback) => {
    listener = callback;
    setTimeout(() => callback({email: "${email}", getIdToken: async () => "synthetic-token"}));
  };
`;

const SCOPES = ["sleep", "health_metrics_and_measurements", "activity_and_fitness", "profile", "settings"]
  .map(area => `https://www.googleapis.com/auth/googlehealth.${area}.readonly`);
const CLIENT_ID = "123456789012-abcdefghijklmnop.apps.googleusercontent.com";
const CONNECTED = {state: "connected", client_id: CLIENT_ID, email: OWNER, connected_at: 1790000000, missing_scopes: []};

function status(state) {
  return {
    google: state.google,
    last_failure: state.lastFailure,
    apps: [],
    diagnostics: {connector_version: "test", repository: "example/example", project_id: "your-project",
      connection_secret: "google-health-connection", auth_database: "(default)", mcp_endpoint: BASE + "/mcp",
      connect_page: "https://your-project.firebaseapp.com/google-connect.html", scopes: SCOPES},
  };
}

// Serve the dashboard with a stand-in for each /owner API, recording what the page sends.
async function openDashboard(page, {google = CONNECTED, lastFailure = null, email = OWNER, path = "/owner"} = {}) {
  const state = {google, lastFailure, sent: [], errors: []};
  page.on("pageerror", error => state.errors.push(error.message));
  await page.route("**/*", async route => {
    const url = new URL(route.request().url());
    const json = body => route.fulfill({contentType: "application/json", body: JSON.stringify(body)});
    if (url.hostname === "www.gstatic.com")
      return route.fulfill({contentType: "text/javascript",
        body: url.pathname.endsWith("firebase-auth.js") ? firebaseAuth(email) : FIREBASE_APP});
    if (url.hostname === "your-project.firebaseapp.com") {
      state.connect_page = url;
      return route.fulfill({contentType: "text/html", body: "Connect page"});
    }
    if (url.origin !== BASE) return route.fulfill({status: 404, body: ""});
    if (url.pathname === "/owner")
      return route.fulfill({contentType: "text/html", body: readFileSync(new URL("owner.html", STATIC))});
    if (url.pathname.startsWith("/assets/")) {
      const name = url.pathname.slice("/assets/".length);
      return route.fulfill({contentType: name.endsWith(".js") ? "text/javascript" : "text/css",
        body: readFileSync(new URL(name, STATIC))});
    }
    if (url.pathname === "/firebase-config") return json({authDomain: "example.test"});
    if (url.pathname === "/owner/status" && email !== OWNER) return new Promise(settle => {
      state.turnAway = () => route.fulfill({status: 403, contentType: "application/json",
        body: JSON.stringify({error: "owner_login_required"})}).then(settle);
    });
    if (url.pathname === "/owner/status") return json(status(state));
    if (url.pathname.startsWith("/owner/google/")) state.sent.push([url.pathname, route.request().postDataJSON()]);
    if (url.pathname === "/owner/google/start")
      return json({result: "redirect", url: "https://your-project.firebaseapp.com/google-connect.html#state=s"});
    if (url.pathname === "/owner/google/disconnect") {
      state.google = {state: "not_connected", client_id: CLIENT_ID};
      return json({result: "disconnected"});
    }
    return json({});
  });
  await page.goto(BASE + path);
  return state;
}

test("connecting goes to the connect page on Firebase Hosting", async ({page}) => {
  const state = await openDashboard(page, {google: {state: "not_connected", client_id: CLIENT_ID}});
  await expect(page.locator("#status-title")).toHaveText("Connect Google Health.");
  await expect(page.locator("#primary-action")).toBeHidden();
  await expect(page.locator("#connect-note")).toContainText("It can be a different account from this one.");
  await page.click("#google-connect");
  await expect.poll(() => state.connect_page?.pathname).toBe("/google-connect.html");
  await expect(page).toHaveURL("https://your-project.firebaseapp.com/google-connect.html#state=s");
  expect(state.sent).toEqual([["/owner/google/start", {}]]);
  expect(state.errors).toEqual([]);
});

test("an unfinished setup says to run it again", async ({page}) => {
  const state = await openDashboard(page, {google: {state: "no_client"}});
  await expect(page.locator("#status-title")).toHaveText("Finish the setup.");
  await expect(page.locator("#google-connect")).toBeHidden();
  await page.click("#primary-action");
  await expect(page.locator("#guide-title")).toHaveText("Run the setup again");
  await expect(page.locator("#guide-body code")).toHaveText("firebase/scripts/bootstrap.sh your-project " + OWNER);
  expect(state.errors).toEqual([]);
});

test("coming back from Google says what happened, once", async ({page}) => {
  const state = await openDashboard(page, {path: "/owner?google=connected"});
  await expect(page.locator("#message")).toHaveText("Connected. Your AI apps can read your Google Health data now.");
  await expect(page.locator("#status-title")).toHaveText("Connect an AI app.");
  expect(new URL(page.url()).search).toBe("");
  await expect(page.locator("#google-rows")).toContainText(OWNER);
  expect(state.errors).toEqual([]);
});

test("disconnecting takes two clicks and keeps the client", async ({page}) => {
  const state = await openDashboard(page);
  const disconnect = page.locator("#google-rows button");
  await disconnect.click();
  await expect(disconnect).toHaveText("Confirm disconnect");
  expect(state.sent).toEqual([]);
  await disconnect.click();
  await expect(page.locator("#status-title")).toHaveText("Connect Google Health.");
  await expect(page.locator("#google-rows")).toContainText("Not connected");
  expect(state.sent).toEqual([["/owner/google/disconnect", {}]]);
  expect(state.errors).toEqual([]);
});

test("data left out at Google is named, with a way to connect again", async ({page}) => {
  const state = await openDashboard(page, {google: {...CONNECTED, missing_scopes: [SCOPES[1]]}});
  await expect(page.locator("#google-rows")).toContainText("Not shared");
  await expect(page.locator("#google-rows")).toContainText("Health metrics (heart rate, HRV, SpO2…)");
  await expect(page.locator("#google-connect")).toHaveText("Connect again");
  expect(state.errors).toEqual([]);
});

test("ended access asks to connect again", async ({page}) => {
  const state = await openDashboard(page, {google: {...CONNECTED, state: "google_health_disconnected"}});
  await expect(page.locator("#status-title")).toHaveText("Connect Google Health again.");
  await expect(page.locator("#status-summary")).toContainText("it was revoked, unused for 6 months");
  await expect(page.locator("#google-connect")).toBeVisible();
  expect(state.errors).toEqual([]);
});

test("Google's status shows only when something is wrong", async ({page}) => {
  const now = Date.now() / 1000;
  const state = await openDashboard(page, {lastFailure: {code: "google_unavailable", at: now - 8 * 86400}});
  await expect(page.locator("#google-rows")).toContainText(OWNER);
  await expect(page.locator("#google-facts")).toBeHidden();

  state.lastFailure = {code: "google_unavailable", at: now - 3600};
  await page.reload();
  await expect(page.locator("#fact-last-error")).toHaveText("Google isn't answering, 1 hour ago");
  await expect(page.locator("#fact-live-row")).toBeHidden();

  state.google = {...CONNECTED, state: "google_unavailable"};
  await page.reload();
  await expect(page.locator("#fact-live")).toHaveText("Google isn't answering");
  expect(state.errors).toEqual([]);
});

test("choosing an app shows only its steps, with a link to its own guide", async ({page}) => {
  const state = await openDashboard(page);
  await expect(page.locator("#mcp-url")).toHaveText(BASE + "/mcp");
  const steps = page.locator("#connect-steps");
  await expect(steps).toBeHidden();
  await expect(page.locator("[data-connect]")).toHaveText(["ChatGPT", "Claude", "Gemini", "Muse", "Another app"]);
  await page.locator('[data-connect="chatgpt"]').click();
  await expect(steps).toBeVisible();
  await expect(steps).toContainText("Name it Google Health");
  const links = await steps.locator("a").evaluateAll(
    anchors => anchors.map(a => [a.textContent, a.href, a.target]));
  expect(links).toEqual([["ChatGPT's guide", "https://help.openai.com/en/articles/12584461", "_blank"]]);
  await page.locator('[data-connect="muse"]').click();
  await expect(page.locator('[data-connect][aria-pressed="true"]')).toHaveText(["Muse"]);
  await expect(steps).not.toContainText("ChatGPT");
  await expect(steps).toContainText("paste the address above and ask Muse to add it");
  await page.locator('[data-connect="muse"]').click();
  await expect(steps).toBeHidden();
  await expect(page.locator('[data-connect][aria-pressed="true"]')).toHaveCount(0);
  expect(state.errors).toEqual([]);
});

test("another Google account is turned away before the dashboard shows", async ({page}) => {
  const state = await openDashboard(page, {email: "someone@example.com"});
  await expect(page.locator("#signin-message")).toHaveText("Checking your account…");
  await expect.poll(() => typeof state.turnAway).toBe("function");
  await expect(page.locator("#overview-view")).toBeHidden();
  await expect(page.locator("#account")).toBeHidden();
  await state.turnAway();
  await expect(page.locator("#signin-message")).toHaveText(
    "someone@example.com doesn't own this deployment. Sign in with the account that does.");
  await expect(page.locator("#signin-message")).toHaveAttribute("data-tone", "danger");
  await expect(page.locator("#signin")).toBeEnabled();
  await expect(page.locator("#overview-view")).toBeHidden();
  expect(state.errors).toEqual([]);
});
