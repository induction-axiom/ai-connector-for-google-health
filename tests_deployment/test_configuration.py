import base64
import json
from urllib.error import HTTPError
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "firebase/scripts"))
from configuration import make_config, mcp_env
import manage
from manage import (bootstrap_config, build, build_connect_page, provision,
                    _save_google_client, _write_bootstrap_firebase_config)

WEB = {"projectId": "your-project", "apiKey": "public-key",
       "authDomain": "your-project.firebaseapp.com", "appId": "app-id"}


def service(owner="owner@example.com", region="us-central1"):
    """The shape `gcloud run services list --format=json` returns, trimmed to what is read."""
    env = [{"name": "MCP_OWNER_EMAIL", "value": owner},
           {"name": "FIREBASE_WEB_CONFIG", "value": json.dumps(WEB)}]
    return {"metadata": {"labels": {"cloud.googleapis.com/location": region}},
            "spec": {"template": {"spec": {"containers": [{"env": env}]}}}}


def fake_gcloud(services, apis="run.googleapis.com"):
    def run(args, **_):
        if args[:3] == ["gcloud", "services", "list"]:
            return apis
        if args[:4] == ["gcloud", "run", "services", "list"]:
            return json.dumps(services)
        if args[:3] == ["gcloud", "projects", "describe"]:
            return "123456"
        raise AssertionError(args)
    return run


class DeploymentTests(unittest.TestCase):
    def config(self, region="us-east4", **updates):
        return make_config("your-project", updates.get("owner_email", "owner@example.com"),
                           region, "123456", WEB)

    def test_parameters_flow_to_the_service(self):
        mcp = mcp_env(self.config(region="us-central1"))
        self.assertIn("us-central1.run.app", mcp["MCP_BASE_URL"])
        self.assertEqual(mcp["CONNECTION_SECRET_ID"], "google-health-connection")
        self.assertEqual(mcp["MCP_AUTH_DATABASE"], "(default)")
        self.assertEqual(json.loads(mcp["FIREBASE_WEB_CONFIG"]), WEB)

    def test_reject_invalid_inputs(self):
        for args in [("--unexpected-option", "owner@example.com", "us-central1"),
                     ("your-project", "not-an-email", "us-central1"),
                     ("your-project", "owner@example.com", "us-central1\nSECRET=bad")]:
            with self.subTest(args=args):
                with self.assertRaises(ValueError): make_config(*args)
        with self.assertRaises(ValueError):
            make_config("your-project", "owner@example.com", "us-central1", "123",
                        {**WEB, "projectId": "someone-else"})

    def test_bundle_holds_only_allowlisted_sources_and_starts_clean(self):
        cfg = self.config()
        dest = build(cfg)
        stray = dest / "mcp/accidental-secret.json"
        stray.write_text('{"synthetic_secret":"never-upload"}')
        build(cfg)
        self.assertFalse(stray.exists())
        files = {str(f.relative_to(dest)) for f in dest.rglob("*")
                 if f.is_file() and "venv" not in f.parts}
        self.assertIn("mcp/google_health.py", files)
        self.assertIn("mcp/static/owner.js", files)
        self.assertFalse(any(f.startswith("functions/") for f in files))
        version = json.loads((dest / "mcp/version.json").read_text())
        self.assertRegex(version["version"], r"^\d+\.\d+\.\d+$")
        self.assertTrue(all("private" not in f and "__pycache__" not in f for f in files))
        self.assertEqual({f for f in files if ".env" in f}, set())
        # The MCP container runs as a non-root user; it must be able to read everything uploaded.
        for path in (dest / "mcp").rglob("*"):
            if path.name not in {"mcp-env.json", ".gcloudignore"}:
                needed = 0o005 if path.is_dir() else 0o004
                self.assertEqual(path.stat().st_mode & needed, needed, path)

    def test_bootstrap_uses_fixed_names(self):
        with patch("manage.run", fake_gcloud([])):
            cfg, updating = bootstrap_config("fresh-project-123", "owner@example.com", None)
        self.assertFalse(updating)
        self.assertEqual(cfg["region"], "us-east4")
        self.assertEqual(cfg["auth_database"], "(default)")
        self.assertEqual(cfg["mcp_service"], "google-health-mcp")
        self.assertEqual(cfg["connection_secret_id"], "google-health-connection")

    def test_a_project_without_cloud_run_turned_on_is_a_first_setup(self):
        with patch("manage.run", fake_gcloud(None, apis="firestore.googleapis.com")):
            _cfg, updating = bootstrap_config("fresh-project-123", "owner@example.com", None)
        self.assertFalse(updating)

    def test_update_finds_the_deployed_connector_without_local_files(self):
        with patch("manage.run", fake_gcloud([service(owner="Owner@Example.com")])):
            cfg, updating = bootstrap_config("your-project", "owner@example.com", None)
            deployed = manage.deployed_config("your-project")
        self.assertTrue(updating)
        self.assertEqual(cfg["region"], "us-central1")
        self.assertEqual(deployed["firebase_web_config"], WEB)
        self.assertEqual(mcp_env(deployed)["MCP_BASE_URL"],
                         "https://google-health-mcp-123456.us-central1.run.app")

    def test_update_refuses_another_owner_or_region(self):
        with patch("manage.run", fake_gcloud([service(owner="someone@example.com")])):
            with self.assertRaisesRegex(RuntimeError, "belongs to someone@example.com"):
                bootstrap_config("your-project", "owner@example.com", None)
        with patch("manage.run", fake_gcloud([service()])):
            with self.assertRaisesRegex(RuntimeError, "runs in us-central1"):
                bootstrap_config("your-project", "owner@example.com", "europe-west1")

    def test_deploy_without_a_connector_points_to_bootstrap(self):
        with patch("manage.run", fake_gcloud([])):
            with self.assertRaisesRegex(RuntimeError, "bootstrap.sh"):
                manage.deployed_config("your-project")

    def test_update_never_recreates_a_missing_identity(self):
        """A missing service account on update stops before anything is created."""
        calls = []

        def cloud(cfg, *args):
            calls.append(args)
            if args[:2] == ("projects", "describe"):
                return json.dumps({"projectNumber": "123456"})
            return "True" if args[0] == "billing" else ""
        with patch("manage.cloud", cloud):
            with self.assertRaisesRegex(RuntimeError, "Service account .* is missing"):
                provision(self.config(), updating=True)
        self.assertFalse([c for c in calls if "create" in c])

    def test_bootstrap_firebase_config_enables_only_google_and_closed_rules(self):
        cfg = self.config()
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            path = _write_bootstrap_firebase_config(cfg, destination)
            value = json.loads(path.read_text())
            providers = value["auth"]["providers"]
            self.assertEqual(set(providers), {"googleSignIn"})
            self.assertEqual(providers["googleSignIn"]["supportEmail"], "owner@example.com")
            # Firebase adds the default handler itself; repeating it is rejected as a duplicate.
            self.assertNotIn("authorizedRedirectUris", providers["googleSignIn"])
            self.assertEqual([item["database"] for item in value["firestore"]], ["(default)"])
            self.assertIn("allow read, write: if false", (destination / "firestore.rules").read_text())


