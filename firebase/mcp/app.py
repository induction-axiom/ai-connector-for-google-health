"""Owner-authorized MCP over the owner's Google Health data, plus the dashboard."""
import json
import logging
from pathlib import Path
import time
from urllib.parse import parse_qs, urlencode, urlsplit

import anyio
import firebase_admin
from firebase_admin import auth as firebase_auth
from mcp.server import MCPServer
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Route

from config import Config
from connection import SecretConnection
from google_health import CONNECT_SCOPES, GoogleHealth, GoogleHealthError
from oauth import ConsentError, Provider, key, random_token
from store import FirestoreStore
import tools

STATIC = Path(__file__).parent / "static"
CONSENT_AUTH_AGE = 300
OWNER_AUTH_AGE = 12 * 3600
COOKIE = "__Host-mcp-consent"
# Written by the deploy step (version from pyproject.toml, commit from git); absent in tests.
BUILD_FILE = Path(__file__).parent / "version.json"
BUILD = json.loads(BUILD_FILE.read_text()) if BUILD_FILE.exists() else {"version": "dev"}
# Where releases and source live; the dashboard checks it for updates. A fork changes this.
REPOSITORY = "induction-axiom/ai-connector-for-google-health"
VERSION = BUILD["version"]


def verify_owner(raw, config, max_age=CONSENT_AUTH_AGE):
    """The owner's identity from a recent Google sign-in. A sign-in that is invalid or too old
    raises owner_google_login_required; a good one by someone else raises not_owner."""
    try:
        decoded = firebase_auth.verify_id_token(raw)
        if (decoded.get("email_verified") is not True
                or decoded.get("firebase", {}).get("sign_in_provider") != "google.com"
                or not decoded.get("uid")
                or time.time() - decoded.get("auth_time", 0) > max_age):
            raise ValueError
    except Exception:
        raise ConsentError("owner_google_login_required") from None
    if decoded.get("email", "").casefold() != config.owner_email:
        raise ConsentError("not_owner")
    return {"uid": decoded["uid"], "email": decoded["email"].casefold()}


RATE_LIMITS = {"/register": 12, "/authorize": 30, "/consent/start": 30, "/consent/finish": 30,
               "/token": 60, "/owner/status": 60, "/owner/google/start": 10, "/google/exchange": 20,
               "/owner/google/disconnect": 10, "/owner/preview": 30, "/owner/apps/disconnect": 10}
MAX_QUERY_BYTES = 4096
MAX_BODY_BYTES = 16384
# The dashboard shows when Google last failed a request, and why.
LAST_FAILURE = "google_last_failure"
# The connect page has this long to bring back Google's answer.
CONNECT_SECONDS = 600

