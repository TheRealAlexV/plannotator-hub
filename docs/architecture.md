# Architecture

The Plannotator plan hub aggregates plan-review sessions that run on separate
agent workstations. It is a single small Python process plus two static pages.
Everything host-specific is read from the environment.

## Two planes

**Control plane (the hub).** One process, `hub/plan-hub.py`. It never proxies
live review traffic. It does two jobs:

1. *Live sessions.* It scans each workstation over a fixed range of ports,
   probing `GET /api/plan` on every port. A response whose JSON contains
   `plan` or `mode` is treated as a live session. Results are merged with a
   short grace window so a session does not flicker out during a restart, and
   served as JSON at `/api/sessions` and a landing page at `/`.
2. *Plan archive.* It polls each workstation's read-only archive server
   (`GET /api/archive/plans`, `GET /api/archive/plan?filename=`) and renders
   the aggregated catalogue at `/plans`.

**Per-session review servers (the data plane).** On each workstation, the
Plannotator CLI binds a review server to a port in that workstation's assigned
range. These servers do the real review/annotate work; the hub only links to
them. The archive server (`plannotator archive`) is a separate read-only
listener on each workstation.

## Why session URLs are port-derived

Because the hub does not proxy review traffic, opening a session means reaching
the workstation's review server directly on its port. The hub therefore emits
URLs of the form:

```
https://p<port>.<HUB_PUBLIC_SUFFIX>/
```

The port is the identifier: `p19432.plan.example.com` and `p19433.plan.example.com`
route to different live sessions. This is why **each workstation must own a
disjoint port range** in `HUB_WORKSTATIONS` — if two workstations could bind
the same port, the same hostname would be ambiguous. Disjointness is a
correctness requirement, not a nicety, and it is why `HUB_WORKSTATIONS` carries
an explicit `start-end` per host rather than a shared pool.

## Hub endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/healthz` | liveness probe, returns `ok` |
| GET | `/api/sessions` | live sessions (title, project, mode, host, port, timestamps) |
| GET | `/api/plans` | aggregated archive index (add `?refresh` to bypass cache) |
| GET | `/api/plans/raw?host=&filename=` | raw markdown for one archived plan |
| GET | `/api/live/plan?host=&port=` | fetch a live session's plan from its workstation |
| POST | `/api/sessions/decide` | forward an approve/deny decision to a session |
| GET | `/api/archive/list` | list stored plans (add `?includeDeleted=1` for soft-deleted) |
| POST | `/api/archive/import` | fetch + store a plan (idempotent per host+filename) |
| GET | `/api/archive/get?id=` | one stored plan: markdown + meta |
| POST | `/api/archive/update` | edit markdown/title/note/tags of one plan |
| POST | `/api/archive/tag` | add/remove tags on many plans |
| POST | `/api/archive/delete` | soft-delete (default) or hard-delete plans |
| POST | `/api/archive/restore` | clear the soft-deleted flag |
| POST | `/api/archive/reopen` | reopen a stored plan as a live review on a workstation agent |
| POST | `/api/archive/apply` | hand a stored plan to an implementation agent on a workstation |
| GET | `/` | live sessions page |
| GET | `/plans` | archive browser |

The archive index is cached (`HUB_ARCHIVE_CACHE`, default 20s) and the session
list is refreshed by a background scanner every `HUB_INTERVAL` seconds.

## Archive store

The read-only aggregation above is stateless: it re-reads each workstation's
archive on demand. The **archive store** is the writable, persistent layer on
top of it — a real archive the user can tag, edit, delete and restore, unlike
`plannotator archive` itself (which is strictly read-only).

Storage lives under `HUB_ARCHIVE_DIR` (default `/opt/plan-hub/archive`):

```
<HUB_ARCHIVE_DIR>/
  index.json        {"version":1,"plans":{"<id>":{...metadata...}}}
  plans/<id>.md     the markdown body for each plan
```

Metadata per plan: `id` (`p_<8hex>`, URL-safe and stable), `title`, `slug`,
`project`, `host`, `originFilename`, `date`, `status`
(`approved|denied|other`), `tags` (normalised), `note`, `source`
(`import|live`), `archivedAt`, `updatedAt`, `size`, `deleted`.