class GoogleClientTests(unittest.TestCase):
    """The setup copies Firebase's Google sign-in client into the connection secret."""
    FIREBASE_CLIENT = {"clientId": "123-abc.apps.googleusercontent.com", "clientSecret": "synthetic-secret"}

    def setUp(self):
        self.cfg = make_config("your-project", "owner@example.com", "us-east4", "123456", WEB)
        self.saved = None
        self.calls = []

    def google_api(self, cfg, url, body=None, method=None):
        self.calls.append(url)
        if "defaultSupportedIdpConfigs" in url:
            return self.FIREBASE_CLIENT
        if url.endswith("/versions/latest:access"):
            if self.saved is None:
                raise HTTPError(url, 404, "Not Found", {}, None)
            return {"payload": {"data": base64.b64encode(json.dumps(self.saved).encode()).decode()}}
        if url.endswith("/versions?filter=state:ENABLED"):
            return {"versions": [{"name": "projects/123456/secrets/google-health-connection/versions/1"}]}
        if url.endswith(":addVersion"):
            self.saved = json.loads(base64.b64decode(body["payload"]["data"]))
            return {}
        if url.endswith(":destroy"):
            return {}
        raise AssertionError(url)

    def save(self):
        with patch("manage.google_api", self.google_api):
            return _save_google_client(self.cfg)

    def test_first_setup_saves_the_client(self):
        self.assertEqual(self.save(), "123-abc.apps.googleusercontent.com")
        self.assertEqual(self.saved, {"client_id": "123-abc.apps.googleusercontent.com",
                                      "client_secret": "synthetic-secret"})
        self.assertTrue(any(url.endswith(":destroy") for url in self.calls))

    def test_rerun_keeps_access_for_the_same_client(self):
        self.saved = {"client_id": "123-abc.apps.googleusercontent.com", "client_secret": "synthetic-secret",
                      "refresh_token": "kept"}
        self.save()
        self.assertFalse(any(url.endswith(":addVersion") for url in self.calls))
        self.saved["client_secret"] = "old-secret"
        self.save()
        self.assertEqual((self.saved["refresh_token"], self.saved["client_secret"]), ("kept", "synthetic-secret"))

    def test_another_client_drops_access_made_for_the_old_one(self):
        self.saved = {"client_id": "old.apps.googleusercontent.com", "client_secret": "x", "refresh_token": "old"}
        self.save()
        self.assertNotIn("refresh_token", self.saved)

    def test_connect_page_names_this_connector_and_client(self):
        with tempfile.TemporaryDirectory() as directory:
            hosting = build_connect_page(self.cfg, "123-abc.apps.googleusercontent.com", Path(directory))
            self.assertEqual({f.name for f in hosting.iterdir()},
                             {"google-connect.html", "google-connect.js", "google-connect.json", "style.css"})
            self.assertEqual(json.loads((hosting / "google-connect.json").read_text()),
                             {"connector": "https://google-health-mcp-123456.us-east4.run.app",
                              "client_id": "123-abc.apps.googleusercontent.com"})

    def test_connect_page_may_talk_only_to_google_and_the_connector(self):
        with tempfile.TemporaryDirectory() as directory:
            value = json.loads(_write_bootstrap_firebase_config(self.cfg, Path(directory)).read_text())
            self.assertTrue((Path(directory) / "hosting").is_dir())
        headers = {h["key"]: h["value"] for h in value["hosting"]["headers"][0]["headers"]}
        policy = headers["Content-Security-Policy"]
        self.assertIn("connect-src 'self' https://accounts.google.com/gsi/ "
                      "https://google-health-mcp-123456.us-east4.run.app;", policy)
        self.assertIn("default-src 'none'", policy)
        self.assertEqual(headers["Cross-Origin-Opener-Policy"], "same-origin-allow-popups")

