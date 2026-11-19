#!/usr/bin/env python3
"""Instagram Reel comment-to-DM dashboard and webhook receiver.

Run with IG_USER_ID, IG_ACCESS_TOKEN, IG_APP_SECRET, IG_WEBHOOK_VERIFY_TOKEN
and a public HTTPS deployment. SQLite state lives under .work/comment-dm/.
"""
import hashlib
import hmac
import json
import os
import random
import re
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA = Path(os.environ.get("COMMENT_DM_DATA", ROOT / ".work/comment-dm"))
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / "automation.sqlite3"
try:
    os.chmod(DATA, 0o700)
except OSError:
    pass
PORT = int(os.environ.get("PORT", "8766"))
VERSION = os.environ.get("IG_API_VERSION", "v26.0")
HOST = os.environ.get("IG_GRAPH_HOST", "graph.facebook.com")
BASE = f"https://{HOST}/{VERSION}"
LOCK = threading.Lock()


def db():
    c = sqlite3.connect(DB, timeout=15)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("""CREATE TABLE IF NOT EXISTS rules (
        media_id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
        keywords TEXT NOT NULL DEFAULT '', reply TEXT NOT NULL DEFAULT '', public_reply TEXT NOT NULL DEFAULT '',
        updated REAL NOT NULL)""")
    columns = {row[1] for row in c.execute("PRAGMA table_info(rules)")}
    if "public_reply" not in columns:
        c.execute("ALTER TABLE rules ADD COLUMN public_reply TEXT NOT NULL DEFAULT ''")
    c.execute("""CREATE TABLE IF NOT EXISTS events (
        comment_id TEXT PRIMARY KEY, media_id TEXT, comment_text TEXT,
        outcome TEXT NOT NULL, detail TEXT, received REAL NOT NULL)""")
    c.commit()
    try:
        os.chmod(DB, 0o600)
    except OSError:
        pass
    return c


