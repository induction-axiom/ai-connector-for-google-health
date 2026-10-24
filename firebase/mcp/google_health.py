"""The Google Health API (v4) and Google's OAuth endpoints: the one place that knows their wire format.

Calls go to health.googleapis.com as the owner, with an access token from the refresh token
the dashboard saved. Answers are passed on as Google sends them; only failures become our
own stable codes. The API's discovery document describes every data type and request, so
nothing here lists data types; describe_data_type reads the document instead.
"""
import base64
import json
import threading
import time

import httpx

API = "https://health.googleapis.com/v4/"
DISCOVERY = "https://health.googleapis.com/$discovery/rest?version=v4"
TOKEN = "https://oauth2.googleapis.com/token"
REVOKE = "https://oauth2.googleapis.com/revoke"
# Read-only, every area but exercise GPS routes (location), which no tool reads. Every Google
# Health scope is restricted; each one the owner grants is listed on Google's consent screen.
# nutrition.readonly is missing from the discovery document, but Google accepts it.
SCOPES = tuple(f"https://www.googleapis.com/auth/googlehealth.{area}.readonly" for area in (
    "sleep", "health_metrics_and_measurements", "activity_and_fitness", "profile", "settings",
    "ecg", "irn", "mindfulness", "logged_symptoms", "reproductive_health", "nutrition"))
# The Google account that granted access, for the dashboard.
IDENTITY_SCOPES = ("openid", "email")
CONNECT_SCOPES = SCOPES + IDENTITY_SCOPES
# A new access token is fetched this long before the current one expires.
EARLY_RENEWAL = 60


class GoogleHealthError(Exception):
    """code is ours and stable; detail is Google's own text."""

    def __init__(self, code, detail=None):
        super().__init__(code)
        self.code, self.detail = code, detail


def email_from(id_token):
    """The email in an ID token that came straight from Google's token endpoint over TLS,
    which OpenID Connect allows reading without checking its signature."""
    try:
        payload = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return claims.get("email") if claims.get("email_verified") else None
    except (AttributeError, IndexError, ValueError):
        return None


class GoogleHealth:
    """connection holds the saved client and refresh token (Secret Manager in production).
    on_failure(code) hears each failure that is about Google or the connection, so the
    dashboard can show the last one. transport replaces the network in tests."""

    def __init__(self, connection, on_failure, transport=None):
        self.connection = connection
        self.on_failure = on_failure
        self.http = httpx.Client(transport=transport, timeout=30)
        self.lock = threading.Lock()
        self.token = None  # {"access_token", "expires_at", "refresh_token"}
        self.document = None

    # ---------- OAuth ----------

    def _token_request(self, form):
        try:
            response = self.http.post(TOKEN, data=form)
        except httpx.HTTPError:
            raise GoogleHealthError("google_unavailable") from None
        if response.status_code >= 500:
            raise GoogleHealthError("google_unavailable")
        answer = response.json()
        if response.status_code >= 400:
            # invalid_grant: revoked, expired (7 days while the app is in Testing), or the
            # password changed. invalid_client: the client was deleted or its secret reset.
            error = answer.get("error")
            code = {"invalid_grant": "google_health_disconnected", "invalid_client": "client_invalid",
                    "unauthorized_client": "client_invalid"}.get(error, "google_rejected")
            raise GoogleHealthError(code, answer.get("error_description") or error)
        return answer

    def exchange_code(self, client, code, redirect_uri):
        """The refresh token, granted scopes and Google account for a consent code. A code
        from Google's popup is exchanged with the origin of the page that asked for it."""
        answer = self._token_request({
            "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
            "client_id": client["client_id"], "client_secret": client["client_secret"]})
        if not answer.get("refresh_token"):
            # Google sends one only when it shows consent, which it skips for an account that
            # already granted this client access.
            raise GoogleHealthError("no_refresh_token")
        return {"refresh_token": answer["refresh_token"],
                "scopes": sorted(set(answer.get("scope", "").split()) & set(SCOPES)),
                "email": email_from(answer.get("id_token"))}

    def revoke(self, token):
        """Ask Google to forget a refresh token, and every access token made from it."""
        try:
            self.http.post(REVOKE, data={"token": token})
        except httpx.HTTPError:
            pass  # the dashboard deletes it either way
        with self.lock:
            self.token = None

    def access_token(self, renew=False):
        with self.lock:
            if self.token and not renew and self.token["expires_at"] > time.time() + EARLY_RENEWAL:
                return self.token["access_token"]
            saved = self.connection.get()
            if not saved.get("refresh_token"):
                raise GoogleHealthError("not_connected")
            answer = self._token_request({
                "grant_type": "refresh_token", "refresh_token": saved["refresh_token"],
                "client_id": saved["client_id"], "client_secret": saved["client_secret"]})
            self.token = {"access_token": answer["access_token"],
                          "expires_at": time.time() + int(answer.get("expires_in", 3600)),
                          "refresh_token": saved["refresh_token"]}
            return self.token["access_token"]

    # ---------- The Health API ----------

    def call(self, method, path, params=None, body=None):
        try:
            return self._call(method, path, params, body)
        except GoogleHealthError as error:
            # A rejected request is about its arguments, not about Google or the connection.
            if error.code not in {"request_rejected", "not_found", "permission_denied"}:
                self.on_failure(error.code)
            raise

    def _call(self, method, path, params, body):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        for attempt in range(2):
            # A token Google refuses before it expires was revoked: renew it once.
            token = self.access_token(renew=attempt > 0)
            try:
                response = self.http.request(method, API + path, params=params, json=body,
                                             headers={"Authorization": "Bearer " + token})
            except httpx.HTTPError:
                raise GoogleHealthError("google_unavailable") from None
            if response.status_code != 401:
                break
        status = response.status_code
        if status < 300:
            return response.json()
        try:
            detail = response.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            detail = None
        if status >= 500:
            raise GoogleHealthError("google_unavailable", detail)
        code = {401: "google_health_disconnected", 403: "permission_denied", 404: "not_found",
                429: "rate_limited"}.get(status, "request_rejected")
        raise GoogleHealthError(code, detail)

    def discovery(self):
        """The API's discovery document, once per instance: it changes only with the API."""
        if self.document is None:
            try:
                response = self.http.get(DISCOVERY)
            except httpx.HTTPError:
                raise GoogleHealthError("google_unavailable") from None
            if response.status_code != 200:
                raise GoogleHealthError("google_unavailable")
            self.document = response.json()
        return self.document
