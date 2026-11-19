"""AWS Lambda entry point for the Instagram Reel comment-to-DM service.

The Lambda Function URL is public for Meta's webhook verification and event
delivery. Dashboard routes require HTTP Basic Auth. Access tokens and app
secrets are loaded from SSM Parameter Store SecureString values and never
returned to the browser or written to logs.
"""
import base64
from decimal import Decimal
import hashlib
import hmac
import html
import json
import logging
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_secrets = {}
_ssm = None
_table = None
ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)
VERSION = os.environ.get("IG_API_VERSION", "v26.0")
HOST = os.environ.get("IG_GRAPH_HOST", "graph.facebook.com")
BASE = f"https://{HOST}/{VERSION}"


def secret(name):
    value = os.environ.get(name, "").strip()
    if value:
        return value
    param = os.environ.get(name + "_PARAM", "").strip()
    if not param:
        return ""
    if param not in _secrets:
        global _ssm
        if _ssm is None:
            import boto3
            _ssm = boto3.client("ssm")
        try:
            _secrets[param] = _ssm.get_parameter(Name=param, WithDecryption=True)["Parameter"]["Value"]
        except Exception as e:
            if "ParameterNotFound" in str(e):
                return ""
            raise
    return _secrets[param]


def token():
    return secret("IG_ACCESS_TOKEN")


def table():
    global _table
    if _table is None:
        import boto3
        _table = boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])
    return _table


def api(method, path, payload=None):
    access_token = token()
    if not access_token:
        raise RuntimeError("Instagram access token is not configured")
    url = f"{BASE}/{path.lstrip('/')}"
    data = urllib.parse.urlencode(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                headers={"Authorization": f"Bearer {access_token}"})
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
    fields = "id,caption,media_type,media_product_type,permalink,timestamp,thumbnail_url,media_url"
    result = api("GET", f"{uid}/media?fields={fields}&limit=100")
    rows = result.get("data", [])
    while result.get("paging", {}).get("next") and len(rows) < 300:
        cursor = result.get("paging", {}).get("cursors", {}).get("after")
        if not cursor:
            break
        result = api("GET", f"{uid}/media?fields={fields}&limit=100&after={urllib.parse.quote(cursor)}")
        rows.extend(result.get("data", []))
    return [m for m in rows[:300] if m.get("media_product_type") == "REELS" or m.get("media_type") == "VIDEO"]


def rules_for():
    from boto3.dynamodb.conditions import Attr
    response = table().scan(FilterExpression=Attr("PK").begins_with("RULE#"), Limit=200)
    out = {}
    for item in response.get("Items", []):
        media_id = item.get("media_id")
        if media_id:
            out[media_id] = {"enabled": bool(item.get("enabled")), "keywords": item.get("keywords", ""),
                             "reply": item.get("reply", ""),
                             "public_reply": item.get("public_reply", ""),
                             "updated": item.get("updated", 0)}
    return out


def matching_rule(media_id, text):
    from boto3.dynamodb.conditions import Key
    response = table().get_item(Key={"PK": f"RULE#{media_id}", "SK": "RULE"}, ConsistentRead=True)
    rule = response.get("Item")
    if not rule or not rule.get("enabled") or not rule.get("reply", "").strip():
        return None
    keywords = [k.strip().casefold() for k in rule.get("keywords", "").split(",") if k.strip()]
    normalized = (text or "").casefold()
    if keywords and not any(re.search(r"(?<!\w)" + re.escape(k) + r"(?!\w)", normalized) for k in keywords):
        return None
    return rule


