"""The owner's Google Health connection, kept in Secret Manager.

It is one JSON object: the OAuth client the owner created in this project ("client_id",
"client_secret"), and once they allow access, the Google refresh token ("refresh_token"),
the scopes they granted ("scopes"), the Google account ("email") and when ("connected_at").
Only this service's account can read or change the secret. It is read whenever a new
access token is needed, so a change in the dashboard takes effect within the hour at most,
and at once when the dashboard disconnects, since that also revokes the token at Google.
"""
import json


class SecretConnection:
    def __init__(self, project, secret_id):
        from google.cloud import secretmanager
        self.client = secretmanager.SecretManagerServiceClient()
        self.parent = f"projects/{project}/secrets/{secret_id}"

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
