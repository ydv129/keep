import json
import os

from google.api_core.exceptions import AlreadyExists, GoogleAPICallError
from google.cloud import secretmanager

from keep.secretmanager.secretmanager import BaseSecretManager


class GcpSecretManager(BaseSecretManager):
    def __init__(self, **kwargs):
        super().__init__()
        self.project_id = os.environ["GOOGLE_CLOUD_PROJECT"]
        self.client = secretmanager.SecretManagerServiceClient()

    def write_secret(self, secret_name: str, secret_value: str) -> None:
        """
        Writes a secret to the Secret Manager.

        Args:
            secret_name (str): The name of the secret.
            secret_value (str): The value of the secret.
        Raises:
            Exception: If an error occurs while writing the secret.
        """
        self.logger.info("Writing secret", extra={"secret_name": secret_name})

        # Construct the resource name
        resource_name = f"projects/{self.project_id}/secrets/{secret_name}"
        parent = f"projects/{self.project_id}"
        try:
            # Create the secret if it does not exist
            response = self.client.create_secret(
                request={
                    "parent": parent,
                    "secret_id": secret_name,
                    "secret": {"replication": {"automatic": {}}},
                }
            )
            self.logger.info(
                "Secret created successfully", extra={"secret_name": secret_name}
            )
        except AlreadyExists:
            # If the secret already exists, update the existing secret version
            pass

        try:
            # Add the secret version.
            parent = self.client.secret_path(self.project_id, secret_name)
            payload_bytes = secret_value.encode("UTF-8")
            response = self.client.add_secret_version(
                request={
                    "parent": parent,
                    "payload": {
                        "data": payload_bytes,
                    },
                }
            )
            self.logger.info(
                "Secret updated successfully", extra={"secret_name": secret_name}
            )
        except Exception as e:
            self.logger.error(
                "Error writing secret",
                extra={"secret_name": secret_name, "error": str(e)},
            )
            raise

    def read_secret(self, secret_name: str, is_json: bool = False) -> str | dict:
        self.logger.info("Getting secret", extra={"secret_name": secret_name})
        resource_name = (
            f"projects/{self.project_id}/secrets/{secret_name}/versions/latest"
        )
        response = self.client.access_secret_version(name=resource_name)
        secret_value = response.payload.data.decode("UTF-8")
        if is_json:
            secret_value = json.loads(secret_value)
        self.logger.info("Got secret successfully", extra={"secret_name": secret_name})
        return secret_value

    def list_secrets(self, prefix) -> list:
        """
        List all secrets with the given prefix.

        Args:
            prefix (str): The prefix to filter secrets by.

        Returns:
            list: A list of secret names.
        """
        self.logger.info("Listing secrets", extra={"prefix": prefix})
        parent = f"projects/{self.project_id}"
        secrets = []
        for secret in self.client.list_secrets(request={"parent": parent}):
            name = secret.name.split("/")[-1]
            if name.startswith(prefix):
                secrets.append(name)
        self.logger.info("Listed secrets successfully", extra={"prefix": prefix})
        return secrets
