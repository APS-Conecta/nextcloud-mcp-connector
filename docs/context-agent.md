# Nextcloud's Context Agent, acting as the asking user

**Scope:** how Nextcloud's Context Agent (the agent behind Nextcloud Assistant) calls this
connector so that every call runs as the user who asked. This is the contract the Context
Agent patch of APS-Conecta/gestion-aio#92 implements; the connector side needs no setting.

The user travels the way AppAPI already carries it between ExApps: the agent signs each call
for the task's user, Nextcloud authenticates that signature, and Nextcloud's AppAPI proxy
forwards the call here with its own signature for that user. The connector never takes the
user from the agent, the model or a tool argument.

## The contract

1. **Endpoint.** The agent sends its MCP requests to Nextcloud's AppAPI proxy:
   `<NEXTCLOUD_URL>/index.php/apps/app_api/proxy/mcp_connector/mcp`. Not to
   `/exapps/mcp_connector/mcp`: HaRP resolves a user only from a Nextcloud session cookie or
   an `Authorization` header, so an ExApp signature for a user does not pass there.
2. **Per call, the asking user.** Every request carries the four AppAPI headers the agent
   already sends to Nextcloud as an ExApp (nc_py_api builds them for every call):

   | Header | Value |
   |---|---|
   | `EX-APP-ID` | the agent's own app id (`context_agent`) |
   | `EX-APP-VERSION` | the agent's installed version |
   | `AA-VERSION` | the AppAPI version |
   | `AUTHORIZATION-APP-API` | base64 of `<user id>:<the agent's APP_SECRET>` |

   The user id is the `userId` of the task Nextcloud handed to the agent. A tool list or MCP
   session built for one task is never reused for a task of another user.
3. **No other credential.** The MCP configuration of the agent carries no `Authorization`
   header, no app password and no token. A call signed for a user never has its
   `Authorization` header read. A call signed with no user falls through to the connector's
   OAuth branch, which does read `Authorization`, and the proxy forwards that header because
   the `/mcp` route in `appinfo/info.xml` excludes only the AppAPI headers: a valid connector
   OAuth bearer on such a call runs it as that token's owner. Carrying no credential is
   therefore what makes a task without a user end in a 401. Over HaRP's route a static
   credential would make every call run as its owner.

## What Nextcloud does

- Nextcloud core accepts the AppAPI signature as a login (`tryAppAPILogin` in `lib/base.php`,
  then `AppAPIService::validateExAppRequestToNC`): the request runs as that user, and a user
  id that does not exist is refused.
- The proxy (`ExAppProxyController`) drops the caller's AppAPI headers, checks the route of
  `appinfo/info.xml` (`/mcp` is PUBLIC), and forwards the call with `AUTHORIZATION-APP-API`
  signed with this app's secret for the signed-in user, plus `X-Origin-IP`.

## What the connector does

- It verifies that signature with its own `APP_SECRET`. The user in it is the identity of
  every Nextcloud request the call makes (AppAPI impersonation), so Nextcloud's own
  permissions decide what the call reads and writes.
- When the signature names a user, an `Authorization` header next to it is not read. No tool
  takes a user parameter, and an argument the tool does not declare is dropped.
- A call whose signature carries no user (a task without a user, or the agent's app context)
  and no valid connector OAuth bearer, which point 3 guarantees, is refused with 401 before
  any MCP code runs, and nothing reaches Nextcloud. The 401 carries the OAuth discovery
  challenge because the same route serves OAuth clients; to the agent it is a refusal.
- The per account switch of the connections page applies as on every other way in.
- After each write that runs, it records the write in Nextcloud's audit log through
  APS-Conecta/auditoria: `POST /ocs/v2.php/apps/auditoria/api/v1/escrituras` as the same
  user, with the tool's name and the object's id (the answer's `id`, or a file's `path`),
  never content. A write is every tool not annotated read-only; reads call nothing. When the
  route does not record it, the write's answer carries an `audit` note saying so, the
  connector logs a warning, and the write is not repeated. The entry reaches audit.log only
  with admin_audit enabled and its info lines let through
  (`occ config:system:set log.condition apps 0 --value=admin_audit`).

External MCP clients keep using `/exapps/mcp_connector/mcp`
([client-setup.md](client-setup.md)). An app password there is resolved by HaRP into an
AppAPI-signed user, and so is a token-exchange identity, so their writes are recorded like the
agent's. Writes over an OAuth sign-in (the connection's own app password) and in the standalone
modes (HTTP passthrough, static bearer, stdio) are not recorded, because auditoria admits only
calls AppAPI signs for `mcp_connector`. Each such write still runs, its answer carries the
`audit` note, and the connector logs a warning.

## Proof

- Unit, at the transport seam: `tests/unit/test_exapp_entry.py`,
  `test_a_proxied_call_runs_as_the_signed_user_whatever_else_it_names` and
  `test_a_proxied_call_without_a_user_is_refused`.
- Live, against Nextcloud, AppAPI and HaRP (compose.exapp.yml): the AppAPI proxy block of
  `tests/integration/test_permission_fidelity_exapp.py`, where an ExApp signature for bob does
  not find alice's file and a signature without a user is refused.
- The audit call: `tests/unit/test_write_audit.py` at the tool call, and live
  `tests/integration/test_audit_log_live.py`, where a write over the proxy leaves exactly one
  audit.log entry naming alice, the tool and the object, and a read leaves none.
