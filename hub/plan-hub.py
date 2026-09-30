#!/usr/bin/env python3
"""Plannotator session hub + plan archive browser.

Two jobs, both control-plane (this process never proxies live review traffic):

1. Live sessions: scan workstations over a fixed port range, probing
   GET /api/plan, and serve a landing page linking each session to its own
   port-indexed subdomain (https://p<port>.plan.example.com/).

2. Plan archive: aggregate each workstation's read-only archive server
   (GET /api/archive/plans, GET /api/archive/plan?filename=) and serve a
   browser that renders the archived markdown (and Mermaid) for review.

3. Archive store: a writable, persistent copy of selected plans under
   HUB_ARCHIVE_DIR (index.json + plans/<id>.md). Plans can be imported from a
   workstation archive or a live session, then tagged, edited, soft-deleted and
   restored via /api/archive/*. Additive to the read-only aggregation above.

4. Workstation actions: proxy the two actions that need a local Plannotator CLI
   on a workstation (POST /api/archive/reopen, /api/archive/apply) to that
   workstation's planhub-agent over HUB_AGENTS.

Config:
    HUB_WORKSTATIONS="10.0.0.10:19432-19463,10.0.0.11:19464-19495"
    HUB_ARCHIVES="10.0.0.10:8897,10.0.0.11:8897"
    HUB_LABELS="10.0.0.10=workstation-a,10.0.0.11=workstation-b"
    HUB_AGENTS="10.0.0.10:8898=token-a,10.0.0.11:8898=token-b"
    HUB_PLAN_PAGE=/opt/plan-hub/plans-page.html
    HUB_ARCHIVE_DIR=/opt/plan-hub/archive
"""
import json
import os
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import Request, urlopen


def _parse_host_port_list(raw):
    out = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        host, _, port = part.partition(":")
        try:
            out.append((host.strip(), int(port)))
        except ValueError:
            continue
    return out


def _parse_workstations():
    raw = os.environ.get("HUB_WORKSTATIONS", "").strip()
    out = []
    if raw:
        for part in raw.split(","):
            part = part.strip()
            if not part:
                continue
            host, _, rng = part.partition(":")
            a, _, b = rng.partition("-")
            try:
                out.append((host.strip(), int(a), int(b)))
            except ValueError:
                continue
    if not out:
        out.append((
            os.environ.get("HUB_SCAN_HOST", "10.0.0.10"),
            int(os.environ.get("HUB_PORT_START", "19432")),
            int(os.environ.get("HUB_PORT_END", "19463")),
        ))
    return out