def token():
    configured = os.environ.get("IG_ACCESS_TOKEN", "").strip()
    if configured:
        return configured
    # Reuse the existing local publishing connection without copying its
    # credential into project files. Hosted installs must provide env vars.
    if sys.platform == "darwin":
        import subprocess
        result = subprocess.run(
            ["security", "find-generic-password", "-a", "your-instagram-account", "-s",
             os.environ.get("IG_KEYCHAIN_SERVICE", "reel-desk.instagram-access-token"), "-w"],
            capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return result.stdout.strip()
    return ""


def api(method, path, payload=None):
    if not token():
        raise RuntimeError("IG_ACCESS_TOKEN is not configured")
    url = f"{BASE}/{path.lstrip('/')}"
    data = urllib.parse.urlencode(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                headers={"Authorization": f"Bearer {token()}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:1000]
        raise RuntimeError(f"Instagram API returned {e.code}: {detail}") from None


def list_media():
    uid = os.environ.get("IG_USER_ID", "").strip()
    if not uid:
        raise RuntimeError("IG_USER_ID is not configured")
    result = api("GET", f"{uid}/media?fields=id,caption,media_type,media_product_type,permalink,timestamp,thumbnail_url,media_url&limit=100")
    rows = result.get("data", [])
    while result.get("paging", {}).get("next") and len(rows) < 300:
        # Graph paging URLs already contain credentials/query data; never fetch them
        # directly. Use the returned cursor with the same authenticated endpoint.
        cursor = result["paging"].get("cursors", {}).get("after")
        if not cursor:
            break
        result = api("GET", f"{uid}/media?fields=id,caption,media_type,media_product_type,permalink,timestamp,thumbnail_url,media_url&limit=100&after={urllib.parse.quote(cursor)}")
        rows.extend(result.get("data", []))
    return [m for m in rows if m.get("media_product_type") == "REELS" or m.get("media_type") == "VIDEO"]


def rules_for(media_ids):
    con = db()
    out = {}
    for r in con.execute("SELECT * FROM rules"):
        out[r["media_id"]] = {"enabled": bool(r["enabled"]), "keywords": r["keywords"],
                               "reply": r["reply"], "public_reply": r["public_reply"], "updated": r["updated"]}
    con.close()
    return out


def matching_rule(media_id, text):
    con = db()
    rule = con.execute("SELECT * FROM rules WHERE media_id=? AND enabled=1", (str(media_id),)).fetchone()
    con.close()
    if not rule or not rule["reply"].strip():
        return None
    keywords = [k.strip().casefold() for k in rule["keywords"].split(",") if k.strip()]
    normalized = (text or "").casefold()
    if not keywords or not any(re.search(r"(?<!\w)" + re.escape(k) + r"(?!\w)", normalized) for k in keywords):
        return None
    return rule


def receive_comment(comment_id, media_id, text):
    if not comment_id or not media_id:
        return
    rule = matching_rule(media_id, text)
    if not rule:
        return
    con = db()
    try:
        with LOCK:
            con.execute("INSERT INTO events(comment_id,media_id,comment_text,outcome,received) VALUES(?,?,?,?,?)",
                        (str(comment_id), str(media_id), (text or "")[:1000], "sending", time.time()))
            con.commit()
    except sqlite3.IntegrityError:
        con.close()
        return
    con.close()
    try:
        uid = os.environ.get("IG_USER_ID", "").strip()
        # One reply per line is a variation; pick one at random so repeat
        # commenters don't all see identical wording.
        variations = [v.strip() for v in rule["reply"].splitlines() if v.strip()]
        reply_text = random.choice(variations) if variations else rule["reply"]
        # Meta's private reply is addressed to the comment ID and is limited to
        # one private reply per comment. Do not retry a failed/uncertain send.
        result = api("POST", f"{uid}/messages", {
            "recipient": json.dumps({"comment_id": str(comment_id)}),
            "message": json.dumps({"text": reply_text}),
        })
        outcome, detail = "sent", str(result.get("message_id", "accepted"))
        public_lines = [v.strip() for v in rule["public_reply"].splitlines() if v.strip()]
        public_reply = random.choice(public_lines) if public_lines else ""   # rotate, same as the private reply
        if public_reply:
            try:
                api("POST", f"{comment_id}/replies", {"message": public_reply})
            except Exception as public_error:
                detail += f" | public reply failed: {str(public_error)[:300]}"
    except Exception as e:
        outcome, detail = "failed", str(e)[:1000]
    con = db()
    con.execute("UPDATE events SET outcome=?,detail=? WHERE comment_id=?", (outcome, detail, str(comment_id)))
    con.commit()
    con.close()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def send_json(self, payload, code=200):
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def dashboard_auth(self):
        # Local installs bind only to loopback. A non-local deployment must
        # require credentials before exposing dashboard or rule-management APIs.
        if os.environ.get("HOST", "127.0.0.1") in {"127.0.0.1", "localhost", "::1"}:
            return True
        user, password = os.environ.get("DASHBOARD_USER", ""), os.environ.get("DASHBOARD_PASSWORD", "")
        if not user or not password:
            return False
        header = self.headers.get("Authorization", "")
        try:
            scheme, encoded = header.split(" ", 1)
            import base64
            supplied = base64.b64decode(encoded, validate=True).decode()
            actual_user, actual_password = supplied.split(":", 1)
        except Exception:
            actual_user, actual_password, scheme = "", "", ""
        return (scheme.lower() == "basic" and hmac.compare_digest(actual_user, user)
                and hmac.compare_digest(actual_password, password))

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path in {"/", "/webhook"} and q.get("hub.mode"):
            if q.get("hub.mode", [""])[0] == "subscribe" and hmac.compare_digest(
                    q.get("hub.verify_token", [""])[0], os.environ.get("IG_WEBHOOK_VERIFY_TOKEN", "")):
                body = q.get("hub.challenge", [""])[0].encode()
                self.send_response(200); self.send_header("Content-Length", str(len(body)))
                self.end_headers(); self.wfile.write(body); return
            return self.send_json({"error": "webhook verification failed"}, 403)
        if u.path == "/privacy":
            page = Path(__file__).with_name("privacy.html").read_bytes()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("X-Robots-Tag", "index, follow")
            self.send_header("Content-Length", str(len(page))); self.end_headers(); self.wfile.write(page); return
        if not self.dashboard_auth():
            self.send_response(401); self.send_header("WWW-Authenticate", 'Basic realm="Reel Desk"')
            self.send_header("Content-Length", "0"); self.end_headers(); return
        if u.path == "/api/status":
            uid = os.environ.get("IG_USER_ID", "")
            subscribed = False
            if token() and os.environ.get("PUBLIC_BASE_URL"):
                try:
                    subscriptions = api("GET", f"{uid}/subscribed_apps")
                    subscribed = any("comments" in app.get("subscribed_fields", [])
                                     for app in subscriptions.get("data", []))
                except Exception:
                    pass
            return self.send_json({"connected": bool(token() and uid), "subscribed": subscribed,
                "accountConfigured": bool(os.environ.get("IG_USER_ID", "")),
                "webhookConfigured": bool(os.environ.get("IG_APP_SECRET") and os.environ.get("IG_WEBHOOK_VERIFY_TOKEN")),
                "publicUrlConfigured": bool(os.environ.get("PUBLIC_BASE_URL")),
                "permissions": "Meta app permissions and app review must be confirmed in Meta Developers."})
        if u.path == "/api/reels":
            try:
                reels = list_media()
                return self.send_json({"reels": reels, "rules": rules_for([m["id"] for m in reels])})
            except Exception as e:
                return self.send_json({"error": str(e)}, 503)
        if u.path == "/api/events":
            con = db()
            rows = [dict(r) for r in con.execute("SELECT * FROM events ORDER BY received DESC LIMIT 50")]
            con.close()
            return self.send_json({"events": rows})
        if u.path == "/" or u.path == "/index.html":
            page = Path(__file__).with_name("index.html").read_bytes()
            self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(page))); self.end_headers(); self.wfile.write(page); return
        return self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        if u.path not in {"/", "/webhook"} and not self.dashboard_auth():
            self.send_response(401); self.send_header("WWW-Authenticate", 'Basic realm="Reel Desk"')
            self.send_header("Content-Length", "0"); self.end_headers(); return
        length = int(self.headers.get("Content-Length", "0"))
        if length > 1_000_000:
            return self.send_json({"error": "payload too large"}, 413)
        raw = self.rfile.read(length)
        if u.path in {"/", "/webhook"}:
            secret = os.environ.get("IG_APP_SECRET", "").encode()
            supplied = self.headers.get("X-Hub-Signature-256", "")
            expected = "sha256=" + hmac.new(secret, raw, hashlib.sha256).hexdigest()
            if not secret or not hmac.compare_digest(supplied, expected):
                return self.send_json({"error": "invalid webhook signature"}, 403)
            try:
                payload = json.loads(raw or b"{}")
                for entry in payload.get("entry", []):
                    for change in entry.get("changes", []):
                        if change.get("field") != "comments":
                            continue
                        value = change.get("value", {})
                        if value.get("from", {}).get("id") == entry.get("id"):
                            continue
                        media = value.get("media") or {}
                        receive_comment(value.get("id"), media.get("id"), value.get("text", ""))
                return self.send_json({"received": True})
            except Exception:
                return self.send_json({"error": "invalid webhook event"}, 400)
        if u.path == "/api/rule":
            try:
                body = json.loads(raw or b"{}")
                media_id = str(body.get("media_id", "")).strip()
                keywords = str(body.get("keywords", "")).strip()[:500]
                reply = str(body.get("reply", "")).strip()[:1000]
                public_reply = str(body.get("public_reply", "")).strip()[:1000]
                enabled = bool(body.get("enabled"))
                if not media_id or (enabled and (not keywords or not reply)):
                    return self.send_json({"error": "Choose at least one keyword and write the private reply before activating."}, 400)
                con = db()
                con.execute("INSERT INTO rules(media_id,enabled,keywords,reply,public_reply,updated) VALUES(?,?,?,?,?,?) "
                            "ON CONFLICT(media_id) DO UPDATE SET enabled=excluded.enabled,keywords=excluded.keywords,reply=excluded.reply,public_reply=excluded.public_reply,updated=excluded.updated",
                            (media_id, int(enabled), keywords, reply, public_reply, time.time()))
                con.commit(); con.close()
                return self.send_json({"ok": True})
            except Exception as e:
                return self.send_json({"error": str(e)}, 400)
        if u.path == "/api/subscribe":
            if not os.environ.get("PUBLIC_BASE_URL") or not os.environ.get("IG_APP_SECRET") or not os.environ.get("IG_WEBHOOK_VERIFY_TOKEN"):
                return self.send_json({"error": "Set the public HTTPS callback, app secret, and webhook verify token before subscribing."}, 400)
            try:
                uid = os.environ.get("IG_USER_ID", "")
                result = api("POST", f"{uid}/subscribed_apps", {"subscribed_fields": "comments"})
                return self.send_json({"ok": True, "result": result})
            except Exception as e:
                return self.send_json({"error": str(e)}, 502)
        return self.send_json({"error": "not found"}, 404)


if __name__ == "__main__":
    server = ThreadingHTTPServer((os.environ.get("HOST", "127.0.0.1"), PORT), Handler)
    print(f"Comment DM dashboard at http://{server.server_address[0]}:{PORT}")
    server.serve_forever()
