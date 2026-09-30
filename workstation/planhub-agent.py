#!/usr/bin/env python3
"""Plan Hub workstation agent.

A small stdlib-only HTTP service that runs on each workstation and performs the
two actions the hub itself cannot: they need a local Plannotator CLI and local
process control.

Routes:
    GET  /healthz            unauthenticated liveness probe -> "ok"
    POST /api/agent/reopen   reopen an archived plan as a live review session
    POST /api/agent/apply    hand a plan to the configured implementation agent
    GET  /api/agent/jobs     list apply jobs (newest first)
    GET  /api/agent/job?id=  one apply job: liveness + last ~20 log lines

Security model (fail-closed):
  * Every route except /healthz requires the header
        X-Planhub-Token: <PLANHUB_AGENT_TOKEN>
    compared in constant time. The service REFUSES TO START when
    PLANHUB_AGENT_TOKEN is unset or empty.
  * The apply command comes ONLY from the environment
    (PLANHUB_APPLY_COMMAND); a request body can never name a command.
  * Filenames are reduced to a safe basename (no "/", "\\", "..").
  * Request bodies are size-capped and must be JSON.

Config (environment; see systemd/planhub-agent.service):
    PLANHUB_AGENT_TOKEN    required shared secret
    PLANHUB_AGENT_BIND     bind address (default 0.0.0.0)
    PLANHUB_AGENT_PORT     listen port (default 8898)
    PLANHUB_URL_HOST       this host's LAN IP -> PLANNOTATOR_URL_HOST
    PLANHUB_PORT_RANGE     this host's Plannotator port slice, e.g.
                           19432-19463 -> PLANNOTATOR_PORT
    PLANHUB_BIN            path to the plannotator binary
                           (default ~/.local/bin/plannotator)
    PLANHUB_APPLY_COMMAND  optional; "{plan}" and "{project}" are substituted.
                           When unset, /api/agent/apply returns 400.
    PLANHUB_HOME           state directory (default ~/.planhub)
"""
import hmac
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

# --- configuration ---------------------------------------------------------

TOKEN = (os.environ.get("PLANHUB_AGENT_TOKEN") or "").strip()
BIND = os.environ.get("PLANHUB_AGENT_BIND") or "0.0.0.0"
PORT = int(os.environ.get("PLANHUB_AGENT_PORT") or "8898")
URL_HOST = (os.environ.get("PLANHUB_URL_HOST") or "").strip()
PORT_RANGE = (os.environ.get("PLANHUB_PORT_RANGE") or "").strip()
PLANNATOR_BIN = os.path.expanduser(
    os.environ.get("PLANHUB_BIN") or "~/.local/bin/plannotator")
APPLY_COMMAND = (os.environ.get("PLANHUB_APPLY_COMMAND") or "").strip()
HOME_DIR = os.path.expanduser(os.environ.get("PLANHUB_HOME") or "~/.planhub")

WORK_DIR = os.path.join(HOME_DIR, "work")
APPLY_DIR = os.path.join(HOME_DIR, "apply")
JOBS_PATH = os.path.join(HOME_DIR, "jobs.json")

SESSIONS_DIR = os.path.expanduser("~/.plannotator/sessions")

MAX_MARKDOWN = 2 * 1024 * 1024          # 2 MB of submitted markdown
MAX_BODY = MAX_MARKDOWN + 256 * 1024    # JSON envelope slack
LOG_TAIL_LINES = 20

_jobs_lock = threading.Lock()
_slug_re = re.compile(r"[^a-z0-9]+")


# --- helpers ---------------------------------------------------------------

def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + ".%03dZ" % (
        int((time.time() % 1) * 1000))


def _slugify(text):
    return _slug_re.sub("-", (text or "").lower()).strip("-")[:80]


def _first_line(markdown):
    for line in (markdown or "").splitlines():
        s = line.strip()
        if s:
            return s.lstrip("#").strip()
    return ""


def _safe_name(filename, fallback):
    """Reduce filename (or fallback) to a plain basename without an extension."""
    raw = str(filename or "").strip()
    if raw:
        if "/" in raw or "\\" in raw or ".." in raw or raw in (".",):
            raise ValueError("filename must be a plain basename")
        raw = os.path.basename(raw)
    else:
        raw = fallback
    raw = re.sub(r"\.(md|markdown)$", "", raw, flags=re.I).strip()
    raw = re.sub(r"[^A-Za-z0-9._-]+", "-", raw).strip("-._")
    if not raw or raw in (".", ".."):
        raise ValueError("filename must be a non-empty basename")
    return raw[:120]


def _auto_name(markdown, title):
    base = _slugify(title or _first_line(markdown) or "plan") or "plan"
    return "%s-%s" % (base, time.strftime("%Y%m%d-%H%M%S"))


def _write_file(path, text):
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


