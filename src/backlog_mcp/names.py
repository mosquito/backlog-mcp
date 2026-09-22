"""Stable project owner names, separate from display names and filesystem paths."""

import json
from base64 import urlsafe_b64encode
from urllib.parse import quote


def owner_name(identity: str) -> str:
    """Basic usernames remain readable; reserved prefixes separate authentication kinds."""
    parts = json.loads(identity)
    if parts == ["local"]:
        return "@local"
    if len(parts) == 2 and parts[0] == "basic":
        handle = quote(parts[1], safe="")
        if handle.startswith((".", "~")):
            handle = f"%{ord(handle[0]):02X}" + handle[1:]
        return handle
    # Include the issuer and identity kind, not only the JWT subject.
    return "@jwt-" + urlsafe_b64encode(identity.encode()).decode().rstrip("=")


def project_name(identity: str, slug: str) -> str:
    return f"{owner_name(identity)}/{slug}"
