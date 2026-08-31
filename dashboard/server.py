#!/usr/bin/env python3
"""Star 助手 — one-click star all repos of a GitHub user.

Zero third-party dependency: stdlib only. Talks to the GitHub REST API
directly with a Personal Access Token obtained either from the local gh
CLI, the OAuth device flow, or pasted manually. The token is stored
DPAPI-encrypted under %LOCALAPPDATA%.

Usage:  pythonw server.py   (or double-click ../启动面板.bat)
Then:   http://127.0.0.1:8631/
"""
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PORT = 8631

if getattr(sys, "frozen", False):
    HERE = sys._MEIPASS
    APP_DIR = os.path.dirname(sys.executable)
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
    APP_DIR = HERE

DEVICE_CLIENT_ID = "178c6fc778ccc68e1d6a"  # GitHub CLI's public OAuth app id
OAUTH_SCOPES = "repo"

API_BASE = "https://api.github.com"

OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})?$")
REPO_PATH_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")

# device-flow sessions awaiting browser confirmation: device_code -> state
PENDING = {}

# in-process cache of my starred repos, refreshed on demand
STARRED_CACHE = {"set": None, "at": 0.0}


class ApiError(Exception):
    def __init__(self, message, status=500, extra=None):
        super().__init__(message)
        self.status = status
        self.extra = extra or {}


# --------------------------------------------------------------------------
# credentials (DPAPI-encrypted via PowerShell SecureString)
#
# We deliberately avoid ctypes/crypt32 here: calling CryptProtectData
# directly was crashing this machine's Anaconda Python silently. PowerShell's
# ConvertFrom-/ConvertTo-SecureString uses the same DPAPI (user-bound) and is
# rock solid. Decrypted values are cached in-process.
# --------------------------------------------------------------------------
if os.name == "nt":
    CRED_DIR = os.path.join(os.environ.get("LOCALAPPDATA", HERE), "StarAssistant")
else:
    CRED_DIR = os.path.join(os.path.expanduser("~"), ".star-assistant")
CRED_PATH = os.path.join(CRED_DIR, "credential.bin")

_CREDS_CACHE = {"data": None, "loaded": False}


def _ps_run(script):
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, encoding="utf-8",
    )
    if proc.returncode != 0:
        raise OSError((proc.stderr or "powershell failed").strip()[:300])
    return proc.stdout


def load_creds():
    """Return {'token','login','avatar'} or None."""
    if _CREDS_CACHE["loaded"]:
        return _CREDS_CACHE["data"]
    data = None
    if os.path.exists(CRED_PATH):
        try:
            if os.name == "nt":
                cred_quoted = CRED_PATH.replace("'", "''")
                script = (
                    "$e = [IO.File]::ReadAllText('" + cred_quoted + "').Trim(); "
                    "$s = ConvertTo-SecureString $e -ErrorAction Stop; "
                    "$b = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s); "
                    "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
                    "[Runtime.InteropServices.Marshal]::PtrToStringAuto($b)"
                )
                plain = _ps_run(script)
            else:
                with open(CRED_PATH, encoding="utf-8") as f:
                    plain = f.read()
            candidate = json.loads(plain)
            if candidate.get("token"):
                data = candidate
        except Exception:
            data = None
    _CREDS_CACHE["data"] = data
    _CREDS_CACHE["loaded"] = True
    return data