- **Import** accepts either `{"host","filename"}` (from a workstation archive)
  or `{"host","port"}` (from a live session's `GET /api/plan`). It is
  **idempotent** on `(host, originFilename)`: re-importing updates the existing
  entry (and clears its soft-delete flag) instead of duplicating it.
- **Tags** are normalised (lowercase, trimmed, spaces collapsed, empties
  dropped, de-duplicated, ≤32 chars, ≤20 per plan). `tags` on update replaces
  the whole array.
- **Soft delete** sets `deleted:true`; the plan stays on disk and is excluded
  from the default listing (`?includeDeleted=1` shows it) until `restore`
  clears the flag. **Hard delete** (`{"hard":true}`) removes both the index
  entry and its markdown file.
- The store is additive: `/api/plans` and `/api/plans/raw` keep serving the
  workstation-wide read-only catalogue unchanged. A UI "import this plan"
  action calls `/api/archive/import` with a `host`+`filename` pair taken from
  `/api/plans`.
- Index writes are atomic (temp file + `os.replace`) and serialised by a
  dedicated lock. A missing or corrupt `index.json` is logged and treated as
  empty rather than crashing the server; the next successful write repairs it.
  Directories are created on first use.


## Workstation actions

Two operations can only happen where the Plannotator CLI and its state live, so
each workstation runs a small **planhub-agent** (`workstation/planhub-agent.py`,
systemd user service `planhub-agent.service`, bound `0.0.0.0:8898`). The hub
never talks to it without a shared token.

- `HUB_AGENTS="host:port=token,host:port=token"` maps each workstation to its
  agent. The token is the agent's `PLANHUB_AGENT_TOKEN`, sent as the
  `X-Planhub-Token` header. Agent routes other than `/healthz` require it and
  are compared in constant time; an agent with no token refuses to start.
- `POST /api/archive/reopen` `{"id","host"?}` reads the stored markdown and
  asks the workstation agent to launch
  `plannotator annotate <file> --gate --json --result-file <file>.result.json`
  detached, returning the new session's `file`, `pid`, `port` and `url`. The
  default host is the stored entry's `host`, else the first configured agent.
- `POST /api/archive/apply` `{"id","host"?,"projectDir"?}` asks the agent to
  run its **environment-configured** `PLANHUB_APPLY_COMMAND` (never a command
  from the request) with `{plan}`/`{project}` substituted, recording a job.
  Returns `{"jobId":…}`; jobs are listed/queried on the agent
  (`/api/agent/jobs`, `/api/agent/job?id=`).
- Agent-side errors (unreachable, 401, missing apply config) are surfaced by the
  hub as `{"ok":false,"error":…}` with HTTP 502. The hub proxies the agent's
  result rather than re-implementing it.

Each agent also needs its own LAN IP (`PLANHUB_URL_HOST`) and Plannotator port
slice (`PLANHUB_PORT_RANGE`) so the reopened session advertises the correct URL
and binds a port unique to that workstation.


## Decision endpoint

`POST /api/sessions/decide` takes `{"host", "port", "decision", "feedback"}`
where `decision` is `approve` or `deny`. The hub builds the corresponding
upstream request (`POST http://<host>:<port>/api/approve|deny`) and returns the
result. It is a thin forwarder: upstream errors are reported back in the JSON
body rather than raised. Along with the `/api/archive/*` write actions, it is
why the hub must sit behind an authentication gate.

## OAuth gate

The hub binds loopback (`HUB_BIND=127.0.0.1`) and is never exposed directly.
A reverse proxy terminates TLS and puts an SSO/OAuth gate (for example
oauth2-proxy) in front of every location, including the API and the generated
`p<port>.<suffix>` hosts. The gate protects both the decision endpoint and the
archive contents. Because the hub trusts caller-supplied `host`/`port`, the gate
must be mandatory: without it, `/api/sessions/decide` would forward arbitrary
requests to arbitrary hosts.

## TLS: one multi-SAN certificate

Every live session gets its own hostname (`p<port>.<suffix>`), so a
per-host certificate would be impractical. Instead a single certificate carries
a SAN for each published `p<port>.<suffix>` name, and DNS points all of them at
the hub. The wildcard option is coarse (`*.plan.example.com` covers every port
but only one label); an explicit multi-SAN cert is the precise choice when the
published ports are known. The certificate is presented by the same reverse
proxy that fronts the hub.

## Configuration

All configuration is environment-driven; see `systemd/plan-hub.env.example`.
The defaults compiled into `hub/plan-hub.py` are generic examples only. The
systemd unit loads `/etc/plan-hub.env` via `EnvironmentFile=-`, so the unit is
identical across deployments and only the env file differs.
