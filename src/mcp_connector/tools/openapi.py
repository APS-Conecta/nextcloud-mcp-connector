"""The APS app tools: one per operation an app's OpenAPI document marks for the AI.

An APS app marks each OCS operation the suite AI may call with ``x-aps-ai: read`` or
``x-aps-ai: write`` (APS-Conecta/estadistica#73). This module reads those markings and
nothing else, so marking one more operation in an app exposes it without a change here.

Refused, so never a tool: an unmarked operation, every DELETE, every admin route (the
sentence nextcloud/openapi-extractor adds to each method without ``#[NoAdminRequired]``),
a PUT marked as a read and anything outside OCS. The name is the app id plus the
operation's ``operationId``, the description its ``summary``, both as the app writes them.

Every call runs with the credentials of the call, so the app sees the asking user and its
own permissions decide (docs/context-agent.md).
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

from ..nextcloud import NcClients
from ..nextcloud.clients.ocs import OCS_PREFIX, ocs_get, ocs_post, ocs_put, parse_ocs

MARKING = "x-aps-ai"

#: What nextcloud/openapi-extractor writes into an admin operation's description, or into
#: its summary when the method has no docblock (ControllerMethod::parse).
ADMIN_ONLY = "This endpoint requires admin access"

#: The verb and marking pairs that become a tool. Any other pair is refused.
ALLOWED = frozenset({("get", "read"), ("post", "read"), ("post", "write"), ("put", "write")})


@dataclass(frozen=True)
class Operation:
    """One marked operation, as much of it as its tool needs."""

    name: str
    description: str
    method: str
    path: str
    access: str
    properties: dict[str, Any]
    required: tuple[str, ...]
    path_params: tuple[str, ...]


def operations(app: str, document: Mapping[str, Any]) -> list[Operation]:
    """Every operation of ``document`` that becomes a tool, in document order."""
    found: list[Operation] = []
    for path, verbs in document.get("paths", {}).items():
        for method, spec in verbs.items():
            if (
                (method, spec.get(MARKING)) in ALLOWED
                and path.startswith(f"{OCS_PREFIX}/")
                and not _admin_only(spec)
            ):
                found.append(_operation(app, path.removeprefix(OCS_PREFIX), method, spec))
    return found


async def call(clients: NcClients, operation: Operation, arguments: Mapping[str, Any]) -> Any:
    """Send ``operation`` with the declared ``arguments`` and answer its OCS data."""
    path = operation.path.format_map(
        {name: quote(str(arguments[name]), safe="") for name in operation.path_params}
    )
    given = {
        name: value
        for name, value in arguments.items()
        if value is not None and name in operation.properties and name not in operation.path_params
    }
    if operation.method == "get":
        response = await ocs_get(clients.client, clients.creds, path, given)
    elif operation.method == "post":
        response = await ocs_post(clients.client, clients.creds, path, given)
    else:
        response = await ocs_put(clients.client, clients.creds, path, given)
    return parse_ocs(response, path)


def _admin_only(spec: Mapping[str, Any]) -> bool:
    return any(ADMIN_ONLY in spec.get(key, "") for key in ("summary", "description"))


def _parameter_schema(parameter: Mapping[str, Any]) -> dict[str, Any]:
    """An OpenAPI parameter's schema, with its description moved in where a model reads it."""
    schema = dict(parameter.get("schema", {}))
    if "description" in parameter:
        schema["description"] = parameter["description"]
    return schema


def _operation(app: str, path: str, method: str, spec: Mapping[str, Any]) -> Operation:
    parameters = [p for p in spec.get("parameters", []) if p.get("in") in ("path", "query")]
    body = spec.get("requestBody", {}).get("content", {}).get("application/json", {})
    schema = body.get("schema", {})
    properties = {p["name"]: _parameter_schema(p) for p in parameters}
    properties.update(schema.get("properties", {}))
    required = [p["name"] for p in parameters if p.get("required")]
    required += schema.get("required", [])
    return Operation(
        name=f"{app}_{spec['operationId']}".replace("-", "_"),
        description=spec.get("summary", ""),
        method=method,
        path=path,
        access=spec[MARKING],
        properties=properties,
        required=tuple(required),
        path_params=tuple(p["name"] for p in parameters if p["in"] == "path"),
    )
