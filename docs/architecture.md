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
| GET | `/` | live sessions page |
| GET | `/plans` | archive browser |

The archive index is cached (`HUB_ARCHIVE_CACHE`, default 20s) and the session
list is refreshed by a background scanner every `HUB_INTERVAL` seconds.

## Decision endpoint

`POST /api/sessions/decide` takes `{"host", "port", "decision", "feedback"}`
where `decision` is `approve` or `deny`. The hub builds the corresponding
upstream request (`POST http://<host>:<port>/api/approve|deny`) and returns the
result. It is a thin forwarder: upstream errors are reported back in the JSON
body rather than raised. This is the only non-GET path, and it is why the hub
must sit behind an authentication gate.

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
