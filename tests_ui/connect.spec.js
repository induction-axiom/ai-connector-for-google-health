// The connect page on Firebase Hosting, with Google's popup script and the connector replaced.
import {readFileSync} from "node:fs";
import {test, expect} from "@playwright/test";

const HOSTING = new URL("../firebase/hosting/", import.meta.url);
const STYLE = new URL("../firebase/mcp/static/style.css", import.meta.url);
const PAGE = "https://your-project.firebaseapp.com";
const CONNECTOR = "https://connector.test";
const CLIENT_ID = "123456789012-abcdefghijklmnop.apps.googleusercontent.com";
const SCOPE = "https://www.googleapis.com/auth/googlehealth.sleep.readonly openid email";

// Google's script: hands the page `answer` when it asks for a code, as the popup would.
const gsi = answer => `window.google = {accounts: {oauth2: {initCodeClient: config => {
  window.codeClientConfig = {...config, callback: undefined, error_callback: undefined};
  return {requestCode: () => ${answer === "closed" ? "config.error_callback({type: 'popup_closed'})"
    : `config.callback(${JSON.stringify(answer)})`}};
}}}};`;

async function openConnect(page, {fragment, answer = {code: "synthetic-good-code"}, outcome = "connected"} = {}) {
  const state = {exchanges: [], errors: []};
  page.on("pageerror", error => state.errors.push(error.message));
  await page.route("**/*", async route => {
    const url = new URL(route.request().url());
    const file = (name, type) => route.fulfill({contentType: type, body: readFileSync(new URL(name, HOSTING))});
    if (url.href === "https://accounts.google.com/gsi/client")
      return route.fulfill({contentType: "text/javascript", body: gsi(answer)});
    if (url.origin === PAGE && url.pathname === "/google-connect.html") return file("google-connect.html", "text/html");
    if (url.origin === PAGE && url.pathname === "/google-connect.js") return file("google-connect.js", "text/javascript");
    if (url.origin === PAGE && url.pathname === "/style.css")
      return route.fulfill({contentType: "text/css", body: readFileSync(STYLE)});
    if (url.origin === PAGE && url.pathname === "/google-connect.json")
      return route.fulfill({contentType: "application/json", body: JSON.stringify({connector: CONNECTOR, client_id: CLIENT_ID})});
    if (url.origin === CONNECTOR && url.pathname === "/google/exchange") {
      state.exchanges.push({headers: route.request().headers(), body: route.request().postDataJSON()});
      return route.fulfill({contentType: "application/json", body: JSON.stringify({outcome}),
        headers: {"access-control-allow-origin": PAGE}});
    }
    if (url.origin === CONNECTOR && url.pathname === "/owner") {
      state.back = url;
      return route.fulfill({contentType: "text/html", body: "Dashboard"});
    }
    return route.fulfill({status: 404, body: ""});
  });
  await page.goto(PAGE + "/google-connect.html" + (fragment ?? "#" + new URLSearchParams({state: "s".repeat(43), scope: SCOPE})));
  return state;
}

test("Google's code goes to this connector, then back to the dashboard", async ({page}) => {
  const state = await openConnect(page);
  await expect(page.locator("#continue")).toBeEnabled();
  expect(new URL(page.url()).hash).toBe("");
  await page.click("#continue");
  await expect.poll(() => state.back?.search).toBe("?google=connected");
  expect(state.exchanges).toHaveLength(1);
  expect(state.exchanges[0].body).toEqual({state: "s".repeat(43), code: "synthetic-good-code"});
  expect(state.exchanges[0].headers["x-requested-with"]).toBe("XmlHttpRequest");
  expect(state.errors).toEqual([]);
});

test("the client and where the code goes come from the deployment, not the link", async ({page}) => {
  const state = await openConnect(page, {fragment: "#" + new URLSearchParams({state: "s".repeat(43), scope: SCOPE,
    client_id: "attacker", connector: "https://evil.example"})});
  await expect(page.locator("#continue")).toBeEnabled();
  const config = await page.evaluate(() => window.codeClientConfig);
  expect(config).toEqual({client_id: CLIENT_ID, scope: SCOPE, ux_mode: "popup", select_account: true,
    include_granted_scopes: false});
  await page.click("#continue");
  await expect.poll(() => state.back?.origin).toBe(CONNECTOR);
  expect(state.errors).toEqual([]);
});

test("a refusal at Google goes back saying so", async ({page}) => {
  const state = await openConnect(page, {answer: {error: "access_denied"}});
  await page.click("#continue");
  await expect.poll(() => state.back?.search).toBe("?google=denied");
  expect(state.exchanges).toEqual([]);
});

test("a closed Google window can be tried again", async ({page}) => {
  const state = await openConnect(page, {answer: "closed"});
  await page.click("#continue");
  await expect(page.locator("#status")).toHaveText(
    "Google's window was closed or blocked. Choose Continue with Google to try again.");
  await expect(page.locator("#continue")).toBeEnabled();
  expect(state.errors).toEqual([]);
});

test("without the dashboard's link it says where to start", async ({page}) => {
  const state = await openConnect(page, {fragment: ""});
  await expect(page.locator("#status")).toHaveText("Start from your dashboard: choose Connect Google Health there.");
  await expect(page.locator("#continue")).toBeDisabled();
  await expect(page.locator("#back")).toHaveAttribute("href", CONNECTOR + "/owner");
  expect(state.errors).toEqual([]);
});
