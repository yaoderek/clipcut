"""POST /api/distribution/export: per-channel bundles under runs/<run>/distribution/<export_id>/."""
from __future__ import annotations

import json
import re
import secrets
import shutil
import threading
import time
import zipfile
from pathlib import Path

from .. import llm_persona
from .captions import cue_count, to_srt, to_vtt
from .copygen import generate_copy, post_text
from .render import prepare_video
from .specs import CHANNELS, DistributionError, aspect_of, best_target, channel_id, parse_channels

DISCLAIMER = (
    "Export bundles are ready-to-upload files. Copy is template text seeded from the variant's own hook line, "
    "or Grok-written when copy.source is \"llm\". Nothing here was posted unless publish says so."
)
_RUN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_ID_RE = re.compile(r"^ex_[0-9]{8}_[0-9]{6}_[0-9a-f]{4}$")
_LOCK = threading.Lock()


def runs_root(root=None):
    return Path(root) if root is not None else Path(__file__).resolve().parents[2] / "runs"


def run_dir(run, root=None):
    if not isinstance(run, str) or not _RUN_RE.fullmatch(run):
        raise DistributionError("run must be a single path segment (letters, digits, ., _, -)")
    d = runs_root(root) / run
    if not d.is_dir():
        raise DistributionError(f"no such run: {run}", 404)
    return d


def load_manifests(d):
    out = {}
    for p in sorted(d.glob("v[0-9][0-9].json")):
        try:
            m = json.loads(p.read_text())
        except json.JSONDecodeError:
            continue
        if isinstance(m, dict):
            m.setdefault("variant_id", p.stem)
            out[str(m["variant_id"])] = m
    if not out:
        raise DistributionError(f"run {d.name} has no vNN.json variants", 404)
    return out


def ranking(d, manifests):
    """Variant ids best-first: results.json order (scorer rank) when present, else the persona heuristic aggregate."""
    res = d / "results.json"
    if res.is_file():
        try:
            ids = [r["variant_id"] for r in json.loads(res.read_text()).get("results", []) if r.get("variant_id") in manifests]
            rest = [v for v in manifests if v not in ids]
            if ids:
                return ids + rest
        except (json.JSONDecodeError, AttributeError, KeyError, TypeError):
            pass
    from ..persona_models import heuristic_variant
    agg = {}
    for vid, m in manifests.items():
        try:
            agg[vid] = heuristic_variant(m)["aggregate"]
        except Exception:  # noqa: BLE001
            agg[vid] = 0.0
    return sorted(manifests, key=lambda v: (-agg[v], v))


def _hook_key(m):
    return (m.get("recipe") or {}).get("hook") or m.get("hook_text")


def select_variant(manifests, order, cid, variant_id=None):
    """(variant_id, selection, needs_reframe).

    selection: requested | requested_reframe | sibling_aspect | channel_target | aspect_match | top_reframe.
    With variant_id: use it when its aspect fits; else a sibling with the same hook in a fitting aspect;
    else reframe it. Without: best-ranked variant cut for this channel, else best in a fitting aspect,
    else the top variant reframed."""
    accepted = CHANNELS[cid]["aspects"]
    if variant_id is not None:
        base = manifests[variant_id]
        if aspect_of(base) in accepted:
            return variant_id, "requested", False
        key = _hook_key(base)
        for vid in order:
            if key and _hook_key(manifests[vid]) == key and aspect_of(manifests[vid]) in accepted:
                return vid, "sibling_aspect", False
        return variant_id, "requested_reframe", True
    for vid in order:
        m = manifests[vid]
        if channel_id(m.get("channel") or (m.get("recipe") or {}).get("channel")) == cid and aspect_of(m) in accepted:
            return vid, "channel_target", False
    for vid in order:
        if aspect_of(manifests[vid]) in accepted:
            return vid, "aspect_match", False
    return order[0], "top_reframe", True


def _duration(m):
    for k in ("duration_s", "length_s"):
        try:
            if m.get(k) is not None:
                return float(m[k])
        except (TypeError, ValueError):
            pass
    caps = [c for c in m.get("captions") or [] if isinstance(c, dict) and c.get("end") is not None]
    return float(max(c["end"] for c in caps)) if caps else None


def _src_mp4(d, m):
    p = d / f"{m['variant_id']}.mp4"
    if p.is_file():
        return p
    vp = m.get("video_path")
    if isinstance(vp, str) and vp.endswith(".mp4"):
        q = Path(vp)
        try:
            if q.is_file() and q.resolve().is_relative_to(d.parent.resolve()):
                return q
        except (OSError, ValueError):
            pass
    return None


