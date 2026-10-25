// README artwork: node tests_ui/screenshots.mjs
// Uses the real dashboard with synthetic data and intercepted requests only.
// The conversation illustration is an example, not a screenshot of any AI app.
import {readFileSync, mkdirSync} from "node:fs";
import {fileURLToPath} from "node:url";
import {chromium, expect} from "@playwright/test";

const STATIC = new URL("../firebase/mcp/static/", import.meta.url);
const OUTPUT = new URL("../docs/images/", import.meta.url);
const BASE = "https://owner.example";
// Every image is this many CSS pixels wide, so the README shows them at one width and text size.
const WIDTH = 1040;
const NOW = new Date("2026-10-25T14:00:00Z");
const seconds = NOW.getTime() / 1000;
const apps = [
  {client_id: "demo-claude", redirect_host: "claude.ai", connected_at: seconds - 9 * 86400, last_used_at: seconds - 600},
  {client_id: "demo-chatgpt", redirect_host: "chatgpt.com", connected_at: seconds - 4 * 86400, last_used_at: seconds - 7200},
];
const status = {
  google: {state: "connected", client_id: "123456789012-demo.apps.googleusercontent.com", email: "you@gmail.com",
    connected_at: seconds - 10 * 86400},
  last_failure: null, apps,
  diagnostics: {connector_version: "demo", repository: "induction-axiom/ai-connector-for-google-health",
    project_id: "my-health-ai", connection_secret: "google-health-connection", auth_database: "(default)",
    mcp_endpoint: "https://google-health-mcp-123456789012.us-east4.run.app/mcp"},
};

// One night as reconcile_data_points answers it: Google's fields and types, made-up values.
const at = (time, offset = "-14400s") => ({...time, startUtcOffset: offset, endUtcOffset: offset});
const stages = [["AWAKE", "04:12", "04:19"], ["LIGHT", "04:19", "04:51"], ["DEEP", "04:51", "05:38"],
  ["LIGHT", "05:38", "06:20"], ["REM", "06:20", "06:44"], ["LIGHT", "06:44", "07:31"]]
  .map(([type, start, end]) => ({type, ...at({startTime: `2026-10-25T${start}:00Z`, endTime: `2026-10-25T${end}:00Z`})}));
const lastSleep = {
  ok: true,
  dataPoints: [{
    dataPointName: "users/me/dataTypes/sleep/dataPoints/7184402937155530241",
    sleep: {
      interval: at({startTime: "2026-10-25T04:12:00Z", endTime: "2026-10-25T11:20:00Z"}),
      type: "STAGES",
      summary: {minutesInSleepPeriod: "428", minutesAsleep: "396", minutesAwake: "32", minutesToFallAsleep: "7",
        minutesAfterWakeUp: "3", stagesSummary: [{type: "DEEP", minutes: "71", count: "4"},
          {type: "REM", minutes: "88", count: "5"}, {type: "LIGHT", minutes: "237", count: "21"},
          {type: "AWAKE", minutes: "32", count: "19"}]},
      stages,
      metadata: {mainSleep: true, nap: false, processed: true, stagesStatus: "SUCCEEDED"},
    },
  }],
  nextPageToken: "CgwI6Ma9xwYQwJbDqAI",
};

const FIREBASE_APP = "export const initializeApp = config => ({config});";
const FIREBASE_AUTH = `
  export const getAuth = app => ({app});
  export class GoogleAuthProvider { setCustomParameters() {} }
  export const browserSessionPersistence = "session";
  export const setPersistence = async () => {};
  export const signInWithPopup = async () => {};
  export const signOut = async () => {};
  export const onAuthStateChanged = (auth, callback) => setTimeout(() => callback(
    {email: "you@gmail.com", getIdToken: async () => "demo-token"}));
`;

