#!/usr/bin/env python3
"""Provision, build, and deploy a self-hosted connector; never handles Google Health credentials."""
import argparse
import base64
import json
import os
import re
import shutil
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from configuration import ROOT, DEFAULT_REGION, NAMES, make_config, email, mcp_env
from console import Console, duration

ui = Console()


REQUIRED_APIS = (
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "firebase.googleapis.com",
    "firebasehosting.googleapis.com",
    "firestore.googleapis.com",
    "health.googleapis.com",
    "identitytoolkit.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    with os.fdopen(fd, "w") as f:
        json.dump(value, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def run(args, *, capture=True, cwd=None, secret=False, interactive=False):
    """Runs a command with its output shown under the running step; returns stdout if capture."""
    code, stdout, stderr = ui.run(args, cwd=cwd, secret=secret, interactive=interactive)
    if code:
        # The whole output is already on screen and in the log; name the tool's own reason.
        # gcloud writes "ERROR: (gcloud.x) reason" to stderr.
        reason = next((line.removeprefix("ERROR:").strip() for line in stderr.splitlines()
                       if line.startswith("ERROR:")), "")
        try:
            # The Firebase CLI with --json reports {"status": "error", "error": "reason"}.
            output = json.loads(stdout or "null") if not secret else None
            if isinstance(output, dict) and output.get("status") == "error":
                reason = reason or str(output.get("error", ""))
        except json.JSONDecodeError:
            pass
        raise RuntimeError("Command failed: " + " ".join(str(x) for x in args[:3])
                           + ("\n  " + reason[:500] if reason else ""))
    return stdout.strip() if capture else ""


def run_json(args, *, cwd=None):
    try:
        value = json.loads(run(args, cwd=cwd))
    except json.JSONDecodeError:
        raise RuntimeError("Command returned an unexpected response") from None
    if isinstance(value, dict) and value.get("status") == "success" and "result" in value:
        return value["result"]
    return value


def cloud(cfg, *args):
    return run(["gcloud", *args, "--project", cfg["project_id"], "--quiet"])


FIREBASE_BIN = ROOT / ".tools/firebase-cli/node_modules/.bin/firebase"


def firebase_cli():
    bundled = FIREBASE_BIN
    if bundled.exists():
        return [str(bundled)]
    manifest = ROOT / ".tools/firebase-cli/package.json"
    if not manifest.exists():
        raise RuntimeError("Firebase CLI manifest missing; clone the complete repository")
    if not shutil.which("npm"):
        raise RuntimeError("npm is required to install the pinned Firebase CLI")
    run(["npm", "ci", "--prefix", manifest.parent], capture=False)
    if not bundled.exists():
        raise RuntimeError("Pinned Firebase CLI installation failed")
    ui.created("Firebase CLI in .tools/firebase-cli")
    return [str(bundled)]


def firebase(cfg, *args):
    return run_json([*firebase_cli(), *args, "--project", cfg["project_id"],
                     "--non-interactive", "--json"])


def build(cfg):
    """Copy only allowlisted sources into a fresh bundle for the Cloud Run deploy."""
    destination = ROOT / "firebase/.build" / cfg["project_id"]
    source, bundle = ROOT / "firebase/mcp", destination / "mcp"
    # Start clean so nothing stale or hand-added is uploaded.
    if bundle.exists():
        shutil.rmtree(bundle)
    for file in source.rglob("*"):
        relative = file.relative_to(source)
        if (not file.is_file()
                or any(part.startswith(".") or part in {"venv", "__pycache__"} for part in relative.parts)
                or not (file.suffix in {".py", ".js", ".html", ".css"}
                        or file.name in {"Dockerfile", "requirements.txt"})):
            continue
        output = bundle / relative
        # Default permissions: the container runs as a non-root user that must read these.
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, output)
    # Plain write, not write_json (0600): the container's non-root user must read it.
    (bundle / "version.json").write_text(json.dumps(build_version()))
    (bundle / ".gcloudignore").write_text("*\n!Dockerfile\n!requirements.txt\n!*.py\n!version.json\n!static/\n!static/**\n"
        # Synced folders (iCloud Desktop) can drop conflict copies like "app 2.py" here.
        "* *\n")
    return destination


def build_version():
    """What the dashboard shows and compares against the latest release."""
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    try:
        commit = run(["git", "-C", ROOT, "log", "-1", "--format=%H %cs"]).split()
    except (RuntimeError, OSError):
        commit = []
    return {"version": version, "commit": commit[0] if commit else None,
            "committed_on": commit[1] if len(commit) > 1 else None}


def find_deployment(project):
    """The connector already deployed in this project, or None before the first full setup."""
    enabled = run(["gcloud", "services", "list", "--enabled", "--project", project,
                   "--format=value(config.name)", "--quiet"]).split()
    if "run.googleapis.com" not in enabled:
        return None
    services = json.loads(run(["gcloud", "run", "services", "list", "--project", project,
                               "--filter=metadata.name=" + NAMES["mcp_service"],
                               "--format=json", "--quiet"]))
    if not services:
        return None
    if len(services) > 1:
        raise RuntimeError("More than one " + NAMES["mcp_service"] + " service; delete the extra one")
    service = services[0]
    env = {item["name"]: item.get("value")
           for item in service["spec"]["template"]["spec"]["containers"][0].get("env", [])}
    return {"region": service["metadata"]["labels"]["cloud.googleapis.com/location"],
            "owner_email": env.get("MCP_OWNER_EMAIL"),
            "firebase_web_config": json.loads(env.get("FIREBASE_WEB_CONFIG") or "null")}


def deployed_config(project):
    """Settings for deploy, doctor and urls, read back from the deployed connector."""
    found = find_deployment(project)
    if not found:
        raise RuntimeError("No connector in this project yet: run firebase/scripts/bootstrap.sh")
    number = run(["gcloud", "projects", "describe", project, "--format=value(projectNumber)"])
    return make_config(project, found["owner_email"], found["region"], number,
                       found["firebase_web_config"])


def _database_condition(cfg, database, title):
    expression = f'resource.name=="projects/{cfg["project_id"]}/databases/{database}"'
    return "expression=" + expression + ",title=" + title


def _grant_database_role(cfg, role_name, database, role, title):
    cloud(cfg, "projects", "add-iam-policy-binding", cfg["project_id"],
          "--member=serviceAccount:" + email(cfg, role_name), "--role=" + role,
          "--condition=" + _database_condition(cfg, database, title))


def _ensure_database(cfg, database, existing, updating):
    full_name = f'projects/{cfg["project_id"]}/databases/{database}'
    if full_name not in existing:
        refuse_on_update(updating, "Firestore database " + database)
        cloud(cfg, "firestore", "databases", "create", "--database", database,
              "--location", cfg["region"], "--type=firestore-native", "--delete-protection")
        ui.created("Firestore database " + database)
    else:
        ui.reused("Firestore database " + database)
    details = json.loads(cloud(cfg, "firestore", "databases", "describe", "--database", database,
                               "--format=json"))
    # A (default) database made earlier, say in the Firebase console, may sit in another
    # location; that works, since the connector only keeps sign-ins there.
    if details.get("type") != "FIRESTORE_NATIVE":
        raise RuntimeError("Existing database is not in Firestore Native mode: " + database)


def _ensure_firebase_project(cfg):
    projects = firebase(cfg, "projects:list")
    if not isinstance(projects, list):
        raise RuntimeError("Firebase project inventory response changed")
    ids = {item.get("projectId") for item in projects if isinstance(item, dict)}
    if cfg["project_id"] not in ids:
        run_json([*firebase_cli(), "projects:addfirebase", cfg["project_id"],
                  "--non-interactive", "--json"])
        ui.created("Firebase in project " + cfg["project_id"])
    else:
        ui.reused("Firebase in project " + cfg["project_id"])


def _ensure_web_app(cfg):
    apps = firebase(cfg, "apps:list", "WEB")
    if not isinstance(apps, list):
        raise RuntimeError("Firebase web app inventory response changed")
    app = next((item for item in apps if isinstance(item, dict)
                and item.get("displayName") == "AI connector for Google Health"), None)
    if app is None and apps:
        app = apps[0]
    if app is None:
        app = firebase(cfg, "apps:create", "WEB", "AI connector for Google Health")
        ui.created("Firebase web app AI connector for Google Health")
    else:
        ui.reused("Firebase web app " + str(app.get("displayName") or app.get("appId")))
    if not isinstance(app, dict) or not app.get("appId"):
        raise RuntimeError("Firebase web app creation response changed")
    response = firebase(cfg, "apps:sdkconfig", "WEB", app["appId"])
    sdk = response.get("sdkConfig", response) if isinstance(response, dict) else None
    required = ("projectId", "apiKey", "authDomain", "appId")
    if not isinstance(sdk, dict) or not all(isinstance(sdk.get(key), str) and sdk[key]
                                            for key in required):
        raise RuntimeError("Firebase web configuration response changed")
    return {key: sdk[key] for key in required}


def google_api(cfg, url, body=None, method=None):
    """A Google REST call as the signed-in gcloud account. Bodies and answers may hold
    secrets, so neither is shown; only the method and address are."""
    headers = {"Authorization": "Bearer " + run(["gcloud", "auth", "print-access-token"], secret=True),
               "x-goog-user-project": cfg["project_id"], "Content-Type": "application/json"}
    method = method or ("POST" if body is not None else "GET")
    ui.line(method + " " + url.split("?", 1)[0])
    data = None if body is None else json.dumps(body).encode()
    with urlopen(Request(url, data=data, headers=headers, method=method), timeout=30) as response:
        return json.loads(response.read() or b"{}")


def _ensure_hosting_site(cfg):
    """The default Hosting site serves <project>.firebaseapp.com, where the connect page goes."""
    base = "https://firebasehosting.googleapis.com/v1beta1/projects/" + cfg["project_id"] + "/sites"
    sites = google_api(cfg, base).get("sites", [])
    if any(site.get("type") == "DEFAULT_SITE" for site in sites):
        ui.reused("Hosting site " + cfg["project_id"] + ".firebaseapp.com")
        return
    google_api(cfg, base + "?siteId=" + cfg["project_id"], {})
    ui.created("Hosting site " + cfg["project_id"] + ".firebaseapp.com")


def _save_google_client(cfg):
    """Copy the OAuth client Firebase made for Google sign-in into the connection secret,
    keeping a saved refresh token only if it is for the same client. Returns the client ID."""
    idp = google_api(cfg, "https://identitytoolkit.googleapis.com/admin/v2/projects/"
                     + cfg["project_id"] + "/defaultSupportedIdpConfigs/google.com")
    if not idp.get("clientId") or not idp.get("clientSecret"):
        raise RuntimeError("Firebase's Google sign-in has no OAuth client yet; run this again in a minute")
    client = {"client_id": idp["clientId"], "client_secret": idp["clientSecret"]}
    secret = ("https://secretmanager.googleapis.com/v1/projects/" + cfg["project_id"]
              + "/secrets/" + cfg["connection_secret_id"])
    try:
        payload = google_api(cfg, secret + "/versions/latest:access")["payload"]["data"]
        saved = json.loads(base64.b64decode(payload))
    except HTTPError as error:
        if error.code != 404:
            raise
        saved = {}
    keep = saved if saved.get("client_id") == client["client_id"] else {}
    updated = {**keep, **client}
    if updated == saved:
        ui.reused("Google client " + client["client_id"])
        return client["client_id"]
    old = google_api(cfg, secret + "/versions?filter=state:ENABLED").get("versions", [])
    google_api(cfg, secret + ":addVersion",
               {"payload": {"data": base64.b64encode(json.dumps(updated).encode()).decode()}})
    for version in old:
        google_api(cfg, "https://secretmanager.googleapis.com/v1/" + version["name"] + ":destroy", {})
    (ui.reused if saved else ui.created)("Google client " + client["client_id"] + " for Google Health")
    return client["client_id"]


def build_connect_page(cfg, client_id, destination):
    """The connect page and its fixed settings: where Google's answer goes, and which client asks."""
    hosting = destination / "hosting"
    if hosting.exists():
        shutil.rmtree(hosting)
    hosting.mkdir(parents=True)
    for name in ("google-connect.html", "google-connect.js"):
        shutil.copyfile(ROOT / "firebase/hosting" / name, hosting / name)
    shutil.copyfile(ROOT / "firebase/mcp/static/style.css", hosting / "style.css")
    (hosting / "google-connect.json").write_text(json.dumps(
        {"connector": mcp_env(cfg)["MCP_BASE_URL"], "client_id": client_id}))
    return hosting


def _ensure_authorized_domain(cfg):
    """Allow Google Sign-In popups from the deterministic Cloud Run origin."""
    domain = mcp_env(cfg)["MCP_BASE_URL"].removeprefix("https://")
    url = ("https://identitytoolkit.googleapis.com/admin/v2/projects/"
           + cfg["project_id"] + "/config")
    headers = {"Authorization": "Bearer " + run(["gcloud", "auth", "print-access-token"], secret=True),
               "x-goog-user-project": cfg["project_id"], "Content-Type": "application/json"}
    ui.line("GET " + url)
    with urlopen(Request(url, headers=headers), timeout=30) as response:
        domains = json.load(response).get("authorizedDomains", [])
    ui.line("Sign-in is allowed from: " + ", ".join(domains))
    if domain in domains:
        ui.reused("sign-in domain " + domain)
        return
    body = json.dumps({"authorizedDomains": [*domains, domain]}).encode()
    ui.line("PATCH " + url + "?updateMask=authorizedDomains")
    urlopen(Request(url + "?updateMask=authorizedDomains", data=body, headers=headers,
                    method="PATCH"), timeout=30).close()
    ui.created("sign-in domain " + domain)


def _keep_recent_images(cfg, repository):
    """Each source deploy stores a new image; keep the last two so storage stays in the free tier."""
    policy = [{"name": "keep-recent", "action": {"type": "Keep"},
               "mostRecentVersions": {"keepCount": 2}},
              {"name": "delete-older", "action": {"type": "Delete"},
               "condition": {"tagState": "ANY", "olderThan": "1d"}}]
    with tempfile.NamedTemporaryFile("w", suffix=".json") as file:
        json.dump(policy, file)
        file.flush()
        cloud(cfg, "artifacts", "repositories", "set-cleanup-policies", repository,
              "--location", cfg["region"], "--policy", file.name, "--no-dry-run")


# Google's popup, from the guide for its code model; the page talks only to the connector.
CONNECT_PAGE_POLICY = ("default-src 'none'; script-src 'self' https://accounts.google.com/gsi/client; "
                       "style-src 'self' https://accounts.google.com/gsi/style; "
                       "connect-src 'self' https://accounts.google.com/gsi/ {connector}; "
                       "frame-src https://accounts.google.com/gsi/; base-uri 'none'; form-action 'none'; "
                       "frame-ancestors 'none'")


def _write_bootstrap_firebase_config(cfg, destination):
    shutil.copyfile(ROOT / "firebase/firestore.rules", destination / "firestore.rules")
    # build_connect_page fills it; the Firebase CLI wants it to exist for every deploy.
    (destination / "hosting").mkdir(exist_ok=True)
    write_json(destination / "firestore.indexes.json", {"indexes": [], "fieldOverrides": []})
    value = {
        "auth": {"providers": {"googleSignIn": {
            "oAuthBrandDisplayName": "Private AI connector for Google Health",
            "supportEmail": cfg["owner_email"],
        }}},
        "firestore": [{"database": cfg["auth_database"], "rules": "firestore.rules",
                       "indexes": "firestore.indexes.json"}],
        "hosting": {"public": "hosting", "headers": [{"source": "**", "headers": [
            {"key": "Content-Security-Policy",
             "value": CONNECT_PAGE_POLICY.format(connector=mcp_env(cfg)["MCP_BASE_URL"])},
            {"key": "Cross-Origin-Opener-Policy", "value": "same-origin-allow-popups"},
            {"key": "Referrer-Policy", "value": "no-referrer"},
            {"key": "X-Content-Type-Options", "value": "nosniff"},
            {"key": "Cache-Control", "value": "no-cache"}]}]},
    }
    path = destination / "firebase.bootstrap.json"
    write_json(path, value)
    return path


SETUP_STEPS = (
    "Turn on the Google Cloud APIs it uses, including the Google Health API",
    "Create the service account the connector runs as",
    "Create the Firestore database for AI app sign-ins",
    "Create the secret that will hold your Google Health access",
    "Give the service account access to only what it needs",
    "Add Firebase Authentication with Google Sign-In for the dashboard",
    "Use Firebase's Google client for Google Health, and publish the connect page",
    "Deploy the connector and its dashboard (the slow part)",
    "Check that it rejects anonymous requests",
)


def setup_step(number):
    return ui.step(SETUP_STEPS[number - 1], number, len(SETUP_STEPS))


def refuse_on_update(updating, what):
    """An update only reuses: a missing piece means something is wrong, so don't recreate it."""
    if updating:
        raise RuntimeError(what + " is missing from your connector's project. Nothing new was "
                           "created; ask for help on the Issues page before running this again.")


def provision(cfg, updating):
    with setup_step(1):
        cloud(cfg, "services", "enable", *REQUIRED_APIS)

    with setup_step(2):
        accounts = set(cloud(cfg, "iam", "service-accounts", "list",
                             "--format=value(email)").splitlines())
        if email(cfg, "mcp") in accounts:
            ui.reused("service account " + email(cfg, "mcp"))
        else:
            refuse_on_update(updating, "Service account " + email(cfg, "mcp"))
            cloud(cfg, "iam", "service-accounts", "create", cfg["mcp_service_account"],
                  "--display-name", "Google Health connector")
            ui.created("service account " + email(cfg, "mcp"))

    with setup_step(3):
        databases = json.loads(cloud(cfg, "firestore", "databases", "list", "--format=json"))
        names = {item.get("name") for item in databases if isinstance(item, dict)}
        _ensure_database(cfg, cfg["auth_database"], names, updating)
        # Expired OAuth grants and per-minute rate-limit counters delete themselves.
        cloud(cfg, "firestore", "fields", "ttls", "update", "purge_after",
              "--collection-group=oauth_records", "--enable-ttl",
              "--database", cfg["auth_database"], "--async")

    with setup_step(4):
        secrets = set(cloud(cfg, "secrets", "list", "--format=value(name)").splitlines())
        if cfg["connection_secret_id"] in secrets:
            ui.reused("secret " + cfg["connection_secret_id"])
        else:
            refuse_on_update(updating, "Secret " + cfg["connection_secret_id"])
            cloud(cfg, "secrets", "create", cfg["connection_secret_id"],
                  "--replication-policy=user-managed", "--locations", cfg["region"])
            ui.created("secret " + cfg["connection_secret_id"])

    with setup_step(5):
        # The setup saves the OAuth client and the dashboard the refresh token; nothing else can read them.
        for role in ("secretAccessor", "secretVersionAdder", "secretVersionManager"):
            cloud(cfg, "secrets", "add-iam-policy-binding", cfg["connection_secret_id"],
                  "--member=serviceAccount:" + email(cfg, "mcp"),
                  "--role=roles/secretmanager." + role)
        _grant_database_role(cfg, "mcp", cfg["auth_database"], "roles/datastore.user",
                             "mcp-oauth-read-write")

    with setup_step(6):
        project = json.loads(cloud(cfg, "projects", "describe", cfg["project_id"], "--format=json"))
        _ensure_firebase_project(cfg)
        cfg = make_config(cfg["project_id"], cfg["owner_email"], cfg["region"],
                          project["projectNumber"], _ensure_web_app(cfg))
        destination = build(cfg)
        firebase_config = _write_bootstrap_firebase_config(cfg, destination)
        run([*firebase_cli(), "deploy", "--project", cfg["project_id"], "--config", firebase_config,
             "--only", "auth,firestore", "--non-interactive"], capture=False)
        _ensure_authorized_domain(cfg)

    with setup_step(7):
        _ensure_hosting_site(cfg)
        client_id = _save_google_client(cfg)
        build_connect_page(cfg, client_id, destination)
        run([*firebase_cli(), "deploy", "--project", cfg["project_id"], "--config", firebase_config,
             "--only", "hosting", "--non-interactive"], capture=False)

    with setup_step(8):
        deploy(cfg)

    with setup_step(9):
        checks = doctor(cfg)
    summarize(cfg, checks)


def summarize(cfg, checks):
    base = mcp_env(cfg)["MCP_BASE_URL"]
    created = ui.created_items or ["nothing: everything was already there"]
    rows = [("Dashboard", ui.bold(base + "/owner")),
            ("MCP address", base + "/mcp"),
            ("Project", f'{cfg["project_id"]} ({cfg["region"]})'),
            ("Owner", cfg["owner_email"]),
            *(("Created" if i == 0 else "", item) for i, item in enumerate(created)),
            ("Checks", f"{checks} of {checks} passed"),
            ("Full log", shown_path(ui.log_path))]
    ui.say()
    ui.say(ui.green("✓ Setup complete") + ui.dim(" in " + duration(time.monotonic() - ui.started)))
    ui.say()
    for label, value in rows:
        ui.say(f"  {label:<12} {value}")
    ui.say()
    ui.say(ui.bold("Next"))
    ui.say("  1. Open your dashboard (the link above) and bookmark it.")
    ui.say(f'  2. Sign in with Google as {cfg["owner_email"]}, then choose Connect Google Health.')
    ui.say("  3. Under AI apps, choose your AI app → How to connect.")
    ui.say()


def shown_path(path):
    try:
        return str(path.relative_to(ROOT))
    except (AttributeError, ValueError):
        return str(path)


def print_urls(cfg):
    base = mcp_env(cfg)["MCP_BASE_URL"]
    ui.say(f'\n  Dashboard      {base}/owner\n  MCP address    {base}/mcp\n  Project        {cfg["project_id"]}\n')


def deploy(cfg):
    dest = build(cfg)
    write_json(dest / "mcp-env.json", mcp_env(cfg))
    cloud(cfg, "run", "deploy", cfg["mcp_service"], "--source", str(dest / "mcp"),
          "--region", cfg["region"], "--service-account", email(cfg, "mcp"),
          "--env-vars-file", str(dest / "mcp-env.json"), "--allow-unauthenticated", "--ingress=all",
          "--min-instances=0", "--max-instances=2", "--memory=256Mi", "--cpu=1",
          "--concurrency=20", "--timeout=300")
    _keep_recent_images(cfg, "cloud-run-source-deploy")
    ui.line("Deployed. If the tools changed, refresh this connector's tools in your AI apps, or reconnect it.")


def doctor(cfg):
    """Checks the public endpoints and that MCP refuses anonymous callers.
    Returns how many checks passed; any failure raises."""
    base = mcp_env(cfg)["MCP_BASE_URL"]
    passed = 0
    for path in ("/health", "/.well-known/oauth-authorization-server", "/mcp"):
        try:
            with urlopen(base + path, timeout=30) as response:
                value = json.load(response)
        except HTTPError as error:
            if path == "/mcp" and error.code == 401:
                ui.line("PASS  GET /mcp without signing in → 401: MCP rejects anonymous requests")
                passed += 1
                continue
            raise RuntimeError(f"Unexpected HTTP status {error.code} from {path}") from None
        if path == "/mcp":
            raise RuntimeError("MCP unexpectedly allows anonymous access")
        if "scopes_supported" in value and value["scopes_supported"] != ["health"]:
            raise RuntimeError("Unexpected OAuth scope")
        ui.line(f"PASS  GET {path} → 200")
        passed += 1
    page = f'https://{cfg["project_id"]}.firebaseapp.com/google-connect.json'
    try:
        with urlopen(page, timeout=30) as response:
            connector = json.load(response).get("connector")
    except (HTTPError, ValueError):
        connector = None
    if connector != base:
        raise RuntimeError("The connect page on Firebase Hosting doesn't name this connector; run the setup again")
    ui.line("PASS  GET " + page + " → names this connector")
    passed += 1
    ui.line("An actual owner tool call still requires OAuth consent.")
    ui.note(f"{passed} of {passed} passed")
    return passed


def gcloud_value(name):
    try:
        value = run(["gcloud", "config", "get-value", name])
    except RuntimeError:
        return ""
    return "" if value == "(unset)" else value


def choose_project(project):
    project = project or gcloud_value("project")
    if not re.fullmatch(r"[a-z0-9:.-]+", project):
        raise RuntimeError("No project chosen. Pick your project in the guide, then run:\n"
                           "  firebase/scripts/bootstrap.sh YOUR_PROJECT_ID")
    # Select the project before signing in, so the sign-in uses it as its quota project too.
    try:
        run(["gcloud", "config", "set", "project", project, "--quiet"])
    except RuntimeError:
        pass
    ui.note(project)
    return project


def google_sign_in(owner_email):
    owner = owner_email or gcloud_value("account")
    if not owner:
        # Cloud Shell opened without trusting the repository has no credentials: sign in here.
        ui.line("Sign in with your Google account: open the link below, allow, and paste the code.")
        run(["gcloud", "auth", "login", "--update-adc"], capture=False, interactive=True)
        owner = gcloud_value("account")
        if not owner:
            raise RuntimeError("Not signed in. Run this command again and finish the Google sign-in.")
    ui.note(owner)
    return owner


def firebase_sign_in(project):
    """The Firebase CLI signs in with Application Default Credentials, separate from gcloud's."""
    try:
        run(["gcloud", "auth", "application-default", "print-access-token"], secret=True)
        ui.note("already signed in")
    except RuntimeError:
        ui.line("Sign in once more for the Firebase tools: open the link below, allow, and paste the code.")
        run(["gcloud", "auth", "application-default", "login"], capture=False, interactive=True)
        ui.note("signed in")
    # Firebase APIs reject signed-in user credentials that have no quota project.
    try:
        run(["gcloud", "auth", "application-default", "set-quota-project", project, "--quiet"])
    except RuntimeError:
        ui.line("That's fine: Cloud Shell's built-in credentials have no file to update.")
    # The Firebase CLI looks for that sign-in only in the default folder; point it at gcloud's.
    folder = run(["gcloud", "info", "--format=value(config.paths.global_config_dir)"])
    adc = Path(folder) / "application_default_credentials.json"
    if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") and folder and adc.is_file():
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(adc)
        # Also name the quota project directly, in case set-quota-project above didn't take.
        os.environ["GOOGLE_CLOUD_QUOTA_PROJECT"] = project
        ui.line("The Firebase tools will use " + str(adc))


def deploy_tools():
    """The pinned Firebase CLI sets up Firebase Authentication and Firestore rules."""
    if FIREBASE_BIN.exists():
        ui.reused("Firebase CLI in .tools/firebase-cli")
    else:
        firebase_cli()


def bootstrap_config(project, owner_email, region):
    """Returns the settings and whether this updates a connector that is already deployed."""
    try:
        run(["gcloud", "projects", "describe", project, "--format=value(projectId)", "--quiet"])
    except RuntimeError as error:
        raise RuntimeError(f"Can't open project {project} as {owner_email}. Check the project ID "
                           "(not its name or number) and that this Google account owns it.\n  "
                           + str(error)) from None
    found = find_deployment(project)
    if not found:
        return make_config(project, owner_email, region or DEFAULT_REGION), False
    if (found["owner_email"] or "").casefold() != owner_email.casefold():
        raise RuntimeError(f'This connector belongs to {found["owner_email"]}. To update it, run '
                           "this again with that email. Nothing was changed.")
    if region and region != found["region"]:
        raise RuntimeError(f'This connector runs in {found["region"]}; '
                           "Firestore cannot move regions in place. Nothing was changed.")
    return make_config(project, owner_email, found["region"]), True


def open_project(project, owner_email, region):
    cfg, updating = bootstrap_config(project, owner_email, region)
    billing = cloud(cfg, "billing", "projects", "describe", cfg["project_id"],
                    "--format=value(billingEnabled)")
    if billing != "True":
        raise RuntimeError(
            "Your project isn't on the Blaze plan yet. Upgrade it here, then run this again:\n"
            "  https://console.firebase.google.com/project/" + cfg["project_id"] + "/usage/details")
    ui.note("connector found, will update" if updating else "no connector yet")
    return cfg, updating


def confirm_bootstrap(cfg, updating, assume_yes):
    verb = "Update the" if updating else "Set up the"
    ui.say()
    ui.say(ui.bold(verb + " AI connector for Google Health"))
    ui.say(f'  Project  {cfg["project_id"]} ({cfg["region"]})')
    ui.say(f'  Owner    {cfg["owner_email"]}')
    ui.say()
    ui.say("It takes a few minutes.")
    if assume_yes:
        ui.say()
        return
    if not sys.stdin.isatty():
        raise RuntimeError("Interactive confirmation required; use --yes only after reviewing the plan")
    if ui.ask("Continue? [y/N] ").strip().lower() not in {"y", "yes"}:
        raise RuntimeError("Setup cancelled; nothing was changed")
    ui.say()


def log_path(project, action):
    if not re.fullmatch(r"[a-z0-9:.-]+", project):
        raise ValueError("Invalid project")
    return ROOT / "firebase/.build" / project / (action + ".log")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["bootstrap", "deploy", "doctor", "urls"])
    p.add_argument("--project", help="bootstrap defaults to the selected gcloud project")
    p.add_argument("--owner-email", help="bootstrap defaults to the signed-in gcloud account")
    p.add_argument("--region", help="first setup only; default " + DEFAULT_REGION)
    p.add_argument("--yes", action="store_true")
    args = p.parse_args()
    if args.action != "bootstrap" and not args.project:
        p.error("Cloud commands require --project explicitly")
    try:
        if args.action == "bootstrap":
            ui.say(ui.bold("AI connector for Google Health setup"))
            with ui.step("Choose your project"):
                project = choose_project(args.project)
            ui.open_log(log_path(project, "setup"))
            with ui.step("Sign in with Google"):
                owner = google_sign_in(args.owner_email)
            with ui.step("Sign in for the Firebase tools"):
                firebase_sign_in(project)
            with ui.step("Get the deploy tools"):
                deploy_tools()
            with ui.step("Open project " + project):
                cfg, updating = open_project(project, owner, args.region)
            confirm_bootstrap(cfg, updating, args.yes)
            provision(cfg, updating)
            return 0
        if args.action != "urls":
            ui.open_log(log_path(args.project, args.action))
        with ui.step("Find the connector in " + args.project):
            cfg = deployed_config(args.project)
            ui.note(cfg["region"])
        if args.action == "urls":
            print_urls(cfg)
        elif args.action == "deploy":
            with ui.step("Deploy the connector and dashboard"):
                ui.line("Costs: build/storage, Cloud Run usage, Firestore; minimum instances=0.")
                deploy(cfg)
        else:
            with ui.step("Check the access boundaries"):
                doctor(cfg)
        if ui.log_path:
            ui.say(ui.dim("  Full log: " + shown_path(ui.log_path)))
        return 0
    except (ValueError, RuntimeError, OSError, KeyError) as error:
        stopped(args.action, str(error))
        return 1
    except KeyboardInterrupt:
        stopped(args.action, "interrupted")
        return 130


def stopped(action, reason):
    step = ui.failed
    what = "Setup stopped" if action == "bootstrap" else "Stopped"
    if step and step.number:
        what += f" at step {step.number}/{step.total}"
    elif step:
        what += f" at “{step.title}”"
    ui.say()
    ui.say(ui.red(f"✗ {what}: ") + reason)
    if action == "bootstrap" and step:
        ui.say("  Fix the problem above, then run the same command again. Finished steps are reused.")
    if ui.log_path:
        ui.say("  Full log: " + shown_path(ui.log_path))


if __name__ == "__main__":
    raise SystemExit(main())
