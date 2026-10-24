"""Synthetic identities, a stand-in Google and a saved connection shared by the MCP tests."""
import base64
import json
from pathlib import Path
import re
import sys
from urllib.parse import parse_qs

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "firebase" / "mcp"))

from google_health import SCOPES

BASE = "https://connector.example"
CALLBACK = "https://chatgpt.com/connector_platform_oauth_redirect"
OWNER = {"uid": "synthetic-owner-id", "email": "owner@example.com"}
CLIENT = {"client_id": "123456789012-abcdefghijklmnop.apps.googleusercontent.com",
          "client_secret": "GOCSPX-synthetic-secret-123"}
# Google's discovery document for the Health API, saved on the revision it names. Every
# request the fake answers must match a method in it, with only parameters it declares.
DISCOVERY = json.loads((ROOT / "tests_mcp/discovery_v4.json").read_text())
SYSTEM_PARAMETERS = set(DISCOVERY["parameters"])


def owner_identity(token, max_age=None):
    if token == "synthetic-owner-login":
        return OWNER
    return {"uid": "stranger", "email": "stranger@example.com"}


class MemoryConnection:
    """SecretConnection's interface without Secret Manager."""

    def __init__(self, saved=None):
        self.saved = {**CLIENT, "refresh_token": "refresh-1", "scopes": list(SCOPES),
                      "email": OWNER["email"], "connected_at": 1790000000} if saved is None else saved

    def get(self):
        return dict(self.saved)

    def save(self, connection):
        self.saved = dict(connection)


def id_token(email):
    claims = base64.urlsafe_b64encode(json.dumps({"email": email, "email_verified": True}).encode())
    return "header." + claims.decode().rstrip("=") + ".signature"


def sleep_point(day, stages=8):
    """One night as the API answers it, ending on the morning of day (an ISO date)."""
    start, end = f"{day}T00:30:00Z", f"{day}T07:30:00Z"
    return {"name": f"users/me/dataTypes/sleep/dataPoints/night-{day}",
            "dataSource": {"platform": "FITBIT", "recordingMethod": "AUTOMATICALLY_RECORDED"},
            "sleep": {"type": "STAGES",
                      "interval": {"startTime": start, "endTime": end, "startUtcOffset": "-14400s",
                                   "endUtcOffset": "-14400s"},
                      "stages": [{"type": ("LIGHT", "DEEP", "REM", "AWAKE")[i % 4],
                                  "startTime": start, "endTime": end, "startUtcOffset": "-14400s",
                                  "endUtcOffset": "-14400s"} for i in range(stages)],
                      "summary": {"minutesAsleep": "390", "minutesAwake": "30", "minutesInSleepPeriod": "420"},
                      "metadata": {"mainSleep": True, "nap": False, "processed": True}}}