async function dashboard(page) {
  await page.clock.install({time: NOW});
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.route("**/*", async route => {
    const url = new URL(route.request().url());
    if (url.hostname === "www.gstatic.com") {
      if (!["firebase-app.js", "firebase-auth.js"].some(name => url.pathname.endsWith(name)))
        throw new Error("Unexpected Firebase module");
      return route.fulfill({contentType: "text/javascript",
        body: url.pathname.endsWith("firebase-auth.js") ? FIREBASE_AUTH : FIREBASE_APP});
    }
    // No update banner: the release check is answered as if there were none.
    if (url.hostname === "api.github.com") return route.fulfill({status: 404, body: ""});
    if (url.origin !== BASE) throw new Error("Unexpected external request");
    const json = body => route.fulfill({contentType: "application/json", body: JSON.stringify(body)});
    if (url.pathname === "/owner")
      return route.fulfill({contentType: "text/html", body: readFileSync(new URL("owner.html", STATIC))});
    if (["/assets/style.css", "/assets/owner.js"].includes(url.pathname))
      return route.fulfill({contentType: url.pathname.endsWith(".js") ? "text/javascript" : "text/css",
        body: readFileSync(new URL(url.pathname.slice(8), STATIC))});
    if (url.pathname === "/firebase-config") return json({authDomain: "auth.example"});
    if (url.pathname === "/owner/status") return json(status);
    if (url.pathname === "/owner/preview") return json(lastSleep);
    throw new Error("Unexpected owner request");
  });
  await page.goto(BASE + "/owner");
  await expect(page.locator("#status-title")).toHaveText("Claude and ChatGPT can read your Google Health data.");
  await page.clock.pauseAt(NOW.getTime() + 1000);
  await page.evaluate(() => document.fonts.ready);
  return errors;
}

const conversationCSS = `
  :root { color-scheme: light; --bg:#f5f5f7; --surface:#fff; --ink:#1d1d1f;
    --muted:#6e6e73; --line:#e8e8ed; --blue:#0071e3; --green:#1d8a3f; --amber:#b26a00; }
  @media(prefers-color-scheme:dark) { :root { color-scheme:dark; --bg:#000;
    --surface:#1c1c1e; --ink:#f5f5f7; --muted:#a1a1a6; --line:#333336;
    --blue:#0071e3; --green:#30d158; --amber:#ffb340; } }
  * {box-sizing:border-box} body {margin:0; background:var(--bg); color:var(--ink);
    font:17px/1.48 -apple-system,BlinkMacSystemFont,"Helvetica Neue",sans-serif;
    -webkit-font-smoothing:antialiased} main {padding:38px; width:${WIDTH}px}
  header {display:flex; align-items:center; justify-content:space-between; gap:18px; margin:0 6px 22px}
  header strong {font-size:18px; letter-spacing:-.025em} .sample {font-size:13px;
    background:var(--line); color:var(--muted); padding:4px 12px; border-radius:24px; white-space:nowrap}
  .user {max-width:620px; margin:0 0 18px auto; padding:16px 20px; border-radius:24px 24px 6px 24px;
    background:var(--blue); color:#fff} .answer {padding:24px 28px; margin-bottom:20px;
    background:var(--surface); border-radius:24px} p {margin:0 0 14px} p:last-child {margin:0}
  .tools {display:flex; flex-wrap:wrap; gap:8px; margin-bottom:16px}
  .tool {font:12.5px/1.4 ui-monospace,SFMono-Regular,Menlo,monospace;
    color:var(--muted); background:var(--line); padding:4px 8px; border-radius:8px}
  h1 {font-size:28px; letter-spacing:-.035em; line-height:1.16; margin:0 0 16px}
  .muted {color:var(--muted)} .small {font-size:14px} .lower {color:var(--amber)}
  table {width:100%; border-collapse:collapse; margin:6px 0 20px; font-variant-numeric:tabular-nums}
  th {text-align:right; font-size:14px; font-weight:600; color:var(--muted); padding:0 0 8px}
  th:first-child, td:first-child {text-align:left} td {padding:9px 0; border-top:1px solid var(--line); text-align:right}
  td:first-child {color:var(--muted)} td + td {font-weight:600; width:180px}
  footer {margin:4px 6px 0; color:var(--muted); font-size:13px; line-height:1.5}
`;
const examples = {
  "example-sleep": `
    <header><strong>Ask about your sleep, night by night.</strong><span class="sample">Simulated example</span></header>
    <div class="user">How has my sleep been over the last two weeks? Does my HRV follow it?</div>
    <div class="answer">
      <div class="tools"><span class="tool">reconcile_data_points · sleep · 14 nights</span>
        <span class="tool">reconcile_data_points · daily-heart-rate-variability · 14 days</span></div>
      <h1>You slept 6 h 48 min a night, and less in the second week.</h1>
      <table><thead><tr><th></th><th>Oct 11–17</th><th>Oct 18–24</th></tr></thead><tbody>
        <tr><td>Asleep, average</td><td>7 h 06 min</td><td class="lower">6 h 30 min</td></tr>
        <tr><td>Deep sleep</td><td>1 h 12 min</td><td class="lower">58 min</td></tr>
        <tr><td>REM sleep</td><td>1 h 34 min</td><td>1 h 27 min</td></tr>
        <tr><td>HRV, average</td><td>44 ms</td><td class="lower">38 ms</td></tr>
      </tbody></table>
      <p>Your four shortest nights, all under 6 hours, were each followed by a day with HRV under 36 ms.
        Three of them started after 00:40.</p>
      <p class="muted small">From Google Health's own records, one entry per night. Not medical advice.</p>
    </div>
    <footer>Illustration with made-up data. The AI app does the analysis; the connector passes on Google Health's answers.</footer>`,
};

