"""Explicit deployment configuration. No Google Health credentials."""
from dataclasses import dataclass
import json
import os
from urllib.parse import urlsplit

SCOPE = "health"


@dataclass(frozen=True)
class Config:
    base_url: str
    project_id: str
    owner_email: str
    firebase_config: dict
    database: str = "(default)"
    test_redirects: tuple[str, ...] = ()
    connection_secret: str = "google-health-connection"
    scope: str = SCOPE

    @property
    def resource(self):
        return self.base_url + "/mcp"

    @property
    def connect_origin(self):
        """The Firebase Hosting site that asks Google for health data access. Firebase's own
        Google client already allows it as a JavaScript origin, so connecting needs no setup."""
        return f"https://{self.project_id}.firebaseapp.com"

    @property
    def connect_page(self):
        return self.connect_origin + "/google-connect.html"

    @classmethod
    def from_env(cls):
        base = os.environ["MCP_BASE_URL"].rstrip("/")
        u = urlsplit(base)
        if u.scheme != "https" or not u.hostname or u.path or u.query or u.fragment or u.username:
            raise ValueError("MCP_BASE_URL must be an HTTPS origin")
        project = os.environ["GOOGLE_CLOUD_PROJECT"]
        owner = os.environ["MCP_OWNER_EMAIL"].casefold()
        if "@" not in owner:
            raise ValueError("MCP_OWNER_EMAIL required")
        public = json.loads(os.environ["FIREBASE_WEB_CONFIG"])
        if public.get("projectId") != project or not public.get("apiKey"):
            raise ValueError("Firebase configuration mismatch")
        return cls(base, project, owner, public, os.environ["MCP_AUTH_DATABASE"],
                   connection_secret=os.environ["CONNECTION_SECRET_ID"])