class FakeGoogle:
    """Answers like Google's token endpoint and the Health API, and records each request."""

    def __init__(self):
        self.requests = []
        self.token_requests = []
        self.revoked = set()
        self.access = {}  # access token → refresh token
        self.down = False
        self.unlinked = False
        self.granted = list(SCOPES)
        self.nights = [sleep_point(f"2026-10-{d:02d}") for d in range(20, 10, -1)]
        self.heart_points = 0

    @property
    def transport(self):
        return httpx.MockTransport(self.handle)

    def handle(self, request):
        if request.url.host == "oauth2.googleapis.com":
            return self.oauth(request)
        assert request.url.host == "health.googleapis.com", request.url
        if request.url.path == "/$discovery/rest":
            return httpx.Response(503) if self.down else httpx.Response(200, json=DISCOVERY)
        return self.api(request)

    # ---------- OAuth ----------

    def oauth(self, request):
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        if request.url.path == "/revoke":
            self.revoked.add(form["token"])
            self.access = {a: r for a, r in self.access.items() if r != form["token"]}
            return httpx.Response(200)
        self.token_requests.append(form)
        if self.down:
            return httpx.Response(503)
        if (form.get("client_id"), form.get("client_secret")) != (CLIENT["client_id"], CLIENT["client_secret"]):
            return httpx.Response(401, json={"error": "invalid_client"})
        if form["grant_type"] == "authorization_code":
            if form.get("code") != "good-code" or not form.get("code_verifier"):
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Bad Request"})
            refresh = "refresh-" + str(len(self.token_requests) + 1)
            return httpx.Response(200, json={
                "access_token": self.issue(refresh), "expires_in": 3599, "refresh_token": refresh,
                "scope": " ".join(self.granted + ["openid", "https://www.googleapis.com/auth/userinfo.email"]),
                "token_type": "Bearer", "id_token": id_token(OWNER["email"])})
        if form["grant_type"] == "refresh_token":
            if form["refresh_token"] in self.revoked:
                return httpx.Response(400, json={"error": "invalid_grant",
                                                 "error_description": "Token has been expired or revoked."})
            return httpx.Response(200, json={"access_token": self.issue(form["refresh_token"]),
                                             "expires_in": 3599, "token_type": "Bearer"})
        raise AssertionError(form)

    def issue(self, refresh):
        token = "access-" + str(len(self.access) + 1)
        self.access[token] = refresh
        return token

    # ---------- The Health API ----------

    def contract(self, request, path):
        """The discovery method this request calls; fails the test if there is none."""
        params = set(request.url.params)
        for resource in walk(DISCOVERY):
            for name, method in resource.get("methods", {}).items():
                pattern = re.sub(r"\\?\{[A-Za-z]+\\?\}", "[^/:]+", re.escape(method["flatPath"]))
                if method["httpMethod"] == request.method and re.fullmatch(pattern, path):
                    declared = set(method.get("parameters", {})) | SYSTEM_PARAMETERS
                    assert params <= declared, (name, params - declared)
                    return name
        raise AssertionError(f"No API method for {request.method} {path}")

    def error(self, code, status, message):
        return httpx.Response(code, json={"error": {"code": code, "message": message, "status": status}})

    def api(self, request):
        path = request.url.path.lstrip("/")
        name = self.contract(request, path)
        body = json.loads(request.content) if request.content else None
        self.requests.append({"method": request.method, "path": path, "params": dict(request.url.params),
                              "body": body, "api_method": name})
        if self.down:
            return self.error(503, "UNAVAILABLE", "The service is currently unavailable.")
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        if token not in self.access:
            return self.error(401, "UNAUTHENTICATED", "Request had invalid authentication credentials.")
        if self.unlinked:
            return self.error(403, "PERMISSION_DENIED", "The user has no Google Health profile.")
        if path == "v4/users/me/identity":
            return httpx.Response(200, json={"name": "users/me/identity", "healthUserId": "1234567890"})
        if path == "v4/users/me/pairedDevices":
            return httpx.Response(200, json={"pairedDevices": [{"name": "users/me/pairedDevices/d1",
                "deviceVersion": "Pixel Watch 4", "batteryLevel": 80, "lastSyncTime": "2026-10-20T08:00:00Z"}]})
        found = re.fullmatch(r"v4/users/me/dataTypes/([a-z0-9-]+)/dataPoints(:\w+)?", path)
        if not found:
            return self.error(404, "NOT_FOUND", "Requested entity was not found.")
        data_type, verb = found[1], found[2]
        if data_type not in {"sleep", "heart-rate", "steps"}:
            return self.error(400, "INVALID_ARGUMENT", f"Unknown data type {data_type}.")
        if verb in {":rollUp", ":dailyRollUp"}:
            return httpx.Response(200, json={"rollupDataPoints": [
                {"startTime": body["range"].get("startTime"), "heartRate": {"beatsPerMinuteAvg": 61.5}}]})
        size = int(request.url.params.get("pageSize", 1440))
        if data_type == "sleep":
            if size > 25:
                return self.error(400, "INVALID_ARGUMENT", "Page size for sleep must be at most 25.")
            start = int(request.url.params.get("pageToken", 0))
            page = self.nights[start:start + size]
            answer = {"dataPoints": page}
            if start + size < len(self.nights):
                answer["nextPageToken"] = str(start + size)
            return httpx.Response(200, json=answer)
        points = [{"dataSource": {"platform": "FITBIT"}, "heartRate": {
            "beatsPerMinute": "62", "sampleTime": {"physicalTime": f"2026-10-20T03:{i // 60 % 60:02d}:{i % 60:02d}Z",
                                                   "utcOffset": "-14400s"}}}
                  for i in range(min(size, self.heart_points))]
        return httpx.Response(200, json={"dataPoints": points})


def walk(resource):
    yield resource
    for child in resource.get("resources", {}).values():
        yield from walk(child)