class Boundary:
    """ASGI wrapper in front of every route: reject bad requests early, patch the few
    OAuth gaps the MCP SDK leaves, add security headers, and hide internal errors."""

    def __init__(self, app, provider):
        self.app, self.provider = app, provider
        self.config = provider.config

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope, receive)
        path = request.url.path

        async def reply(body, status):
            await JSONResponse(body, status)(scope, receive, send)

        problem = self.reject(request, path, len(scope.get("query_string", b"")))
        if problem:
            return await reply(*problem)
        body = await self.read_body(receive)
        if body is None:
            return  # client disconnected
        if len(body) > MAX_BODY_BYTES:
            return await reply({"error": "request_too_large"}, 413)
        if path in {"/authorize", "/token", "/revoke"}:
            raw = scope.get("query_string", b"") if request.method == "GET" else body
            params, problem = self.oauth_params(request.method, path, raw)
            if problem:
                return await reply(*problem)
            if path == "/revoke" and request.method == "POST" and "client_secret" not in params:
                body, scope = self.add_empty_client_secret(params, scope)

        body_copy = bytes(body)

        async def replay():
            nonlocal body
            if body is not None:
                message, body = {"type": "http.request", "body": bytes(body), "more_body": False}, None
                return message
            return await receive()

        started, status, answer = False, None, bytearray()

        async def safe_send(message):
            nonlocal started, status
            if message["type"] == "http.response.start":
                started, status = True, message["status"]
                message = {**message, "headers": self.response_headers(path, message.get("headers", []))}
            elif path == "/register" and len(answer) < MAX_BODY_BYTES:
                answer.extend(message.get("body", b""))
            await send(message)

        try:
            if request.method != "OPTIONS" and path in RATE_LIMITS:
                group = path.strip("/").replace("/", "_")
                if not await self.provider.rate_limit(group, RATE_LIMITS[path]):
                    return await JSONResponse({"error": "rate_limited"}, 429)(scope, replay, safe_send)
            await self.app(scope, replay, safe_send)
            if path == "/register" and request.method == "POST" and status == 400:
                logging.warning("register_rejected %s", self.registration_summary(body_copy, answer))
        except Exception:
            logging.error("mcp_request_failed")
            if not started:
                await JSONResponse({"error": "service_unavailable"}, 503)(scope, replay, safe_send)

    @staticmethod
    def registration_summary(body, answer):
        """What a rejected client asked for, so a new AI app's needs show in the logs.
        Registration metadata holds no credentials; callbacks lose their query strings."""
        def short(value, limit=160):
            return value[:limit] if isinstance(value, str) else None
        try:
            asked = json.loads(body)
            asked = asked if isinstance(asked, dict) else {}
        except ValueError:
            asked = {}
        try:
            error = json.loads(answer)
            error = error if isinstance(error, dict) else {}
        except ValueError:
            error = {}
        uris = asked.get("redirect_uris")
        uris = uris if isinstance(uris, list) else []
        lists = {k: [short(v, 40) for v in asked[k][:5]] if isinstance(asked.get(k), list) else None
                 for k in ("grant_types", "response_types")}
        return json.dumps({
            "error": short(error.get("error")), "detail": short(error.get("error_description")),
            "client_name": short(asked.get("client_name"), 60),
            "token_endpoint_auth_method": short(asked.get("token_endpoint_auth_method"), 40),
            "scope": short(asked.get("scope")), **lists,
            "redirect_count": len(uris),
            "redirects": [short(u.split("?", 1)[0].split("#", 1)[0]) for u in uris[:10] if isinstance(u, str)],
        })

    def reject(self, request, path, query_bytes):
        base = self.config.base_url
        if request.headers.get("host") != urlsplit(base).netloc:
            return {"error": "invalid_host"}, 400
        # Our browser endpoints only accept same-origin calls; OAuth wire endpoints keep
        # the SDK's CORS behaviour for MCP clients.
        if path.startswith(("/consent/", "/owner/")) and request.headers.get("origin") != base:
            return {"error": "invalid_origin"}, 403
        # The one cross-origin call: the connect page on Firebase Hosting, with Google's answer.
        if path == "/google/exchange" and request.headers.get("origin") != self.config.connect_origin:
            return {"error": "invalid_origin"}, 403
        if query_bytes > MAX_QUERY_BYTES:
            return {"error": "request_too_large"}, 413
        return None

    @staticmethod
    async def read_body(receive):
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return None
            body.extend(message.get("body", b""))
            if len(body) > MAX_BODY_BYTES or not message.get("more_body"):
                return body

    def oauth_params(self, method, path, raw):
        """Reject repeated OAuth parameters, and bind /token to our one MCP resource."""
        try:
            params = parse_qs(raw.decode(), keep_blank_values=True, max_num_fields=20)
            if any(len(v) != 1 for v in params.values()):
                raise ValueError
        except (UnicodeError, ValueError):
            if path == "/token":
                logging.warning("token_rejected invalid_request repeated_or_unreadable_params")
            return None, ({"error": "invalid_request"}, 400)
        # The SDK checks PKCE and callbacks but does not bind the token request's resource.
        # It may be left out (RFC 8707 §2.2): the code or refresh token is already bound to this endpoint.
        resource = params.get("resource")
        if path == "/token" and method == "POST" and resource is not None and resource != [self.config.resource]:
            logging.warning("token_rejected invalid_target grant_type=%s resource=other",
                            (params.get("grant_type") or ["missing"])[0][:40])
            return None, ({"error": "invalid_target"}, 400)
        return params, None

    @staticmethod
    def add_empty_client_secret(params, scope):
        """SDK revocation requires client_secret, which public PKCE clients do not have.
        Fill in an empty one; the SDK still checks the client and token ownership."""
        body = bytearray(urlencode({**{k: v[0] for k, v in params.items()}, "client_secret": ""}).encode())
        headers = [(k, v) for k, v in scope["headers"] if k != b"content-length"]
        return body, {**scope, "headers": headers + [(b"content-length", str(len(body)).encode())]}

    def response_headers(self, path, headers):
        base = self.config.base_url
        pairs = list(headers)
        # RFC 9207: authorization error redirects also carry the issuer.
        if path == "/authorize":
            for i, (name, value) in enumerate(pairs):
                if name == b"location" and self.provider.valid_redirect(value.decode().split("?", 1)[0]):
                    url = value.decode()
                    pairs[i] = (name, (url + ("&" if "?" in url else "?") + urlencode({"iss": base})).encode())
        pairs += [(b"cache-control", b"no-store"), (b"referrer-policy", b"no-referrer"),
                  (b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY")]
        if path == "/google/exchange":
            pairs += [(b"access-control-allow-origin", self.config.connect_origin.encode()),
                      (b"access-control-allow-methods", b"POST"),
                      (b"access-control-allow-headers", b"content-type, x-requested-with"),
                      (b"access-control-max-age", b"600"), (b"vary", b"origin")]
        if path in {"/consent", "/owner"}:
            domain = self.config.firebase_config.get("authDomain", "")
            policy = ("default-src 'none'; script-src 'self' https://www.gstatic.com https://apis.google.com; "
                      "style-src 'self'; connect-src 'self' https://api.github.com https://identitytoolkit.googleapis.com "
                      "https://securetoken.googleapis.com https://www.googleapis.com; "
                      f"frame-src https://{domain}; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
            pairs += [(b"content-security-policy", policy.encode()),
                      (b"cross-origin-opener-policy", b"same-origin-allow-popups")]
        return pairs


def create_app(config, store, owner_verifier, connection, transport=None):
    """connection holds the saved Google client and refresh token (Secret Manager in
    production); transport replaces the network to Google in tests."""
    provider = Provider(config, store)
    verifier = owner_verifier or (lambda token, max_age=CONSENT_AUTH_AGE:
                                  verify_owner(token, config, max_age))
    owner_url = config.base_url + "/owner"

    def remember_failure(code):
        store.transact([LAST_FAILURE], lambda _: (None, {LAST_FAILURE: {"code": code, "at": time.time()}}))

    health = GoogleHealth(connection, remember_failure, transport)

    server = MCPServer(
        "Private AI connector for Google Health",
        version=VERSION,
        instructions=("Read-only access to the owner's Google Health data, through the Google "
            "Health API v4. Answers are Google's own JSON, unchanged. Before reading a data type "
            "for the first time, call describe_data_type for its schema, filter syntax and notes. "
            "Prefer reconcile_data_points for sleep and anything several devices record, and "
            "roll-ups for dense data such as heart-rate. An answer too large to read comes back "
            "as result_too_large: ask for less. When owner_url is present with next_action "
            "connect_google_health or tell_owner, give the owner that exact clickable link."),
        auth_server_provider=provider,
        auth=AuthSettings(issuer_url=config.base_url, resource_server_url=config.resource,
            validate_token_resource=True, required_scopes=[config.scope],
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=[config.scope], default_scopes=[config.scope]),
            revocation_options=RevocationOptions(enabled=True)),
        log_level="WARNING",
    )

    # Which code is running, and where to read it, for when something needs debugging.
    connector = {"version": VERSION, "commit": BUILD.get("commit"),
                 "source_code": f"https://github.com/{REPOSITORY}/tree/{BUILD.get('commit') or 'main'}",
                 "known_issues": f"https://github.com/{REPOSITORY}/issues"}
    security = {"securitySchemes": [{"type": "oauth2", "scopes": [config.scope]}]}
    previews = tools.register(server, health, owner_url, connector, security)

    # ---------- Public pages and OAuth consent ----------

    async def health_page(request):
        return JSONResponse({"service": "ai-connector-for-google-health", "connector_version": VERSION})

    async def metadata(request):
        return JSONResponse({
            "issuer": config.base_url, "authorization_response_iss_parameter_supported": True,
            "authorization_endpoint": config.base_url + "/authorize",
            "token_endpoint": config.base_url + "/token",
            "registration_endpoint": config.base_url + "/register",
            "revocation_endpoint": config.base_url + "/revoke",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"],
            "revocation_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"], "scopes_supported": [config.scope]})

    async def consent_page(request):
        return FileResponse(STATIC / "consent.html")

    async def owner_page(request):
        return FileResponse(STATIC / "owner.html")

    async def asset(request):
        name = request.path_params["name"]
        if name not in {"consent.js", "owner.js", "style.css"}:
            return JSONResponse({"error": "not_found"}, 404)
        return FileResponse(STATIC / name)

    async def public_config(request):
        return JSONResponse(config.firebase_config)

    async def consent_start(request):
        try:
            data = await request.json()
            ticket = data.get("request", "")
            if not isinstance(ticket, str) or not 32 <= len(ticket) <= 128:
                raise ConsentError("invalid_request")
            view, browser = await provider.start_consent(ticket, request.cookies.get(COOKIE))
            response = JSONResponse(view)
            response.set_cookie(COOKIE, browser, max_age=600, path="/", secure=True,
                                httponly=True, samesite="strict")
            return response
        except (ConsentError, ValueError, TypeError, AttributeError):
            return JSONResponse({"error": "authorization_expired_or_invalid"}, 400)

    async def consent_finish(request):
        try:
            data = await request.json()
            if data.get("decision") not in {"approve", "deny"}:
                raise ConsentError("invalid_decision")
            raw = data.get("id_token")
            if not isinstance(raw, str) or not 10 <= len(raw) <= 12000:
                raise ConsentError("invalid_identity")
            owner = await anyio.to_thread.run_sync(verifier, raw)
            callback = await provider.finish_consent(data.get("request", ""),
                request.cookies.get(COOKIE), data.get("csrf", ""), owner,
                approve=data["decision"] == "approve")
            response = JSONResponse({"redirect": callback})
            response.delete_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="strict")
            return response
        except (ConsentError, ValueError, TypeError, AttributeError):
            return JSONResponse({"error": "owner_login_or_consent_invalid"}, 403)

    # ---------- Dashboard API ----------

    def owner_endpoint(handler):
        """Require the owner's Google sign-in, pass the JSON body, and map bad input to 400.
        A sign-in that has grown old says so, apart from another account's: the dashboard
        asks the first to sign in again, and tells the second it isn't the owner."""
        async def endpoint(request):
            authorization = request.headers.get("authorization", "")
            token = authorization.removeprefix("Bearer ")
            owner = None
            if authorization.startswith("Bearer ") and 10 <= len(token) <= 12000:
                try:
                    owner = await anyio.to_thread.run_sync(verifier, token, OWNER_AUTH_AGE)
                except ConsentError as error:
                    if str(error) == "not_owner":
                        return JSONResponse({"error": "not_owner"}, 403)
            if not owner:
                return JSONResponse({"error": "owner_login_required"}, 403)
            if owner.get("email", "").casefold() != config.owner_email:
                return JSONResponse({"error": "not_owner"}, 403)
            try:
                body = await request.json()
                if not isinstance(body, dict):
                    raise ValueError
                return await handler(body)
            except (ValueError, TypeError, AttributeError):
                return JSONResponse({"error": "request_invalid"}, 400)
        return endpoint

    def check_connection():
        """The saved connection, and whether Google Health answers for it: the dashboard's live status."""
        saved = connection.get()
        if not saved.get("client_id"):
            return {"state": "no_client"}
        facts = {"client_id": saved["client_id"]}
        if not saved.get("refresh_token"):
            return {"state": "not_connected", **facts}
        facts.update({"email": saved.get("email"), "connected_at": saved.get("connected_at")})
        try:
            health.call("GET", "users/me/identity")
            return {"state": "connected", **facts}
        except GoogleHealthError as error:
            return {"state": error.code, "detail": error.detail, **facts}

    @owner_endpoint
    async def owner_status(body):
        return JSONResponse({"google": await anyio.to_thread.run_sync(check_connection),
                             "last_failure": await anyio.to_thread.run_sync(store.get, LAST_FAILURE),
                             "apps": await anyio.to_thread.run_sync(provider.connected_apps),
                             "diagnostics": {"connector_version": VERSION,
                                             "commit": BUILD.get("commit"),
                                             "repository": REPOSITORY,
                                             "committed_on": BUILD.get("committed_on"),
                                             "auth_database": config.database,
                                             "connection_secret": config.connection_secret,
                                             "project_id": config.project_id,
                                             "mcp_endpoint": config.resource}})

    def forget_token(saved):
        """Revoke the saved refresh token at Google, and keep only the client."""
        if saved.get("refresh_token"):
            health.revoke(saved["refresh_token"])
        connection.save({"client_id": saved["client_id"], "client_secret": saved["client_secret"]})

    @owner_endpoint
    async def owner_google_start(body):
        """Where to send the owner to allow access: the connect page on Firebase Hosting,
        with a one-time state that Google's answer must come back with within ten minutes.
        Access saved before is revoked first: Google sends a refresh token only with a fresh
        consent, and its popup can't ask for one."""
        saved = await anyio.to_thread.run_sync(connection.get)
        if not saved.get("client_id"):
            return JSONResponse({"result": "no_client"})
        if saved.get("refresh_token"):
            await anyio.to_thread.run_sync(forget_token, saved)
        state = random_token()
        record = key("google_connect", state)
        await anyio.to_thread.run_sync(store.transact, [record], lambda _: (None, {record: {
            "client_id": saved["client_id"], "purge_at": time.time() + CONNECT_SECONDS}}))
        # In the fragment, so the state never reaches a server log. Where the page sends
        # Google's answer, and which client asks, are fixed when the setup deploys it.
        return JSONResponse({"result": "redirect", "url": config.connect_page + "#" + urlencode({
            "state": state, "scope": " ".join(CONNECT_SCOPES)})})

    def finish_google(state, code):
        """Exchange Google's code once, for a state the dashboard handed out, and save the refresh token."""
        record = key("google_connect", state)

        def take(current):
            return current[record], {record: {"purge_at": time.time()}}  # one use only
        found = store.transact([record], take)
        if not found or "client_id" not in found or found["purge_at"] < time.time():
            return "expired"
        saved = connection.get()
        if saved.get("client_id") != found["client_id"]:
            return "expired"  # the client changed while the owner was at Google
        try:
            granted = health.exchange_code(saved, code, config.connect_origin)
        except GoogleHealthError as error:
            return error.code
        connection.save({"client_id": saved["client_id"], "client_secret": saved["client_secret"],
                         **granted, "connected_at": time.time()})
        return "connected"

    async def google_exchange(request):
        """Google's answer, from the connect page. The custom header keeps it to scripts
        that passed the CORS preflight, and the state to the dashboard that asked."""
        if request.method == "OPTIONS":
            return Response(status_code=204)
        try:
            data = await request.json()
            state, code = data["state"], data["code"]
            if (request.headers.get("x-requested-with") != "XmlHttpRequest"
                    or not isinstance(state, str) or not 32 <= len(state) <= 128
                    or not isinstance(code, str) or not 10 <= len(code) <= 2048):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            return JSONResponse({"error": "request_invalid"}, 400)
        return JSONResponse({"outcome": await anyio.to_thread.run_sync(finish_google, state, code)})

    @owner_endpoint
    async def owner_google_disconnect(body):
        saved = await anyio.to_thread.run_sync(connection.get)
        if saved.get("client_id"):
            await anyio.to_thread.run_sync(forget_token, saved)
        return JSONResponse({"result": "disconnected"})

    @owner_endpoint
    async def owner_preview(body):
        """Show exactly what an AI tool returns; this reads Google Health live."""
        target = body.get("target")
        if target not in previews:
            return JSONResponse({"error": "preview_target_invalid"}, 400)
        tool = previews[target]
        arguments = {"reconcile_data_points": {"data_type": "sleep", "page_size": 1},
                     "get_resource": {"name": "users/me/identity"}}.get(target, {})
        result = await anyio.to_thread.run_sync(lambda: tool(**arguments))
        return JSONResponse(json.loads(result.content[0].text))

    @owner_endpoint
    async def owner_disconnect_app(body):
        client_id = body.get("client_id")
        if not isinstance(client_id, str) or not 1 <= len(client_id) <= 128:
            return JSONResponse({"error": "request_invalid"}, 400)
        revoked = await anyio.to_thread.run_sync(provider.disconnect_app, client_id)
        return JSONResponse({"revoked_grants": revoked})

    app = server.streamable_http_app(json_response=True, stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=True,
            allowed_hosts=[urlsplit(config.base_url).netloc],
            allowed_origins=[config.base_url, "https://chatgpt.com", "https://claude.ai", "https://claude.com"]))
    app.routes[:] = [r for r in app.routes if getattr(r, "path", "") != "/.well-known/oauth-authorization-server"]
    app.routes.extend([
        Route("/", health_page), Route("/health", health_page),
        Route("/.well-known/oauth-authorization-server", metadata),
        Route("/consent", consent_page), Route("/owner", owner_page),
        Route("/assets/{name}", asset),
        Route("/firebase-config", public_config),
        Route("/consent/start", consent_start, methods=["POST"]),
        Route("/consent/finish", consent_finish, methods=["POST"]),
        Route("/owner/status", owner_status, methods=["POST"]),
        Route("/owner/google/start", owner_google_start, methods=["POST"]),
        Route("/google/exchange", google_exchange, methods=["POST", "OPTIONS"]),
        Route("/owner/google/disconnect", owner_google_disconnect, methods=["POST"]),
        Route("/owner/preview", owner_preview, methods=["POST"]),
        Route("/owner/apps/disconnect", owner_disconnect_app, methods=["POST"]),
    ])
    return Boundary(app, provider)


def production_app():
    """Tests call create_app with fakes; production wires the real stores here."""
    config = Config.from_env()
    firebase_admin.initialize_app(options={"projectId": config.project_id})
    return create_app(config, FirestoreStore(config.project_id, config.database), None,
                      SecretConnection(config.project_id, config.connection_secret))