def receive_comment(comment_id, media_id, text):
    if not comment_id or not media_id:
        return "missing_comment_or_media_id"
    rule = matching_rule(str(media_id), text)
    if not rule:
        return "no_matching_rule"
    now = int(time.time())
    item = {"PK": f"EVENT#{comment_id}", "SK": "EVENT", "comment_id": str(comment_id),
            "media_id": str(media_id), "comment_text": (text or "")[:1000], "outcome": "sending",
            "received": now, "expires": now + 30 * 86400}
    from botocore.exceptions import ClientError
    try:
        table().put_item(Item=item, ConditionExpression="attribute_not_exists(PK)")
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return "duplicate"
        raise
    try:
        uid = os.environ.get("IG_USER_ID", "").strip()
        variations = [v.strip() for v in rule.get("reply", "").splitlines() if v.strip()]
        reply_text = random.choice(variations) if variations else rule.get("reply", "")
        sent = api("POST", f"{uid}/messages", {
            "recipient": json.dumps({"comment_id": str(comment_id)}),
            "message": json.dumps({"text": reply_text}),
        })
        outcome, detail = "sent", str(sent.get("message_id", "accepted"))
        public_lines = [v.strip() for v in (rule.get("public_reply") or "").splitlines() if v.strip()]
        public_text = random.choice(public_lines) if public_lines else ""   # rotate like the private reply, so replies do not repeat
        if public_text:
            try:
                api("POST", f"{comment_id}/replies", {"message": public_text})
            except Exception as public_error:
                detail += f" | public reply failed: {str(public_error)[:300]}"
    except Exception as e:
        outcome, detail = "failed", str(e)[:1000]
    table().update_item(Key={"PK": f"EVENT#{comment_id}", "SK": "EVENT"},
                        UpdateExpression="SET #o=:o, detail=:d",
                        ExpressionAttributeNames={"#o": "outcome"},
                        ExpressionAttributeValues={":o": outcome, ":d": detail})
    return outcome


def process_webhook(raw, signature):
    app_secret = secret("IG_APP_SECRET")
    expected = "sha256=" + hmac.new(app_secret.encode(), raw, hashlib.sha256).hexdigest()
    if not app_secret or not hmac.compare_digest(signature or "", expected):
        LOG.warning("webhook_rejected reason=invalid_signature app_secret_configured=%s", bool(app_secret))
        return response(403, {"error": "invalid webhook signature"})
    try:
        payload = json.loads(raw or b"{}")
        entries = payload.get("entry", [])
        counts = {"comments": 0, "matched": 0, "unmatched": 0, "duplicates": 0, "ignored": 0}
        fields = []
        for entry in entries:
            for change in entry.get("changes", []):
                fields.append(str(change.get("field", "unknown")))
                if change.get("field") != "comments":
                    continue
                counts["comments"] += 1
                value = change.get("value", {})
                if value.get("from", {}).get("id") == entry.get("id"):
                    counts["ignored"] += 1
                    continue
                media = value.get("media") or {}
                result = receive_comment(value.get("id"), media.get("id"), value.get("text", ""))
                if result in {"sent", "failed"}:
                    counts["matched"] += 1
                elif result == "duplicate":
                    counts["duplicates"] += 1
                else:
                    counts["unmatched"] += 1
        LOG.info("webhook_processed entries=%d fields=%s counts=%s", len(entries),
                 ",".join(sorted(set(fields))), json.dumps(counts, sort_keys=True))
        return response(200, {"received": True})
    except Exception as e:
        LOG.exception("webhook_processing_failed error_type=%s", type(e).__name__)
        return response(400, {"error": "invalid webhook event"})


def response(code, body, content_type="application/json; charset=utf-8", extra=None):
    def json_default(value):
        if isinstance(value, Decimal):
            return int(value) if value == value.to_integral_value() else float(value)
        raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")

    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, default=json_default)
    headers = {"Content-Type": content_type, "Cache-Control": "no-store"}
    if extra:
        headers.update(extra)
    return {"statusCode": code, "headers": headers, "body": text, "isBase64Encoded": False}


def authorized(headers):
    user = os.environ.get("DASHBOARD_USER", "")
    password = secret("DASHBOARD_PASSWORD")
    if not user or not password:
        return False
    supplied = headers.get("authorization", headers.get("Authorization", ""))
    try:
        scheme, encoded = supplied.split(" ", 1)
        actual = base64.b64decode(encoded, validate=True).decode()
        actual_user, actual_password = actual.split(":", 1)
    except Exception:
        return False
    return (scheme.lower() == "basic" and hmac.compare_digest(actual_user, user)
            and hmac.compare_digest(actual_password, password))


def dashboard_status():
    uid = os.environ.get("IG_USER_ID", "").strip()
    subscribed = False
    subscription_error = ""
    if token() and os.environ.get("PUBLIC_BASE_URL"):
        try:
            apps = api("GET", f"{uid}/subscribed_apps").get("data", [])
            subscribed = any("comments" in app.get("subscribed_fields", []) for app in apps)
        except Exception as e:
            subscription_error = str(e)[:500]
    return {"connected": bool(token() and uid), "subscribed": subscribed,
            "accountConfigured": bool(uid),
            "webhookConfigured": bool(secret("IG_APP_SECRET") and secret("IG_WEBHOOK_VERIFY_TOKEN")),
            "publicUrlConfigured": bool(os.environ.get("PUBLIC_BASE_URL")),
            "subscriptionError": subscription_error,
            "permissions": "Meta app permissions and app review must be confirmed in Meta Developers."}


