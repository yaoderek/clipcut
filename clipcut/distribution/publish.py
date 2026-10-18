"""POST /api/distribution/publish: turn an export into per-channel publish states in runs/<run>/publish.json.

Only YouTube Shorts can really upload, and only with YT_* env credentials (private upload, labelled
"uploaded (private)"). Every other channel, and Shorts without credentials, is "exported" (bundle ready
to upload by hand) or "scheduled (demo)" when schedule_at is given. Demo states never post anything.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path

from . import youtube
from .bundle import export_run, load_export, run_dir
from .specs import CHANNELS, DistributionError, parse_channels

DISCLAIMER = (
    "Only a status of \"uploaded (private)\" means a file reached a platform (YouTube, private visibility). "
    "\"exported\" means the bundle is ready to upload by hand; \"scheduled (demo)\" is a demo state and nothing will be posted."
)
_LOCK = threading.RLock()
_HISTORY_MAX = 100


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _path(d):
    return d / "publish.json"


def _read(d):
    p = _path(d)
    if p.is_file():
        try:
            data = json.loads(p.read_text())
            if isinstance(data, dict):
                data.setdefault("entries", {})
                data.setdefault("history", [])
                return data
        except json.JSONDecodeError:
            pass
    return {"run": d.name, "entries": {}, "history": []}


def _save(d, entries):
    with _LOCK:
        doc = _read(d)
        for e in entries:
            doc["entries"][e["channel"]] = e
            doc["history"].append(e)
        doc["history"] = doc["history"][-_HISTORY_MAX:]
        doc["run"] = d.name
        doc["updated_at"] = _now()
        doc["youtube_configured"] = youtube.configured()
        tmp = _path(d).with_suffix(".json.tmp")
        tmp.write_text(json.dumps(doc, indent=1))
        tmp.replace(_path(d))
        return doc


def _schedule(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise DistributionError("schedule_at must be an ISO 8601 string")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).isoformat()
    except ValueError as exc:
        raise DistributionError("schedule_at must be an ISO 8601 string") from exc


def _video_file(d, row):
    url = (row.get("video") or {}).get("url")
    if not url:
        return None
    rel = url.split(f"/runs/{d.name}/", 1)[-1]
    p = (d / rel).resolve()
    return p if p.is_file() and p.is_relative_to(d.resolve()) else None


def _base(export, row):
    return dict(channel=row["channel"], label=row["label"], export_id=export["export_id"], variant_id=row["variant_id"],
                bundle_url=export["bundle_url"] + "?channel=" + row["channel"], video_url=(row.get("video") or {}).get("url"),
                title=row["copy"].get("title"), caption=row["copy"]["caption"], at=_now())


def _do_upload(d, entry, path, row):
    try:
        res = youtube.upload(path, row["copy"].get("title") or row["copy"]["hook"], row["copy"]["caption"], row["copy"]["hashtags"])
        entry.update(status="uploaded (private)", state="uploaded", demo=False, error=None, at=_now(), **res,
                     note="Uploaded to YouTube as a private video. Change visibility in YouTube Studio to go live.")
    except (youtube.YouTubeError, OSError) as exc:
        entry.update(status="failed", state="failed", demo=False, error=str(exc), at=_now(), note="YouTube upload failed; the bundle is still available.")
    _save(d, [dict(entry)])
    return entry


def publish_run(run, export_id=None, variant_id=None, channels=None, schedule_at=None, *, root=None, background=False):
    """Same object as POST /api/distribution/publish. background=True runs YouTube uploads in a thread (status 'uploading')."""
    d = run_dir(run, root)
    when = _schedule(schedule_at)
    try:
        export = load_export(run, export_id, root=root)
    except DistributionError as exc:
        if export_id is not None or exc.status != 404:
            raise
        export = export_run(run, variant_id=variant_id, channels=channels, root=root)  # nothing exported yet: export first
    rows = {r["channel"]: r for r in export["channels"]}
    wanted = parse_channels(channels) if channels is not None else list(rows)
    missing = [c for c in wanted if c not in rows]
    if missing:
        raise DistributionError(f"export {export['export_id']} has no channel(s): {', '.join(missing)}", 404)
    yt = youtube.configured()
    entries, uploads = [], []
    for cid in wanted:
        row = rows[cid]
        e = _base(export, row)
        if cid == "shorts" and yt:
            path = _video_file(d, row)
            if path is None:
                e.update(status="failed", state="failed", demo=False, error="no video in the export bundle (" + row["video"]["status"] + ")",
                         note=row["video"].get("note") or "Re-export with the mp4 present.")
            else:
                e.update(status="uploading", state="uploading", demo=False, error=None, note="Private YouTube upload in progress.")
                uploads.append((e, path, row))
        elif when:
            e.update(status="scheduled (demo)", state="scheduled", demo=True, scheduled_for=when, error=None,
                     note="Demo schedule. Nothing will be posted automatically.")
        else:
            note = "Bundle ready to upload by hand."
            if cid == "shorts":
                note += " Set YT_CLIENT_ID, YT_CLIENT_SECRET and YT_REFRESH_TOKEN for a real private upload."
            e.update(status="exported", state="exported", demo=False, error=None, note=note)
        entries.append(e)
    _save(d, [dict(e) for e in entries])
    for e, path, row in uploads:
        if background:
            threading.Thread(target=_do_upload, args=(d, e, path, row), daemon=True).start()
        else:
            _do_upload(d, e, path, row)
    return dict(run=run, export_id=export["export_id"], youtube_configured=yt, disclaimer=DISCLAIMER,
                entries=[dict(e) for e in entries])


def publish_status(run, *, root=None):
    """Same object as GET /api/distribution/publish/{run}."""
    d = run_dir(run, root)
    with _LOCK:
        doc = _read(d)
    doc["youtube_configured"] = youtube.configured()
    doc["disclaimer"] = DISCLAIMER
    doc["channels"] = [doc["entries"][c] for c in CHANNELS if c in doc["entries"]]
    return doc