def _export_one(d, run, export_id, out_dir, m, cid, selection, reframe, mode, client, render):
    s = CHANNELS[cid]
    vid = m["variant_id"]
    cdir = out_dir / cid
    cdir.mkdir(parents=True, exist_ok=True)
    dur = _duration(m)
    trim_to = float(s["max_s"]) if dur is not None and dur > s["max_s"] else None
    out_dur = min(dur, s["max_s"]) if dur is not None else None
    src_aspect = aspect_of(m)
    target, res = best_target(cid, src_aspect) if reframe else (src_aspect, None)
    video_name = f"{cid}_{vid}.mp4"
    v = prepare_video(_src_mp4(d, m), cdir / video_name, src_aspect=src_aspect, target_aspect=target,
                      resolution=res, reframe=reframe, trim_to=trim_to, render=render)
    caps = m.get("captions") or []
    (cdir / "captions.srt").write_text(to_srt(caps, out_dur))
    (cdir / "captions.vtt").write_text(to_vtt(caps, out_dur))
    copy = generate_copy(m, cid, mode=mode, client=client)
    (cdir / "copy.json").write_text(json.dumps(copy, indent=1))
    (cdir / "post.txt").write_text(post_text(copy))
    base = f"/runs/{run}/distribution/{export_id}/{cid}"
    style = (m.get("recipe") or {}).get("captions", "none")
    return dict(
        channel=cid, label=s["label"], variant_id=vid, selection=selection,
        aspect=src_aspect, target_aspect=target, needs_reframe=reframe, reframed=v["status"] == "reframed",
        duration_s=out_dur, max_s=s["max_s"], trimmed=trim_to is not None,
        video=dict(status=v["status"], url=f"{base}/{video_name}" if v["path"] else None, note=v["note"]),
        captions=dict(burned=style not in (None, "", "none"), style=style, cues=cue_count(caps, out_dur),
                      srt_url=f"{base}/captions.srt", vtt_url=f"{base}/captions.vtt"),
        copy=copy, post_url=f"{base}/post.txt",
        ready=v["path"] is not None,
    )


def export_run(run, variant_id=None, channels=None, mode="auto", render=True, *, root=None, llm_client=None):
    """Build bundles for each channel. Same object as POST /api/distribution/export."""
    if mode not in ("auto", "llm", "template"):
        raise DistributionError("mode must be one of auto, llm, template")
    cids = parse_channels(channels)
    d = run_dir(run, root)
    manifests = load_manifests(d)
    if variant_id is not None:
        if not isinstance(variant_id, str) or variant_id not in manifests:
            raise DistributionError(f"unknown variant_id: {variant_id}", 404)
    if mode == "llm" and llm_client is None and llm_persona.default_client() is None:
        raise DistributionError("mode llm requires XAI_API_KEY or GROK_API_KEY", 503)
    client = None
    if mode != "template":
        client = llm_client if llm_client is not None else llm_persona.default_client()
    order = ranking(d, manifests)
    export_id = "ex_" + time.strftime("%Y%m%d_%H%M%S") + "_" + secrets.token_hex(2)
    out_dir = d / "distribution" / export_id
    out_dir.mkdir(parents=True)
    rows = []
    for cid in cids:
        vid, selection, reframe = select_variant(manifests, order, cid, variant_id)
        rows.append(_export_one(d, run, export_id, out_dir, manifests[vid], cid, selection, reframe,
                                "template" if client is None else mode, client, render))
    sources = {r["copy"]["source"] for r in rows}
    doc = dict(
        run=run, export_id=export_id, created_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        requested_variant=variant_id, mode=mode,
        copy_source=sources.pop() if len(sources) == 1 else "mixed",
        disclaimer=DISCLAIMER,
        bundle_url=f"/api/distribution/bundle/{run}/{export_id}.zip",
        channels=rows,
    )
    (out_dir / "export.json").write_text(json.dumps(doc, indent=1))
    with _LOCK:
        (d / "distribution" / "latest.json").write_text(json.dumps({"export_id": export_id}))
    return doc


def _export_dir(d, export_id):
    if not isinstance(export_id, str) or not _ID_RE.fullmatch(export_id):
        raise DistributionError("export_id must look like ex_YYYYMMDD_HHMMSS_xxxx")
    p = d / "distribution" / export_id
    if not (p / "export.json").is_file():
        raise DistributionError(f"no such export: {export_id}", 404)
    return p


def load_export(run, export_id=None, *, root=None):
    """Latest (or a named) export. Same object as GET /api/distribution/export/{run}."""
    d = run_dir(run, root)
    if export_id is None:
        latest = d / "distribution" / "latest.json"
        try:
            export_id = json.loads(latest.read_text())["export_id"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            raise DistributionError(f"run {run} has no export yet; POST /api/distribution/export", 404)
    doc = json.loads((_export_dir(d, export_id) / "export.json").read_text())
    doc["exports"] = list_exports(d)
    return doc


def list_exports(d):
    base = d / "distribution"
    if not base.is_dir():
        return []
    return sorted((p.name for p in base.iterdir() if p.is_dir() and _ID_RE.fullmatch(p.name) and (p / "export.json").is_file()), reverse=True)


def build_zip(run, export_id, channel=None, *, root=None):
    """Write (once) and return the zip path for an export, optionally one channel."""
    d = run_dir(run, root)
    p = _export_dir(d, export_id)
    if channel is not None:
        cid = channel.strip().lower() if isinstance(channel, str) else None
        if cid not in CHANNELS or not (p / cid).is_dir():
            raise DistributionError(f"export {export_id} has no channel {channel}", 404)
        dirs = [p / cid]
        zpath = p / f"{run}_{export_id}_{cid}.zip"
    else:
        dirs = sorted(x for x in p.iterdir() if x.is_dir())
        zpath = p / f"{run}_{export_id}.zip"
    if zpath.is_file():
        return zpath
    tmp = zpath.with_suffix(".zip.tmp")
    prefix = f"{run}_{export_id}"
    with zipfile.ZipFile(tmp, "w") as z:
        z.write(p / "export.json", f"{prefix}/export.json", compress_type=zipfile.ZIP_DEFLATED)
        for cdir in dirs:
            for f in sorted(cdir.iterdir()):
                if f.is_file():
                    ct = zipfile.ZIP_STORED if f.suffix == ".mp4" else zipfile.ZIP_DEFLATED
                    z.write(f, f"{prefix}/{cdir.name}/{f.name}", compress_type=ct)
    shutil.move(tmp, zpath)
    return zpath