def _pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    # A detached child we launched is reaped only when this process exits, so an
    # exited job can linger as a zombie — which still answers kill(pid, 0).
    # Read its state and treat zombies as exited.
    try:
        with open("/proc/%d/stat" % pid, "rb") as fh:
            data = fh.read()
        state = data[data.rfind(b")") + 2:data.rfind(b")") + 3]
        if state == b"Z":
            return False
    except OSError:
        pass
    return True


def _log_tail(path, lines=LOG_TAIL_LINES):
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except Exception:
        return ""
    out = data.decode("utf-8", "replace").splitlines()
    return "\n".join(out[-lines:])


# --- plannotator session discovery ----------------------------------------

def _read_sessions():
    out = {}
    try:
        names = os.listdir(SESSIONS_DIR)
    except OSError:
        return out
    for fn in names:
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(SESSIONS_DIR, fn), "r", encoding="utf-8") as fh:
                rec = json.load(fh)
        except Exception:
            continue
        if isinstance(rec, dict):
            out[fn] = rec
    return out


def _wait_for_session(pid, since_iso, baseline, timeout=10.0):
    """Best-effort discovery of the new session's port/url. Returns (port, url)."""
    deadline = time.time() + timeout
    exact = "%d.json" % pid
    while time.time() < deadline:
        recs = _read_sessions()
        hit = recs.get(exact)
        if hit is None:
            for fn, rec in recs.items():
                if fn in baseline:
                    continue
                if int(rec.get("pid") or -1) == pid:
                    hit = rec
                    break
                if str(rec.get("startedAt") or "") >= since_iso:
                    hit = rec
                    break
        if hit is not None:
            return hit.get("port"), hit.get("url")
        time.sleep(0.25)
    return None, None


# --- job store -------------------------------------------------------------

def _load_jobs():
    try:
        with open(JOBS_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("jobs"), list):
            return data["jobs"]
        if isinstance(data, list):
            return data
    except FileNotFoundError:
        return []
    except Exception as exc:
        print("jobs.json unreadable (%r); starting empty" % (exc,), flush=True)
    return []


def _save_jobs(jobs):
    _write_file(JOBS_PATH, json.dumps({"jobs": jobs}, indent=2, sort_keys=True))


def _append_job(job):
    with _jobs_lock:
        jobs = _load_jobs()
        jobs.append(job)
        _save_jobs(jobs)


def _job_view(job):
    out = dict(job)
    out["status"] = "running" if _pid_alive(job.get("pid")) else "exited"
    out["logTail"] = _log_tail(job.get("log"))
    return out


# --- actions ---------------------------------------------------------------

def launch_reopen(name, markdown, title):
    os.makedirs(WORK_DIR, exist_ok=True)
    plan_path = os.path.join(WORK_DIR, name + ".md")
    _write_file(plan_path, markdown)

    result_path = plan_path + ".result.json"
    # Plannotator requires --result-file to not already exist.
    if os.path.exists(result_path):
        try:
            os.unlink(result_path)
        except OSError:
            pass
    log_path = os.path.join(WORK_DIR, name + ".log")

    env = dict(os.environ)
    env.update({
        "PLANNOTATOR_REMOTE": "1",
        "PLANNOTATOR_URL_HOST": URL_HOST,
        "PLANNOTATOR_SKIP_BROWSER_OPEN": "1",
        "PLANNOTATOR_BIN": PLANNATOR_BIN,
    })
    if PORT_RANGE:
        env["PLANNOTATOR_PORT"] = PORT_RANGE

    argv = [PLANNATOR_BIN, "annotate", plan_path, "--gate", "--json",
            "--result-file", result_path]
    baseline = set(_read_sessions().keys())
    since_iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())

    with open(log_path, "ab") as logfh:
        proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=logfh,
            stderr=subprocess.STDOUT, env=env, start_new_session=True,
            cwd=os.path.expanduser("~"))

    port, url = _wait_for_session(proc.pid, since_iso, baseline)
    return {
        "file": plan_path,
        "pid": proc.pid,
        "port": port,
        "url": url,
        "log": log_path,
        "resultFile": result_path,
    }


def launch_apply(name, markdown, project_dir):
    if not APPLY_COMMAND:
        raise LookupError("apply is not configured on this workstation")
    os.makedirs(APPLY_DIR, exist_ok=True)
    plan_path = os.path.join(APPLY_DIR, name + ".md")
    _write_file(plan_path, markdown)

    job_id = "job_" + os.urandom(4).hex()
    log_path = os.path.join(APPLY_DIR, name + "." + job_id + ".log")
    cmd = APPLY_COMMAND.replace("{plan}", plan_path).replace(
        "{project}", project_dir or "")

    env = dict(os.environ)
    cwd = project_dir if project_dir and os.path.isdir(project_dir) else os.path.expanduser("~")
    with open(log_path, "ab") as logfh:
        proc = subprocess.Popen(
            ["bash", "-lc", cmd], stdin=subprocess.DEVNULL, stdout=logfh,
            stderr=subprocess.STDOUT, env=env, start_new_session=True, cwd=cwd)

    job = {
        "id": job_id,
        "plan": plan_path,
        "project": project_dir or "",
        "cmd": cmd,
        "startedAt": _now_iso(),
        "pid": proc.pid,
        "log": log_path,
        "status": "running",
    }
    _append_job(job)
    return job


