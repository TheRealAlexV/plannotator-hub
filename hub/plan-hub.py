#!/usr/bin/env python3
"""Plannotator session hub + plan archive browser.

Two jobs, both control-plane (this process never proxies live review traffic):

1. Live sessions: scan workstations over a fixed port range, probing
   GET /api/plan, and serve a landing page linking each session to its own
   port-indexed subdomain (https://p<port>.plan.example.com/).

2. Plan archive: aggregate each workstation's read-only archive server
   (GET /api/archive/plans, GET /api/archive/plan?filename=) and serve a
   browser that renders the archived markdown (and Mermaid) for review.

Config:
    HUB_WORKSTATIONS="10.0.0.10:19432-19463,10.0.0.11:19464-19495"
    HUB_ARCHIVES="10.0.0.10:8897,10.0.0.11:8897"
    HUB_LABELS="10.0.0.10=workstation-a,10.0.0.11=workstation-b"
    HUB_PLAN_PAGE=/opt/plan-hub/plans-page.html
"""
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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


WORKSTATIONS = _parse_workstations()
TARGETS = [(host, port) for (host, a, b) in WORKSTATIONS for port in range(a, b + 1)]
ARCHIVES = _parse_host_port_list(os.environ.get("HUB_ARCHIVES", ""))
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

_lock = threading.Lock()
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
    server_version = "plan-hub/1.2"

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
                html = fh.read().replace("__SUFFIX__", json.dumps(PUBLIC_SUFFIX))
        except Exception:
            html = PAGE.replace("__SUFFIX__", json.dumps(PUBLIC_SUFFIX))
        return self._send(200, "text/html; charset=utf-8", html)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path.rstrip("/") != "/api/sessions/decide":
            return self._send(404, "application/json", json.dumps({"error": "not found"}))
        try:
            length = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception:
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


def main():
    threading.Thread(target=_scanner, daemon=True).start()
    httpd = ThreadingHTTPServer((BIND, BIND_PORT), Handler)
    print("plan-hub listening on %s:%d, workstations=%s archives=%s" % (
        BIND, BIND_PORT, WORKSTATIONS, ARCHIVES), flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
