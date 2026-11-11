"""Registration of the APS app tools (gestion-aio#93). The logic lives in ``tools/openapi.py``.

Each ``<app>.json`` under ``mcp_connector/openapi`` is an APS app's ``openapi.json``, copied
as the app ships it. The app keeps its document out of its release and out of the web
server, so the connector carries the copy, and refreshing it is the whole step for a newly
marked operation. ``estadistica.json`` is Estadística's at APS-Conecta/estadistica#82.

No bundle and no switch: ``_load_registrations`` calls :func:`register` on every start, so
the tools are part of the default surface and of its byte budget.

The read and write classification is the tool annotation, the one place a client such as
the Context Agent reads to decide what it confirms: a read is ``READ_ONLY``, a marked POST
``CREATE_ONLY`` and a marked PUT ``UPDATE_EXISTING``.
"""

import inspect
import json
from pathlib import Path
from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations
from pydantic import WithJsonSchema

from .. import deps
from ..tools import openapi
from . import CREATE_ONLY, READ_ONLY, UPDATE_EXISTING, compact, graceful

DOCUMENTS = Path(__file__).resolve().parents[1] / "openapi"


def register(server: MCPServer, documents: Path = DOCUMENTS) -> None:
    """Build the tools of every app document in ``documents`` on ``server``."""
    for document in sorted(documents.glob("*.json")):
        app = document.stem
        for operation in openapi.operations(app, json.loads(document.read_text("utf-8"))):
            server.add_tool(
                _tool(operation),
                name=operation.name,
                description=operation.description,
                annotations=_annotations(operation),
                structured_output=False,
            )


def _annotations(operation: openapi.Operation) -> ToolAnnotations:
    if operation.access == "read":
        return READ_ONLY
    return UPDATE_EXISTING if operation.method == "put" else CREATE_ONLY


def _tool(operation: openapi.Operation) -> Any:
    """One tool function whose signature is the operation's parameters and body fields.

    The SDK builds the input schema and the argument check from the signature, so each
    field carries the schema the app wrote, and an argument the operation does not declare
    never reaches the function.
    """

    async def tool(ctx: Context | None = None, **arguments: Any) -> str:
        return compact(await openapi.call(deps.resolve_clients(ctx), operation, arguments))

    tool.__name__ = operation.name
    audited = graceful(tool)
    fields = [
        inspect.Parameter(
            name,
            inspect.Parameter.KEYWORD_ONLY,
            annotation=Annotated[Any, WithJsonSchema(schema)],
            **({} if name in operation.required else {"default": None}),
        )
        for name, schema in operation.properties.items()
    ]
    context = inspect.Parameter(
        "ctx", inspect.Parameter.KEYWORD_ONLY, annotation=Context | None, default=None
    )
    audited.__signature__ = inspect.Signature([*fields, context])  # type: ignore[attr-defined]
    return audited