def save_creds(token, login, avatar):
    data = {
        "token": token,
        "login": login,
        "avatar": avatar,
        "saved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    plain = json.dumps(data)
    os.makedirs(CRED_DIR, exist_ok=True)
    if os.name == "nt":
        tmp_path = CRED_PATH + ".plain.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(plain)
        t_quoted = tmp_path.replace("'", "''")
        c_quoted = CRED_PATH.replace("'", "''")
        _ps_run(
            "$j = [IO.File]::ReadAllText('" + t_quoted + "', [Text.Encoding]::UTF8); "
            "$s = ConvertTo-SecureString $j -AsPlainText -Force; "
            "[IO.File]::WriteAllText('" + c_quoted + "', (ConvertFrom-SecureString $s))"
        )
        try:
            os.remove(tmp_path)
        except FileNotFoundError:
            pass
    else:
        with open(CRED_PATH, "w", encoding="utf-8") as f:
            f.write(plain)
    _CREDS_CACHE["data"] = data
    _CREDS_CACHE["loaded"] = True


def clear_creds():
    try:
        os.remove(CRED_PATH)
    except FileNotFoundError:
        pass
    _CREDS_CACHE["data"] = None
    _CREDS_CACHE["loaded"] = True
    STARRED_CACHE["set"] = None
    STARRED_CACHE["at"] = 0.0


# --------------------------------------------------------------------------
# GitHub HTTP helper
#
# Network note (China): Python's TLS handshake to GitHub often gets reset
# by the GFW/proxy while curl/gh pass. If direct fails we retry through a
# locally running Clash-style HTTP proxy on common ports and remember which
# route worked.
# --------------------------------------------------------------------------
_WORKING_PROXY = {"url": None}
PROXY_CANDIDATES = ["http://127.0.0.1:7897", "http://127.0.0.1:7890",
                    "http://127.0.0.1:10809", "http://127.0.0.1:1080"]


def fetch_text(url, method, headers, data, timeout):
    routes = []
    if _WORKING_PROXY["url"] is not None:
        routes.append(_WORKING_PROXY["url"])
    routes.append(None)
    routes.extend(PROXY_CANDIDATES)

    last_err = None
    seen = set()
    for proxy in routes:
        key = proxy or ""
        if key in seen:
            continue
        seen.add(key)
        handler = (
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            if proxy else urllib.request.ProxyHandler({})
        )
        try:
            req = urllib.request.Request(url, method=method)
            for hkey, hval in (headers or {}).items():
                req.add_header(hkey, hval)
            opener = urllib.request.build_opener(handler)
            with opener.open(req, data=data, timeout=timeout) as resp:
                _WORKING_PROXY["url"] = proxy
                return resp.read().decode("utf-8"), resp.status
        except urllib.error.HTTPError as exc:
            # real response arrived: route is fine, surface the status
            try:
                detail = exc.read().decode("utf-8")[:300]
            except Exception:
                detail = ""
            _WORKING_PROXY["url"] = proxy
            return detail, exc.code
        except Exception as exc:
            last_err = exc
            continue
    raise ApiError(f"network error: {last_err}", 502)


def github(method, path, token, body=None, timeout=25):
    """Call GitHub API; raises ApiError on HTTP >= 400."""
    url = path if path.startswith("http") else API_BASE + path
    headers = {
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "Accept": "application/json",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif method in ("POST", "PUT", "PATCH", "DELETE"):
        data = b""
    raw, status = fetch_text(url, method, headers, data, timeout)
    if status >= 400:
        raise ApiError(f"GitHub API {status}: {raw or 'request failed'}", 502)
    return json.loads(raw) if raw.strip() else None


def github_status(method, path, token, body=None, timeout=25, accept="application/json"):
    """Like github() but never raises on HTTP status: returns (parsed|None, status)."""
    url = path if path.startswith("http") else API_BASE + path
    headers = {
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "Accept": accept,
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif method in ("POST", "PUT", "PATCH", "DELETE"):
        data = b""
    raw, status = fetch_text(url, method, headers, data, timeout)
    try:
        parsed = json.loads(raw) if raw and raw.strip() else None
    except Exception:
        parsed = None
    return parsed, status


def oauth_post(path, body, timeout=25):
    """POST to github.com OAuth endpoints (not api.github.com)."""
    from urllib.parse import urlencode
    raw, status = fetch_text(
        path, "POST",
        {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        urlencode(body).encode("utf-8"),
        timeout,
    )
    parsed = json.loads(raw) if raw.strip() else {}
    if status >= 400 and "error" not in parsed:
        raise ApiError(f"oauth error {status}", 502)
    return parsed


# --------------------------------------------------------------------------
# auth flows
# --------------------------------------------------------------------------
def validate_token(token):
    user = github("GET", "/user", token)
    return {"login": user.get("login"), "avatar": user.get("avatar_url", "")}


def auth_start():
    resp = oauth_post(
        "https://github.com/login/device/code",
        {"client_id": DEVICE_CLIENT_ID, "scope": OAUTH_SCOPES},
    )
    if "device_code" not in resp:
        raise ApiError(f"device flow init failed: {resp}")
    PENDING[resp["device_code"]] = {
        "expires": time.time() + int(resp.get("expires_in", 900)),
        "next_poll": 0,
        "interval": max(3, int(resp.get("interval", 5))),
    }
    return {
        "device_code": resp["device_code"],
        "user_code": resp["user_code"],
        "verify_url": resp.get("verification_uri", "https://github.com/login/device"),
    }


def auth_poll(device_code):
    state = PENDING.get(device_code)
    if not state:
        raise ApiError("unknown or expired pairing session", 400)
    if time.time() > state["expires"]:
        PENDING.pop(device_code, None)
        raise ApiError("expired", 400)

    wait = state["interval"]
    if time.time() < state["next_poll"]:
        return {"status": "pending"}
    state["next_poll"] = time.time() + wait

    resp = oauth_post(
        "https://github.com/login/oauth/access_token",
        {
            "client_id": DEVICE_CLIENT_ID,
            "device_code": device_code,
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        },
    )
    err = resp.get("error")
    if err == "authorization_pending":
        return {"status": "pending"}
    if err == "slow_down":
        state["interval"] += 5
        return {"status": "pending"}
    if err == "expired_token":
        PENDING.pop(device_code, None)
        raise ApiError("expired", 400)
    if err:
        PENDING.pop(device_code, None)
        raise ApiError(f"denied: {err}", 400)

    PENDING.pop(device_code, None)
    token = resp["access_token"]
    user = validate_token(token)
    save_creds(token, user["login"], user["avatar"])
    return {"status": "ok", "login": user["login"], "avatar": user["avatar"]}


def auth_import_gh():
    proc = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, encoding="utf-8")
    token = (proc.stdout or "").strip()
    if proc.returncode != 0 or not token:
        raise ApiError("本机 gh CLI 未登录或未安装，无法导入", 400)
    user = validate_token(token)
    save_creds(token, user["login"], user["avatar"])
    return {"status": "ok", "login": user["login"], "avatar": user["avatar"]}


def auth_paste_token(token):
    token = (token or "").strip()
    if not token or len(token) < 20 or any(c in token for c in "\r\n"):
        raise ApiError("token 无效", 400)
    user = validate_token(token)
    save_creds(token, user["login"], user["avatar"])
    return {"status": "ok", "login": user["login"], "avatar": user["avatar"]}


def require_creds():
    creds = load_creds()
    if not creds:
        raise ApiError("not logged in", 401, extra={"auth_required": True})
    return creds["token"]


# --------------------------------------------------------------------------
# star assistant core
# --------------------------------------------------------------------------
def parse_owner(raw):
    """Accept 'user', 'github.com/user', 'https://github.com/user?tab=repositories'."""
    text = (raw or "").strip()
    if not text:
        raise ApiError("请输入用户主页地址或用户名", 400)
    text = text.split("#", 1)[0].split("?", 1)[0]
    text = text.rstrip("/")
    m = re.match(r"^(?:https?://)?(?:www\.)?github\.com/(.+)$", text, re.I)
    if m:
        text = m.group(1)
    text = text.rstrip("/")
    owner = text.split("/", 1)[0].strip()
    if not OWNER_RE.match(owner):
        raise ApiError(f"用户名格式不对：{owner}", 400)
    return owner


def my_starred_set(token, max_pages=50, ttl=120):
    if (STARRED_CACHE["set"] is not None
            and time.time() - STARRED_CACHE["at"] < ttl):
        return STARRED_CACHE["set"]
    starred = set()
    for page in range(1, max_pages + 1):
        data = github(
            "GET", f"/user/starred?per_page=100&page={page}", token) or []
        for item in data:
            full = item.get("full_name")
            if full:
                starred.add(full.lower())
        if len(data) < 100:
            break
    STARRED_CACHE["set"] = starred
    STARRED_CACHE["at"] = time.time()
    return starred


def starred_cache_invalidate():
    STARRED_CACHE["set"] = None
    STARRED_CACHE["at"] = 0.0


def list_target(owner, include_forks=True):
    token = require_creds()
    user, st = github_status("GET", f"/users/{owner}", token)
    if st == 404:
        raise ApiError(f"GitHub 上没有这个用户：{owner}", 404)
    if st >= 400:
        raise ApiError(f"查询用户失败(HTTP {st})", 502)

    repos = []
    for page in range(1, 21):  # up to 2000 repos
        data = github(
            "GET",
            f"/users/{owner}/repos?per_page=100&page={page}&sort=updated",
            token,
        ) or []
        for r in data:
            repos.append({
                "full_name": r.get("full_name", ""),
                "name": r.get("name", ""),
                "description": r.get("description") or "",
                "fork": bool(r.get("fork")),
                "stars": int(r.get("stargazers_count", 0)),
                "language": r.get("language") or "",
                "archived": bool(r.get("archived")),
                "url": r.get("html_url", ""),
            })
        if len(data) < 100:
            break

    starred = my_starred_set(token)
    for r in repos:
        r["starred"] = r["full_name"].lower() in starred

    if not include_forks:
        repos = [r for r in repos if not r["fork"]]

    return {
        "owner": user.get("login", owner),
        "avatar": user.get("avatar_url", ""),
        "profile_url": user.get("html_url", f"https://github.com/{owner}"),
        "bio": user.get("bio") or "",
        "followers": int(user.get("followers", 0)),
        "created_at": (user.get("created_at") or "")[:10],
        "public_repos": int(user.get("public_repos", 0)),
        "total": len(repos),
        "already": sum(1 for r in repos if r["starred"]),
        "total_stars": sum(r["stars"] for r in repos),
        "repos": repos,
    }


def recent_stars(limit=15):
    """My most recently starred repos (star+json media type carries starred_at)."""
    token = require_creds()
    data, st = github_status(
        "GET", f"/user/starred?per_page={limit}&page=1", token,
        accept="application/vnd.github.star+json")
    if st >= 400:
        raise ApiError(f"读取 star 列表失败(HTTP {st})", 502)
    out = []
    for item in data or []:
        repo = item.get("repo", {}) or {}
        out.append({
            "full_name": repo.get("full_name", ""),
            "url": repo.get("html_url", ""),
            "description": (repo.get("description") or "")[:80],
            "stars": int(repo.get("stargazers_count", 0)),
            "starred_at": item.get("starred_at", ""),
        })
    return out


# --------------------------------------------------------------------------
# batch-operation history, stored locally (not on GitHub)
# --------------------------------------------------------------------------
HISTORY_PATH = os.path.join(CRED_DIR, "history.json")
HISTORY_MAX = 200


def history_load():
    try:
        with open(HISTORY_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def history_append(entry):
    items = history_load()
    items.insert(0, entry)
    items = items[:HISTORY_MAX]
    os.makedirs(CRED_DIR, exist_ok=True)
    with open(HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=1)
    return items


def record_batch(target, action, ok, fail):
    entry = {
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "target": target,
        "action": action,
        "ok": int(ok),
        "fail": int(fail),
    }
    return history_append(entry)


def star_one(full_name, action):
    token = require_creds()
    parts = full_name.strip().split("/")
    if len(parts) != 2 or not all(REPO_PATH_RE.match(p) for p in parts):
        raise ApiError(f"仓库名格式不对：{full_name}", 400)
    owner, repo = parts[0], parts[1]
    method = "PUT" if action == "star" else "DELETE"
    _, st = github_status(method, f"/user/starred/{owner}/{repo}", token)
    if st not in (204, 200, 404):
        raise ApiError(f"{action} 失败(HTTP {st})", 502)
    starred_cache_invalidate()
    return {"ok": True, "repo": f"{owner}/{repo}",
            "starred": action == "star" and st != 404}


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------
class Handler(SimpleHTTPRequestHandler):
    server_version = "StarAssistant/1.0"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=HERE, **kwargs)

    def log_message(self, fmt, *args):  # keep console quiet & ASCII-safe
        pass

    def _send(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _api_error(self, exc):
        status = exc.status if isinstance(exc, ApiError) else 500
        payload = {"error": str(exc)}
        payload.update(getattr(exc, "extra", {}) or {})
        try:
            self._send(status, payload)
        except Exception:
            pass

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ApiError("missing request body", 400)
        return json.loads(self.rfile.read(length).decode("utf-8"))

    # ---------------- GET ----------------
    def do_GET(self):
        try:
            route = self.path.split("?", 1)[0]
            if route == "/api/me":
                creds = load_creds()
                if creds:
                    self._send(200, {
                        "logged_in": True,
                        "login": creds.get("login"),
                        "avatar": creds.get("avatar"),
                    })
                else:
                    self._send(200, {"logged_in": False})
            elif route == "/api/target":
                require_creds()
                from urllib.parse import parse_qs, unquote
                qs = parse_qs(self.path.split("?", 1)[1]) if "?" in self.path else {}
                owner = parse_owner(unquote(qs.get("owner", [""])[0]))
                include_forks = qs.get("forks", ["1"])[0] not in ("0", "false")
                self._send(200, list_target(owner, include_forks))
            elif route == "/api/recent_stars":
                require_creds()
                self._send(200, {"items": recent_stars()})
            elif route == "/api/history":
                require_creds()
                self._send(200, {"items": history_load()})
            else:
                super().do_GET()  # static files; '/' serves index.html
        except BrokenPipeError:
            pass
        except Exception as exc:
            self._api_error(exc)

    # ---------------- POST ----------------
    def do_POST(self):
        try:
            route = self.path.split("?", 1)[0]
            if route == "/api/auth/start":
                self._send(200, auth_start())
            elif route == "/api/auth/poll":
                self._send(200, auth_poll(self._read_body().get("device_code")))
            elif route == "/api/auth/import_gh":
                self._send(200, auth_import_gh())
            elif route == "/api/auth/paste":
                self._send(200, auth_paste_token(self._read_body().get("token")))
            elif route == "/api/auth/logout":
                clear_creds()
                self._send(200, {"ok": True})
            elif route == "/api/star":
                require_creds()
                body = self._read_body()
                action = "unstar" if body.get("action") == "unstar" else "star"
                self._send(200, star_one(body.get("repo") or "", action))
            elif route == "/api/history":
                require_creds()
                body = self._read_body()
                items = record_batch(
                    str(body.get("target") or "")[:120],
                    "unstar" if body.get("action") == "unstar" else "star",
                    int(body.get("ok") or 0),
                    int(body.get("fail") or 0),
                )
                self._send(200, {"items": items})
            else:
                self._send(404, {"error": "not found"})
        except BrokenPipeError:
            pass
        except json.JSONDecodeError as exc:
            self._api_error(ApiError(f"bad JSON body: {exc}", 400))
        except Exception as exc:
            self._api_error(exc)


def open_window_later():
    def run():
        try:
            edge_candidates = [
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            ]
            # dedicated profile so --window-size is honored (shared profile ignores flags)
            profile = os.path.join(CRED_DIR, "edge-profile")
            os.makedirs(profile, exist_ok=True)
            for exe in edge_candidates:
                if os.path.exists(exe):
                    subprocess.Popen([
                        exe,
                        f"--app=http://127.0.0.1:{PORT}/",
                        f"--user-data-dir={profile}",
                        "--window-size=980,860",
                        "--no-first-run",
                        "--no-default-browser-check",
                    ])
                    return
            webbrowser.open(f"http://127.0.0.1:{PORT}/")
        except Exception:
            pass
    threading.Timer(1.2, run).start()


def main(argv):
    serve_only = "--serve" in argv
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), partial(Handler))
    print(f"star-assistant dashboard at http://127.0.0.1:{PORT}/", flush=True)
    if not serve_only:
        open_window_later()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main(sys.argv[1:])