def handler(event, context=None):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET").upper()
    path = event.get("rawPath", "/")
    headers = {str(k).lower(): str(v) for k, v in (event.get("headers") or {}).items()}
    query = event.get("queryStringParameters") or {}
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw)
    elif isinstance(raw, str):
        raw = raw.encode()

    if path == "/privacy" and method == "GET":
        try:
            return response(200, (ROOT / "privacy.html").read_text(), "text/html; charset=utf-8",
                            extra={"X-Robots-Tag": "index, follow"})
        except Exception:
            return response(500, {"error": "privacy policy unavailable"})
    # Meta can use the Function URL root as its callback. Retain the legacy route.
    if path in {"/", "/webhook"} and method == "GET" and query.get("hub.mode"):
        verify = secret("IG_WEBHOOK_VERIFY_TOKEN")
        if (query.get("hub.mode") == "subscribe" and verify and
                hmac.compare_digest(query.get("hub.verify_token", ""), verify)):
            return response(200, query.get("hub.challenge", ""), "text/plain; charset=utf-8")
        return response(403, {"error": "webhook verification failed"})
    if path in {"/", "/webhook"} and method == "POST":
        return process_webhook(raw, headers.get("x-hub-signature-256", ""))

    if not authorized(headers):
        return response(401, {"error": "authentication required"}, extra={"WWW-Authenticate": 'Basic realm="Reel Desk"'})
    if path == "/api/status" and method == "GET":
        return response(200, dashboard_status())
    if path == "/api/reels" and method == "GET":
        try:
            reels = list_media()
            return response(200, {"reels": reels, "rules": rules_for()})
        except Exception as e:
            return response(503, {"error": str(e)[:1000]})
    if path == "/api/events" and method == "GET":
        from boto3.dynamodb.conditions import Attr
        rows = table().scan(FilterExpression=Attr("PK").begins_with("EVENT#"), Limit=100).get("Items", [])
        rows.sort(key=lambda row: row.get("received", 0), reverse=True)
        return response(200, {"events": rows[:50]})
    if path == "/api/rule" and method == "POST":
        try:
            body = json.loads(raw or b"{}")
            media_id = str(body.get("media_id", "")).strip()
            keywords = str(body.get("keywords", "")).strip()[:500]
            reply = str(body.get("reply", "")).strip()[:1000]
            # Older dashboards omit this field. Preserve the saved reply in that
            # case; an explicit empty string still intentionally disables it.
            if "public_reply" in body:
                public_reply = str(body["public_reply"]).strip()[:1000]
            else:
                existing = table().get_item(Key={"PK": f"RULE#{media_id}", "SK": "RULE"},
                                            ConsistentRead=True).get("Item", {})
                public_reply = existing.get("public_reply", "")
            enabled = bool(body.get("enabled"))
            if not media_id or (enabled and not reply):
                return response(400, {"error": "Write the private reply before activating."})
            table().put_item(Item={"PK": f"RULE#{media_id}", "SK": "RULE", "media_id": media_id,
                                   "enabled": enabled, "keywords": keywords, "reply": reply,
                                   "public_reply": public_reply, "updated": int(time.time())})
            return response(200, {"ok": True})
        except Exception as e:
            return response(400, {"error": str(e)[:1000]})
    if path == "/api/subscribe" and method == "POST":
        if not os.environ.get("PUBLIC_BASE_URL") or not secret("IG_APP_SECRET") or not secret("IG_WEBHOOK_VERIFY_TOKEN"):
            return response(400, {"error": "Set the public HTTPS callback, app secret, and webhook verify token before subscribing."})
        try:
            uid = os.environ.get("IG_USER_ID", "").strip()
            result = api("POST", f"{uid}/subscribed_apps", {"subscribed_fields": "comments"})
            return response(200, {"ok": True, "result": result})
        except Exception as e:
            return response(502, {"error": str(e)[:1000]})
    if path in {"/", "/index.html"} and method == "GET":
        try:
            return response(200, (ROOT / "index.html").read_text(), "text/html; charset=utf-8")
        except Exception:
            return response(500, {"error": "dashboard unavailable"})
    return response(404, {"error": "not found"})


def lambda_handler(event, context):
    return handler(event, context)