def _parse_labels():
    out = {}
    for part in (os.environ.get("HUB_LABELS", "") or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, _, v = part.partition("=")
        if k.strip() and v.strip():
            out[k.strip()] = v.strip()
    return out


def _parse_agents():
    """Parse HUB_AGENTS="host:port=token,host:port=token" into host -> {port,token}."""
    out = {}
    for part in (os.environ.get("HUB_AGENTS", "") or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        loc, _, token = part.partition("=")
        token = token.strip()
        host, _, port = loc.strip().partition(":")
        host = host.strip()
        if not host or not token:
            continue
        try:
            port = int(port)
        except ValueError:
            continue
        out[host] = {"port": port, "token": token}
    return out


WORKSTATIONS = _parse_workstations()
TARGETS = [(host, port) for (host, a, b) in WORKSTATIONS for port in range(a, b + 1)]
ARCHIVES = _parse_host_port_list(os.environ.get("HUB_ARCHIVES", ""))
AGENTS = _parse_agents()
LABELS = _parse_labels()
PLAN_PAGE = os.environ.get("HUB_PLAN_PAGE", "/opt/plan-hub/plans-page.html")
SESSIONS_PAGE = os.environ.get("HUB_SESSIONS_PAGE", "/opt/plan-hub/sessions-page.html")

INTERVAL = float(os.environ.get("HUB_INTERVAL", "3"))
PROBE_TIMEOUT = float(os.environ.get("HUB_PROBE_TIMEOUT", "2"))
BIND = os.environ.get("HUB_BIND", "127.0.0.1")
BIND_PORT = int(os.environ.get("HUB_PORT", "8899"))
PUBLIC_SUFFIX = os.environ.get("HUB_PUBLIC_SUFFIX", "plan.example.com")
GRACE = float(os.environ.get("HUB_GRACE", "10"))
ARCHIVE_TIMEOUT = float(os.environ.get("HUB_ARCHIVE_TIMEOUT", "6"))
ARCHIVE_CACHE = float(os.environ.get("HUB_ARCHIVE_CACHE", "20"))
AGENT_TIMEOUT = float(os.environ.get("HUB_AGENT_TIMEOUT", "15"))

# Persistent plan archive store (writable, additive to the read-only aggregation
# above). Root holds index.json plus plans/<id>.md bodies.
ARCHIVE_DIR = os.environ.get("HUB_ARCHIVE_DIR", "/opt/plan-hub/archive")
ARCHIVE_INDEX = os.path.join(ARCHIVE_DIR, "index.json")
ARCHIVE_PLANS = os.path.join(ARCHIVE_DIR, "plans")

_lock = threading.Lock()
_archive_lock = threading.Lock()
_sessions = {}
_archive_cache = {"at": 0.0, "data": None}


def label_for(host):
    return LABELS.get(host, host)


def _title_from_plan(text):
    if not text:
        return None
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("#"):
            t = s.lstrip("#").strip()
            if t:
                return t[:120]
        elif s:
            return s[:120]
    return None


def _probe(target):
    host, port = target
    url = "http://%s:%d/api/plan" % (host, port)
    try:
        with urlopen(url, timeout=PROBE_TIMEOUT) as resp:
            if resp.status != 200:
                return None
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if "plan" not in data and "mode" not in data:
        return None
    project = None
    vi = data.get("versionInfo")
    if isinstance(vi, dict):
        project = vi.get("project")
    if not project and data.get("projectRoot"):
        project = os.path.basename(str(data["projectRoot"]).rstrip("/")) or None
    title = _title_from_plan(data.get("plan"))
    if not title and data.get("filePath"):
        title = os.path.basename(str(data["filePath"]))
    return {
        "host": host,
        "hostLabel": label_for(host),
        "port": port,
        "mode": data.get("mode") or "plan",
        "project": project or "(unknown project)",
        "title": title or "(untitled)",
    }


def _scan_once():
    now = time.time()
    found = {}
    with ThreadPoolExecutor(max_workers=24) as ex:
        for res in ex.map(_probe, TARGETS):
            if res:
                res["lastSeen"] = now
                found[(res["host"], res["port"])] = res
    with _lock:
        merged = dict(found)
        for key, prev in _sessions.items():
            if key not in merged and now - prev.get("lastSeen", 0) <= GRACE:
                merged[key] = prev
        for key, s in merged.items():
            prev = _sessions.get(key)
            same = prev and all(prev.get(k) == s.get(k) for k in ("title", "project", "mode"))
            s["firstSeen"] = prev.get("firstSeen", now) if same else now
        _sessions.clear()
        _sessions.update(merged)


def _scanner():
    while True:
        try:
            _scan_once()
        except Exception as exc:  # keep the loop alive
            print("scan error: %r" % (exc,), flush=True)
        time.sleep(INTERVAL)


def _fetch_bytes(url):
    with urlopen(url, timeout=ARCHIVE_TIMEOUT) as resp:
        return resp.read().decode("utf-8", "replace")


def _archive_for_host(host, port):
    out = []
    try:
        data = json.loads(_fetch_bytes("http://%s:%d/api/archive/plans" % (host, port)))
    except Exception:
        return out
    for p in (data.get("plans") or []):
        if not isinstance(p, dict) or not p.get("filename"):
            continue
        fn = str(p["filename"])
        out.append({
            "host": host,
            "hostLabel": label_for(host),
            "filename": fn,
            "title": p.get("title") or fn,
            "date": p.get("date") or "",
            "timestamp": p.get("timestamp") or "",
            "status": p.get("status") or "",
            "size": p.get("size") or 0,
            "kind": "feedback" if fn.endswith(".annotations.md") else "plan",
        })
    return out


def _archive_index(force=False):
    now = time.time()
    with _lock:
        cached = _archive_cache["data"]
        if not force and cached is not None and now - _archive_cache["at"] < ARCHIVE_CACHE:
            return cached
    plans = []
    if ARCHIVES:
        with ThreadPoolExecutor(max_workers=len(ARCHIVES)) as ex:
            for res in ex.map(lambda hp: _archive_for_host(*hp), ARCHIVES):
                plans.extend(res)
    plans.sort(key=lambda p: (p.get("timestamp") or "", p.get("filename") or ""), reverse=True)
    with _lock:
        _archive_cache["at"] = now
        _archive_cache["data"] = plans
    return plans


def _archive_plan(host, filename):
    for h, port in ARCHIVES:
        if h == host:
            url = "http://%s:%d/api/archive/plan?filename=%s" % (h, port, quote(filename))
            data = json.loads(_fetch_bytes(url))
            return data.get("markdown") or ""
    raise KeyError(host)


# ---------------------------------------------------------------------------
# Persistent archive store
#
# Layout (root defaults to /opt/plan-hub/archive, HUB_ARCHIVE_DIR):
#   index.json        {"version":1,"plans":{"<id>":{...meta...}}}
#   plans/<id>.md     markdown body
#
# Writes are atomic (temp file + os.replace) and serialised by _archive_lock.
# A corrupt/unreadable index never crashes the server: it is logged and treated
# as empty (the next successful write repairs it).
# ---------------------------------------------------------------------------

_ARCHIVE_NAME_RE = re.compile(
    r"^(?P<slug>.+?)-(?P<date>\d{4}-\d{2}-\d{2})-"
    r"(?P<status>approved|denied|other)(?P<ann>\.annotations)?\.md$"
)


def _now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:80]


def _derive_filename_meta(filename):
    """Return (slug, date, status) from a plannotator `<slug>-<date>-<status>.md` name."""
    fn = filename or ""
    m = _ARCHIVE_NAME_RE.match(fn)
    if m:
        return m.group("slug"), m.group("date"), m.group("status")
    base = re.sub(r"\.(annotations\.)?md$", "", fn).strip()
    return (base[:80] or "plan"), "", "other"


def _norm_tags(tags):
    """lowercase, trim, collapse spaces, drop empties, unique, <=32 chars, <=20 tags."""
    out = []
    if not isinstance(tags, (list, tuple)):
        return out
    for t in tags:
        s = re.sub(r"\s+", " ", str(t)).strip().lower()[:32]
        if s and s not in out:
            out.append(s)
        if len(out) >= 20:
            break
    return out


def _write_atomic(path, text):
    parent = os.path.dirname(path)
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _load_index():
    try:
        with open(ARCHIVE_INDEX, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or not isinstance(data.get("plans"), dict):
            raise ValueError("index.json missing a plans object")
        return data
    except FileNotFoundError:
        return {"version": 1, "plans": {}}
    except Exception as exc:
        print("archive index unreadable (%r); starting empty" % (exc,), flush=True)
        return {"version": 1, "plans": {}}


def _save_index(index):
    _write_atomic(ARCHIVE_INDEX, json.dumps(index, indent=2, sort_keys=True))


def _plan_path(pid):
    return os.path.join(ARCHIVE_PLANS, "%s.md" % pid)


def _read_plan(pid):
    try:
        with open(_plan_path(pid), "r", encoding="utf-8") as fh:
            return fh.read()
    except FileNotFoundError:
        return None
    except Exception as exc:
        print("archive plan read error %s: %r" % (pid, exc), flush=True)
        return None


def _new_id():
    return "p_" + os.urandom(4).hex()


def _find_entry(index, host, origin):
    for pid, entry in index["plans"].items():
        if entry.get("host") == host and entry.get("originFilename") == origin:
            return pid, entry
    return None, None


def _store_plan(host, origin_filename, markdown, project="", title=None,
                tags=None, note="", source="import"):
    """Create or update the entry keyed on (host, originFilename); returns (id, created)."""
    slug, date, status = _derive_filename_meta(origin_filename)
    now = _now_iso()
    with _archive_lock:
        index = _load_index()
        pid, entry = _find_entry(index, host, origin_filename)
        created = entry is None
        if created:
            pid = _new_id()
            entry = {
                "id": pid, "title": "", "slug": slug, "project": project,
                "host": host, "originFilename": origin_filename, "date": date,
                "status": status, "tags": [], "note": "", "source": source,
                "archivedAt": now, "updatedAt": now, "size": 0, "deleted": False,
            }
            index["plans"][pid] = entry
        _write_atomic(_plan_path(pid), markdown or "")
        entry.update({
            "slug": slug,
            "date": date,
            "status": status,
            "title": title or _title_from_plan(markdown) or entry.get("title") or origin_filename,
            "project": project or entry.get("project") or "",
            "size": len((markdown or "").encode("utf-8")),
            "source": source,
            "updatedAt": now,
            "deleted": False,
        })
        if tags is not None:
            entry["tags"] = _norm_tags(tags)
        if note:
            entry["note"] = str(note)[:2000]
        _save_index(index)
        return pid, created


def _archive_list(include_deleted=False):
    with _archive_lock:
        entries = list(_load_index()["plans"].values())
    if not include_deleted:
        entries = [e for e in entries if not e.get("deleted")]
    entries.sort(key=lambda e: e.get("archivedAt") or "", reverse=True)
    tags = sorted({t for e in entries for t in (e.get("tags") or [])})
    counts = {
        "total": len(entries),
        "active": sum(1 for e in entries if not e.get("deleted")),
        "deleted": sum(1 for e in entries if e.get("deleted")),
        "imported": sum(1 for e in entries if e.get("source") == "import"),
        "live": sum(1 for e in entries if e.get("source") == "live"),
    }
    return {"plans": [dict(e) for e in entries], "tags": tags, "counts": counts}


def _archive_get(pid):
    with _archive_lock:
        entry = _load_index()["plans"].get(pid)
    if entry is None:
        return None
    return {"id": pid, "markdown": _read_plan(pid) or "", "meta": dict(entry)}


def _archive_update(pid, fields):
    with _archive_lock:
        index = _load_index()
        entry = index["plans"].get(pid)
        if entry is None:
            return None
        if "markdown" in fields:
            md = fields["markdown"]
            if not isinstance(md, str):
                raise ValueError("markdown must be a string")
            _write_atomic(_plan_path(pid), md)
            entry["size"] = len(md.encode("utf-8"))
        if "title" in fields:
            entry["title"] = str(fields["title"])[:200]
        if "note" in fields:
            entry["note"] = str(fields["note"])[:2000]
        if "tags" in fields:
            entry["tags"] = _norm_tags(fields["tags"])
        entry["updatedAt"] = _now_iso()
        _save_index(index)
        return dict(entry)


def _archive_tag(ids, add, remove):
    add = _norm_tags(add)
    remove = set(_norm_tags(remove))
    updated, missing = [], []
    with _archive_lock:
        index = _load_index()
        for pid in ids:
            entry = index["plans"].get(pid)
            if entry is None:
                missing.append(pid)
                continue
            tags = [t for t in (entry.get("tags") or []) if t not in remove]
            for t in add:
                if t not in tags:
                    tags.append(t)
            entry["tags"] = _norm_tags(tags)
            entry["updatedAt"] = _now_iso()
            updated.append(dict(entry))
        if updated:
            _save_index(index)
    return updated, missing


def _archive_delete(ids, hard=False):
    count, missing = 0, []
    with _archive_lock:
        index = _load_index()
        for pid in ids:
            entry = index["plans"].get(pid)
            if entry is None:
                missing.append(pid)
                continue
            if hard:
                index["plans"].pop(pid, None)
                try:
                    os.unlink(_plan_path(pid))
                except FileNotFoundError:
                    pass
                except Exception as exc:
                    print("archive file delete error %s: %r" % (pid, exc), flush=True)
            else:
                entry["deleted"] = True
                entry["updatedAt"] = _now_iso()
            count += 1
        if count:
            _save_index(index)
    return count, missing


def _archive_restore(ids):
    count, missing = 0, []
    with _archive_lock:
        index = _load_index()
        for pid in ids:
            entry = index["plans"].get(pid)
            if entry is None:
                missing.append(pid)
                continue
            entry["deleted"] = False
            entry["updatedAt"] = _now_iso()
            count += 1
        if count:
            _save_index(index)
    return count, missing


def _import_plan(host, filename=None, port=None, project="", title=None,
                 tags=None, note=""):
    """Fetch a plan's markdown and store it. Raises LookupError for unknown host."""
    if filename:
        try:
            markdown = _archive_plan(host, filename)
        except KeyError:
            raise LookupError("host %s has no configured archive" % host)
        origin = filename
        source = "import"
    else:
        data = json.loads(_fetch_bytes("http://%s:%d/api/plan" % (host, int(port))))
        markdown = data.get("plan") or ""
        fp = str(data.get("filePath") or "")
        origin = os.path.basename(fp.rstrip("/")) if fp else ""
        vi = data.get("versionInfo") if isinstance(data.get("versionInfo"), dict) else {}
        project = project or (vi or {}).get("project") \
            or os.path.basename(str(data.get("projectRoot") or "").rstrip("/")) or ""
        if not origin:
            origin = "%s-%s-live.md" % (
                _slugify(_title_from_plan(markdown) or "live-plan") or "live-plan",
                time.strftime("%Y-%m-%d"))
        source = "live"
    return _store_plan(host, origin, markdown if isinstance(markdown, str) else "",
                       project=project, title=title, tags=tags, note=note, source=source)


def _agent_call(host, path, payload):
    """POST JSON to a workstation planhub-agent. Returns (status, dict)."""
    agent = AGENTS.get(host)
    if agent is None:
        raise KeyError(host)
    req = Request(
        "http://%s:%d%s" % (host, agent["port"], path),
        data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json",
                 "X-Planhub-Token": agent["token"]})
    status = 0
    try:
        with urlopen(req, timeout=AGENT_TIMEOUT) as resp:
            status = resp.status
            text = resp.read().decode("utf-8", "replace")
    except HTTPError as exc:
        status = exc.code
        try:
            text = exc.read().decode("utf-8", "replace")
        except Exception:
            text = ""
    except Exception as exc:
        return 0, {"error": repr(exc)}
    try:
        data = json.loads(text)
    except Exception:
        data = {"error": text[:300] or "unparseable agent response"}
    if not isinstance(data, dict):
        data = {"error": "unexpected agent response"}
    return status, data


def _resolve_agent_host(requested, entry):
    """Pick the target workstation agent: explicit, else the entry's host, else first."""
    host = str(requested or "").strip()
    if not host:
        host = (entry or {}).get("host") or ""
    if not host and AGENTS:
        host = next(iter(AGENTS))
    return host


def _proxy_workstation(pid, action, host_override, project_dir=None):
    """Shared body for /api/archive/reopen and /api/archive/apply."""
    if not AGENTS:
        return 400, {"ok": False, "error": "no workstation agents configured (HUB_AGENTS)"}
    plan = _archive_get(pid)
    if plan is None:
        return 404, {"ok": False, "error": "unknown id"}
    entry = plan["meta"]
    host = _resolve_agent_host(host_override, entry)
    if host not in AGENTS:
        return 400, {"ok": False, "error": "unknown agent host: %s" % host}
    payload = {
        "markdown": plan.get("markdown") or "",
        "filename": entry.get("originFilename") or "",
        "title": entry.get("title") or "",
    }
    if project_dir:
        payload["projectDir"] = project_dir
    try:
        status, data = _agent_call(host, "/api/agent/%s" % action, payload)
    except Exception as exc:
        return 502, {"ok": False, "error": "agent unreachable: %r" % (exc,)}
    if not data.get("ok"):
        return 502, {"ok": False, "error": data.get("error") or "agent error"}
    if action == "reopen":
        return 200, {"ok": True, "host": host, "file": data.get("file"),
                     "port": data.get("port"), "url": data.get("url")}
    return 200, {"ok": True, "host": host, "jobId": data.get("jobId")}



PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>Plannotator sessions</title>
<style>
:root{color-scheme:dark light}
*{box-sizing:border-box}
body{margin:0;font:15px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
  background:#0b0e14;color:#e6e9ef}
header{padding:28px 24px 12px;max-width:900px;margin:0 auto}
h1{margin:0;font-size:20px;letter-spacing:.2px}
.sub{color:#8b93a7;font-size:13px;margin-top:4px}
.nav{display:flex;gap:14px;margin-top:10px;font-size:13.5px}
.nav a{color:#9fb0cc;text-decoration:none;border-bottom:1px dotted #2f3d57}
.nav a:hover{color:#e6e9ef}
main{max-width:900px;margin:0 auto;padding:12px 24px 48px}
.card{display:flex;align-items:center;gap:16px;justify-content:space-between;
  background:#121722;border:1px solid #1e2636;border-radius:12px;
  padding:16px 18px;margin:12px 0}
.card:hover{border-color:#2f3d57}
.t{font-weight:600;font-size:16px;word-break:break-word}
.meta{color:#8b93a7;font-size:12.5px;margin-top:4px}
.badge{display:inline-block;background:#1b2433;color:#9fb0cc;border-radius:999px;
  padding:1px 9px;font-size:11px;margin-right:6px;text-transform:lowercase}
a.open{white-space:nowrap;background:#2b60ff;color:#fff;text-decoration:none;
  padding:9px 16px;border-radius:9px;font-weight:600;font-size:13.5px}
a.open:hover{background:#3b70ff}
.empty{color:#8b93a7;padding:40px 0;text-align:center}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:#8b93a7;font-size:12px}
</style></head>
<body>
<header>
  <h1>Plannotator sessions</h1>
  <div class="sub">Live plan / review / annotate sessions on the workstations. Auto-refreshes every 5s.</div>
  <div class="nav"><a href="/plans">Plan archive &rarr;</a></div>
</header>
<main><div id="list"><div class="empty">Loading&hellip;</div></div></main>
<script>
const SUFFIX = __SUFFIX__;
function esc(s){return String(s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function ago(ms){const s=Math.max(0,Math.round((Date.now()-ms)/1000));
  if(s<60)return s+'s ago';const m=Math.round(s/60);if(m<60)return m+'m ago';
  return Math.round(m/60)+'h ago';}
function render(d){
  const el=document.getElementById('list');
  if(!d.sessions.length){el.innerHTML='<div class="empty">No active Plannotator sessions.</div>';return;}
  el.innerHTML=d.sessions.map(s=>{
    const url='https://p'+s.port+'.'+SUFFIX+'/';
    return '<div class="card"><div>'+
      '<div class="t">'+esc(s.title)+'</div>'+
      '<div class="meta"><span class="badge">'+esc(s.mode)+'</span>'+
      '<span class="badge">'+esc(s.hostLabel||s.host)+'</span>'+
      esc(s.project)+' &middot; '+ago(s.firstSeenMs)+'</div>'+
      '<div class="mono">p'+s.port+'.'+SUFFIX+'</div>'+
      '</div><a class="open" href="'+url+'">Open &rarr;</a></div>';
  }).join('');
}
async function tick(){
  try{const r=await fetch('api/sessions',{cache:'no-store'});const d=await r.json();
    d.sessions.forEach(s=>s.firstSeenMs=s.firstSeen*1000);render(d);}catch(e){}
}
tick();setInterval(tick,5000);
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "plan-hub/1.4"

    def log_message(self, *args):
        pass

    def _send(self, code, ctype, body):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _serve_plan_page(self):
        try:
            with open(PLAN_PAGE, "r", encoding="utf-8") as fh:
                return self._send(200, "text/html; charset=utf-8", fh.read())
        except Exception as exc:
            return self._send(500, "text/plain; charset=utf-8", "plan page unavailable: %r" % (exc,))

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)

        if path == "/healthz":
            return self._send(200, "text/plain; charset=utf-8", "ok")

        if path.rstrip("/") == "/api/archive/list":
            inc = (query.get("includeDeleted") or query.get("include_deleted") or ["0"])[0]
            include_deleted = str(inc).lower() not in ("", "0", "false", "no")
            return self._send(200, "application/json", json.dumps(_archive_list(include_deleted)))

        if path.rstrip("/") == "/api/archive/get":
            pid = (query.get("id") or [""])[0]
            if not pid:
                return self._send(400, "application/json", json.dumps({"error": "id is required"}))
            result = _archive_get(pid)
            if result is None:
                return self._send(404, "application/json", json.dumps({"error": "unknown id"}))
            return self._send(200, "application/json", json.dumps(result))

        if path.rstrip("/") == "/api/plans":
            plans = _archive_index(force="refresh" in query)
            return self._send(200, "application/json", json.dumps({"now": time.time(), "plans": plans}))

        if path == "/api/plans/raw":
            host = (query.get("host") or [""])[0]
            filename = (query.get("filename") or [""])[0]
            if not host or not filename:
                return self._send(400, "application/json", json.dumps({"error": "host and filename are required"}))
            try:
                md = _archive_plan(host, filename)
            except Exception as exc:
                return self._send(502, "text/plain; charset=utf-8", "archive fetch failed: %r" % (exc,))
            return self._send(200, "text/markdown; charset=utf-8", md)

        if path == "/plans/":
            # Keep the canonical URL slash-less so the page's relative
            # api/* fetches resolve against the site root.
            self.send_response(301)
            self.send_header("Location", "/plans")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path.rstrip("/") == "/plans":
            return self._serve_plan_page()

        with _lock:
            sessions = sorted(_sessions.values(), key=lambda s: s.get("firstSeen", 0), reverse=True)
        if path.rstrip("/") == "/api/sessions":
            return self._send(200, "application/json", json.dumps({"now": time.time(), "sessions": sessions}))

        if path.rstrip("/") == "/api/live/plan":
            host = (query.get("host") or [""])[0]
            port = (query.get("port") or [""])[0]
            if not host or not str(port).isdigit():
                return self._send(400, "application/json", json.dumps({"error": "host and port are required"}))
            try:
                data = json.loads(_fetch_bytes("http://%s:%s/api/plan" % (host, port)))
            except Exception as exc:
                return self._send(502, "application/json", json.dumps({"error": "session unreachable: %r" % (exc,)}))
            vi = data.get("versionInfo") if isinstance(data.get("versionInfo"), dict) else {}
            return self._send(200, "application/json", json.dumps({
                "host": host,
                "port": int(port),
                "plan": data.get("plan") or "",
                "mode": data.get("mode") or "",
                "project": (vi or {}).get("project")
                           or os.path.basename(str(data.get("projectRoot") or "").rstrip("/")) or "",
                "filePath": data.get("filePath") or "",
            }))

        try:
            with open(SESSIONS_PAGE, "r", encoding="utf-8") as fh:
                html = fh.read()
            # The page reads the public suffix from window.__SUFFIX__. Inject a
            # definition rather than token-substituting, because the page's own
            # JS contains the literal "window.__SUFFIX__" and a blind replace
            # would corrupt it.
            inject = "<script>window.__SUFFIX__=%s;</script>" % json.dumps(PUBLIC_SUFFIX)
            html = html.replace("</head>", inject + "\n</head>", 1) if "</head>" in html else inject + html
        except Exception:
            html = PAGE.replace("__SUFFIX__", json.dumps(PUBLIC_SUFFIX))
        return self._send(200, "text/html; charset=utf-8", html)

    def _read_json_body(self, sentinel=None):
        """Parse the request body. Returns sentinel on failure, else the value."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception:
            return sentinel

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        if path == "/api/sessions/decide":
            return self._decide()
        if path.startswith("/api/archive/"):
            return self._archive_post(path)
        return self._send(404, "application/json", json.dumps({"error": "not found"}))

    def _decide(self):
        payload = self._read_json_body()
        if payload is None:
            return self._send(400, "application/json", json.dumps({"error": "invalid JSON body"}))
        if not isinstance(payload, dict):
            return self._send(400, "application/json", json.dumps({"error": "JSON object expected"}))

        host = str(payload.get("host") or "")
        decision = str(payload.get("decision") or "").lower()
        feedback = str(payload.get("feedback") or "")
        if not host or decision not in ("approve", "deny"):
            return self._send(400, "application/json", json.dumps({"error": "host and decision (approve|deny) are required"}))
        try:
            port = int(payload.get("port"))
        except (TypeError, ValueError):
            return self._send(400, "application/json", json.dumps({"error": "a valid port is required"}))

        body = json.dumps({"draftGeneration": 0, "feedback": feedback}).encode("utf-8")
        req = Request("http://%s:%d/api/%s" % (host, port, decision),
                      data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(req, timeout=ARCHIVE_TIMEOUT) as resp:
                text = resp.read().decode("utf-8", "replace")
            return self._send(200, "application/json", json.dumps(
                {"ok": True, "status": resp.status, "upstream": text[:400]}))
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            return self._send(200, "application/json", json.dumps(
                {"ok": False, "status": exc.code, "error": detail or str(exc)}))
        except Exception as exc:
            return self._send(502, "application/json", json.dumps({"ok": False, "error": repr(exc)}))

    def _archive_post(self, path):
        action = path.rsplit("/", 1)[-1]
        if action not in ("import", "update", "tag", "delete", "restore",
                          "reopen", "apply"):
            return self._send(404, "application/json", json.dumps({"error": "not found"}))
        payload = self._read_json_body()
        if not isinstance(payload, dict):
            return self._send(400, "application/json", json.dumps({"error": "invalid JSON body"}))

        if action == "import":
            host = str(payload.get("host") or "").strip()
            filename = payload.get("filename")
            port = payload.get("port")
            if not host:
                return self._send(400, "application/json", json.dumps({"error": "host is required"}))
            if not filename and port is None:
                return self._send(400, "application/json", json.dumps({"error": "filename or port is required"}))
            try:
                if filename:
                    pid, created = _import_plan(
                        host, filename=str(filename),
                        project=str(payload.get("project") or ""),
                        title=payload.get("title"), tags=payload.get("tags"),
                        note=str(payload.get("note") or ""))
                else:
                    pid, created = _import_plan(
                        host, port=int(port), title=payload.get("title"),
                        tags=payload.get("tags"), note=str(payload.get("note") or ""))
            except (TypeError, ValueError):
                return self._send(400, "application/json", json.dumps({"error": "a valid port is required"}))
            except LookupError as exc:
                return self._send(400, "application/json", json.dumps({"error": str(exc)}))
            except Exception as exc:
                return self._send(502, "application/json", json.dumps({"error": "import failed: %r" % (exc,)}))
            return self._send(200, "application/json", json.dumps({"id": pid, "created": created}))

        if action == "update":
            pid = str(payload.get("id") or "")
            if not pid:
                return self._send(400, "application/json", json.dumps({"error": "id is required"}))
            fields = {k: payload[k] for k in ("markdown", "title", "note", "tags") if k in payload}
            try:
                meta = _archive_update(pid, fields)
            except ValueError as exc:
                return self._send(400, "application/json", json.dumps({"error": str(exc)}))
            if meta is None:
                return self._send(404, "application/json", json.dumps({"error": "unknown id"}))
            return self._send(200, "application/json", json.dumps(meta))

        if action in ("reopen", "apply"):
            pid = str(payload.get("id") or "")
            if not pid:
                return self._send(400, "application/json", json.dumps({"ok": False, "error": "id is required"}))
            project_dir = str(payload.get("projectDir") or "").strip()
            code, body = _proxy_workstation(pid, action, payload.get("host"),
                                            project_dir or None)
            return self._send(code, "application/json", json.dumps(body))

        ids = payload.get("ids")
        if not isinstance(ids, list) or not ids:
            return self._send(400, "application/json", json.dumps({"error": "ids must be a non-empty array"}))
        ids = [str(i) for i in ids]

        if action == "tag":
            add = payload.get("add") if payload.get("add") is not None else []
            remove = payload.get("remove") if payload.get("remove") is not None else []
            if not isinstance(add, list) or not isinstance(remove, list):
                return self._send(400, "application/json", json.dumps({"error": "add and remove must be arrays"}))
            updated, missing = _archive_tag(ids, add, remove)
            return self._send(200, "application/json", json.dumps({"plans": updated, "missing": missing}))

        if action == "delete":
            hard = bool(payload.get("hard"))
            count, missing = _archive_delete(ids, hard)
            return self._send(200, "application/json", json.dumps(
                {"deleted": count, "hard": hard, "missing": missing}))

        if action == "restore":
            count, missing = _archive_restore(ids)
            return self._send(200, "application/json", json.dumps(
                {"restored": count, "missing": missing}))

        return self._send(404, "application/json", json.dumps({"error": "not found"}))


def main():
    threading.Thread(target=_scanner, daemon=True).start()
    httpd = ThreadingHTTPServer((BIND, BIND_PORT), Handler)
    print("plan-hub listening on %s:%d, workstations=%s archives=%s" % (
        BIND, BIND_PORT, WORKSTATIONS, ARCHIVES), flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
