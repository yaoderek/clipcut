"""YouTube Data API v3 resumable upload with the standard library only.

Enabled only when YT_CLIENT_ID, YT_CLIENT_SECRET and YT_REFRESH_TOKEN are all set (an OAuth client with
the youtube.upload scope and a refresh token for the target channel). Uploads are always private.
Tests replace `_http`; nothing here runs without credentials.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

TOKEN_URL = "https://oauth2.googleapis.com/token"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status"
ENV_KEYS = ("YT_CLIENT_ID", "YT_CLIENT_SECRET", "YT_REFRESH_TOKEN")
TIMEOUT_S = 300


class YouTubeError(Exception):
    pass


def configured():
    return all(os.environ.get(k) for k in ENV_KEYS)


def _http(method, url, data=None, headers=None, timeout=TIMEOUT_S):
    """(status, headers{lowercase: value}, body bytes). Non-2xx is returned, not raised."""
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {k.lower(): v for k, v in (exc.headers or {}).items()}, exc.read() or b""


def _detail(body):
    try:
        data = json.loads(body or b"{}")
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err)[:300]
        return str(data.get("error_description") or err or body[:200])[:300]
    except (ValueError, AttributeError):
        return (body or b"")[:200].decode("utf-8", "replace")


def access_token():
    form = urllib.parse.urlencode({
        "client_id": os.environ["YT_CLIENT_ID"], "client_secret": os.environ["YT_CLIENT_SECRET"],
        "refresh_token": os.environ["YT_REFRESH_TOKEN"], "grant_type": "refresh_token",
    }).encode()
    status, _h, body = _http("POST", TOKEN_URL, form, {"Content-Type": "application/x-www-form-urlencoded"}, timeout=30)
    if status != 200:
        raise YouTubeError(f"token refresh failed ({status}): {_detail(body)}")
    try:
        return json.loads(body)["access_token"]
    except (ValueError, KeyError) as exc:
        raise YouTubeError("token refresh returned no access_token") from exc


def _clean(text, limit):
    text = re.sub(r"[<>]", "", str(text or "")).strip()
    return text[:limit]


def metadata(title, description, tags):
    return {
        "snippet": {
            "title": _clean(title, 100) or "ClipCut short",
            "description": _clean(description, 5000),
            "tags": [t.lstrip("#") for t in tags or []][:15],
            "categoryId": "22",
        },
        "status": {"privacyStatus": "private", "selfDeclaredMadeForKids": False},
    }


def upload(path, title, description, tags):
    """Upload `path` as a private video. Returns {"video_id", "url", "privacy": "private"}."""
    size = path.stat().st_size
    token = access_token()
    body = json.dumps(metadata(title, description, tags)).encode()
    status, headers, resp = _http("POST", UPLOAD_URL, body, {
        "Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4",
    }, timeout=60)
    location = headers.get("location")
    if status != 200 or not location:
        raise YouTubeError(f"upload session failed ({status}): {_detail(resp)}")
    status, _h, resp = _http("PUT", location, path.read_bytes(), {
        "Authorization": f"Bearer {token}", "Content-Type": "video/mp4", "Content-Length": str(size),
    })
    if status not in (200, 201):
        raise YouTubeError(f"upload failed ({status}): {_detail(resp)}")
    try:
        vid = json.loads(resp)["id"]
    except (ValueError, KeyError) as exc:
        raise YouTubeError("upload returned no video id") from exc
    return {"video_id": vid, "url": f"https://www.youtube.com/shorts/{vid}", "privacy": "private"}
