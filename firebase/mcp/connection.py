"""The owner's Google Health connection, kept in Secret Manager.

It is one JSON object: the OAuth client Firebase created for Google sign-in in this project
("client_id", "client_secret"), which the setup copies here, and once the owner allows
access, the Google refresh token ("refresh_token"), the scopes they granted ("scopes"), the
Google account ("email") and when ("connected_at").
Only this service's account can read or change the secret. It is read whenever a new
access token is needed, so a change in the dashboard takes effect within the hour at most,
and at once when the dashboard disconnects, since that also revokes the token at Google.
"""
from functools import cached_property
import json


class SecretConnection:
    def __init__(self, project, secret_id):
        self.parent = f"projects/{project}/secrets/{secret_id}"

    @cached_property
    def client(self):
        # Made on first use: a cold start serving /token never loads Secret Manager.
        from google.cloud import secretmanager
        return secretmanager.SecretManagerServiceClient()

    def get(self):
        from google.api_core.exceptions import NotFound
        try:
            data = self.client.access_secret_version(name=self.parent + "/versions/latest").payload.data
        except NotFound:
            return {}  # nothing saved yet
        return json.loads(data)

    def save(self, connection):
        """Save the whole object, then destroy older versions so only the current one exists."""
        old = list(self.client.list_secret_versions(request={"parent": self.parent, "filter": "state:ENABLED"}))
        self.client.add_secret_version(request={"parent": self.parent,
                                                "payload": {"data": json.dumps(connection).encode()}})
        for version in old:
            self.client.destroy_secret_version(request={"name": version.name})