mkdirSync(OUTPUT, {recursive: true});
const browser = await chromium.launch();
try {
  for (const colorScheme of ["light", "dark"]) {
    const context = await browser.newContext({viewport: {width: WIDTH, height: 1100},
      deviceScaleFactor: 1.5, colorScheme, locale: "en-CA", timezoneId: "America/Toronto",
      reducedMotion: "reduce", serviceWorkers: "block"});
    const page = await context.newPage();
    const errors = await dashboard(page);
    const save = (name, options = {}) => page.screenshot({path: fileURLToPath(new URL(`${name}-${colorScheme}.png`, OUTPUT)),
      animations: "disabled", ...options});

    // The hero and the Google Health panel.
    const google = await page.locator("#google-rows").locator("..").boundingBox();
    await save("overview", {clip: {x: 0, y: 0, width: WIDTH, height: Math.ceil(google.y + google.height + 24)}});

    // The AI apps panel with Claude's steps open, from its heading down.
    await page.locator('[data-connect="claude"]').click();
    // Taller than the window, so measured in page coordinates and captured from the full page.
    const [top, bottom] = await page.locator("#apps").evaluate(head => [
      head.getBoundingClientRect().top + scrollY - 18,
      head.nextElementSibling.getBoundingClientRect().bottom + scrollY + 24]);
    // The sticky nav would otherwise be drawn over the middle of a full-page capture.
    await save("connect-claude", {fullPage: true, clip: {x: 0, y: top, width: WIDTH, height: Math.ceil(bottom - top)},
      style: ".nav { position: static !important; }"});

    // What a tool returns, as the Developer tab shows it.
    await page.locator('a[data-view="developer"]').click();
    const output = page.locator("#preview-output");
    await expect(output).toContainText("dataPointName");
    const panel = await output.locator("..").evaluate(element => {
      const box = element.getBoundingClientRect();
      return [box.top + scrollY - 24, box.bottom + scrollY + 24];
    });
    await save("tool-output", {fullPage: true, clip: {x: 0, y: panel[0], width: WIDTH, height: Math.ceil(panel[1] - panel[0])},
      style: ".nav { position: static !important; }"});
    if (errors.length) throw new Error(errors.join("\n"));

    for (const [name, body] of Object.entries(examples)) {
      // A short viewport, so the full-page capture ends where the content does.
      await page.setViewportSize({width: WIDTH, height: 400});
      await page.setContent(`<!doctype html><html lang="en"><meta charset="utf-8">
        <title>Google Health connector — simulated example</title><style>${conversationCSS}</style><main>${body}</main></html>`);
      await page.evaluate(() => document.fonts.ready);
      await save(name, {fullPage: true});
    }
    await context.close();
    console.log(`Generated 4 ${colorScheme} screenshots with synthetic data.`);
  }
} finally {
  await browser.close();
}