# --- HTTP ------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "planhub-agent/1.0"
    protocol_version = "HTTP/1.1"

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

    def _json(self, code, obj):
        self._send(code, "application/json", json.dumps(obj))

    def _authorized(self):
        supplied = (self.headers.get("X-Planhub-Token") or "").encode("utf-8")
        expected = TOKEN.encode("utf-8")
        return bool(expected) and hmac.compare_digest(supplied, expected)

    def _read_json(self):
        raw_len = self.headers.get("Content-Length")
        if raw_len is None:
            return None, "Content-Length is required"
        try:
            length = int(raw_len)
        except ValueError:
            return None, "invalid Content-Length"
        if length < 0 or length > MAX_BODY:
            return None, "request body too large"
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception:
            return None, "invalid JSON body"
        if not isinstance(data, dict):
            return None, "JSON object expected"
        return data, None

    # -- routing ------------------------------------------------------------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        if path == "/healthz":
            return self._send(200, "text/plain; charset=utf-8", "ok")

        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})

        if path == "/api/agent/jobs":
            jobs = [_job_view(j) for j in _load_jobs()]
            jobs.sort(key=lambda j: j.get("startedAt") or "", reverse=True)
            return self._json(200, {"jobs": jobs})

        if path == "/api/agent/job":
            job_id = (query.get("id") or [""])[0]
            if not job_id:
                return self._json(400, {"error": "id is required"})
            for job in _load_jobs():
                if job.get("id") == job_id:
                    return self._json(200, _job_view(job))
            return self._json(404, {"error": "unknown job"})

        return self._json(404, {"error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        if not self._authorized():
            return self._json(401, {"error": "unauthorized"})

        if path == "/api/agent/reopen":
            return self._reopen()
        if path == "/api/agent/apply":
            return self._apply()
        return self._json(404, {"error": "not found"})

    # -- handlers -----------------------------------------------------------

    def _markdown_of(self, payload):
        markdown = payload.get("markdown")
        if not isinstance(markdown, str):
            return None, "markdown must be a string"
        if len(markdown.encode("utf-8")) > MAX_MARKDOWN:
            return None, "markdown exceeds the 2 MB limit"
        return markdown, None

    def _reopen(self):
        payload, err = self._read_json()
        if err:
            return self._json(400, {"error": err})
        markdown, err = self._markdown_of(payload)
        if err:
            return self._json(400, {"error": err})
        title = str(payload.get("title") or "")
        try:
            name = _safe_name(payload.get("filename"),
                              _auto_name(markdown, title))
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        try:
            result = launch_reopen(name, markdown, title)
        except Exception as exc:
            return self._json(500, {"error": "reopen failed: %r" % (exc,)})
        result["ok"] = True
        return self._json(200, result)

    def _apply(self):
        payload, err = self._read_json()
        if err:
            return self._json(400, {"error": err})
        markdown, err = self._markdown_of(payload)
        if err:
            return self._json(400, {"error": err})
        title = str(payload.get("title") or "")
        project_dir = str(payload.get("projectDir") or "").strip()
        if project_dir and not project_dir.startswith("/"):
            return self._json(400, {"error": "projectDir must be an absolute path"})
        if not APPLY_COMMAND:
            return self._json(400, {"error": "apply is not configured on this workstation"})
        try:
            name = _safe_name(payload.get("filename"),
                              _auto_name(markdown, title))
        except ValueError as exc:
            return self._json(400, {"error": str(exc)})
        try:
            job = launch_apply(name, markdown, project_dir)
        except LookupError as exc:
            return self._json(400, {"error": str(exc)})
        except Exception as exc:
            return self._json(500, {"error": "apply failed: %r" % (exc,)})
        return self._json(200, {"ok": True, "jobId": job["id"]})


def main():
    if not TOKEN:
        print("planhub-agent: refusing to start: PLANHUB_AGENT_TOKEN is unset or "
              "empty. Set it in /etc/planhub-agent.env.", file=sys.stderr)
        sys.exit(1)
    os.makedirs(WORK_DIR, exist_ok=True)
    os.makedirs(APPLY_DIR, exist_ok=True)
    httpd = ThreadingHTTPServer((BIND, PORT), Handler)
    print("planhub-agent listening on %s:%d (url_host=%s range=%s) apply=%s" % (
        BIND, PORT, URL_HOST or "-", PORT_RANGE or "-",
        "configured" if APPLY_COMMAND else "unconfigured"), flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()
