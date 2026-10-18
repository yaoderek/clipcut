"""GET /api/distribution/dashboard/{run}: campaign view built on derek-personas' compare_run.

No store of its own: metrics come from clipcut.persona_api.compare_run (runs/<run>/metrics.json written by
store_metrics / POST /api/persona/metrics), joined with our publish.json and latest export copy.
Per-channel Thompson winners come only from compare's `by_channel` when present; this module never runs
clipcut.ab itself. Without `by_channel`, channels are grouped from compare's variant rows and the
per-channel winner is the top `actual`.
"""
from __future__ import annotations

from .bundle import load_export, run_dir
from .publish import publish_status
from .specs import CHANNELS, DistributionError, channel_id

NOTE_GROUPED = ("Per-channel winner is the highest `actual` among that channel's metric rows. "
                "Per-channel Thompson is not computed here; it appears when compare returns by_channel.")
NOTE_COMPARE = "Per-channel rows and Thompson winners come from GET /api/persona/compare/<run> by_channel."
UNASSIGNED = "unassigned"


def _cid(raw):
    return channel_id(raw) or (UNASSIGNED if raw in (None, "") else str(raw).strip().lower())


def _compare(run, root):
    from ..persona_api import PersonaScoreError, compare_run
    try:
        return compare_run(run, runs_root=root), None
    except PersonaScoreError as exc:
        if exc.status == 404:
            return None, str(exc)
        raise DistributionError(str(exc), exc.status) from exc


def _best(rows, key):
    rows = [r for r in rows if isinstance(r.get(key), (int, float))]
    if not rows:
        return None
    return min(rows, key=lambda r: (-r[key], str(r.get("id"))))


def _summary(rows):
    top = _best(rows, "actual")
    pred = _best(rows, "predicted")
    return dict(
        variants=[r.get("id") for r in rows],
        n=len(rows),
        winner_actual=None if top is None else dict(id=top.get("id"), actual=top.get("actual"), predicted=top.get("predicted"), views=top.get("views")),
        predicted_top=None if pred is None else pred.get("id"),
        agree=None if top is None or pred is None else top.get("id") == pred.get("id"),
    )


def _by_channel_compare(by_channel, rows_by_id):
    """Normalize compare's by_channel (object keyed by channel id, or a list of {channel, ...})."""
    items = by_channel.items() if isinstance(by_channel, dict) else (
        ((b.get("channel"), b) for b in by_channel if isinstance(b, dict)) if isinstance(by_channel, list) else [])
    out = {}
    for raw, block in items:
        if not isinstance(block, dict):
            continue
        cid = _cid(raw)
        vs = block.get("variants") or []
        rows = [v if isinstance(v, dict) else rows_by_id.get(v, {"id": v}) for v in vs]
        entry = _summary(rows)
        entry["thompson_winner"] = block.get("thompson_winner") or block.get("winner")
        entry["thompson_confident"] = block.get("thompson_confident")
        entry["spearman"] = block.get("spearman")
        entry["compare"] = block  # passthrough, so new fields reach the canvas without a change here
        out[cid] = entry
    return out


def _by_channel_grouped(rows):
    groups = {}
    for r in rows:
        groups.setdefault(_cid(r.get("channel")), []).append(r)
    out = {}
    for cid, rs in groups.items():
        entry = _summary(rs)
        entry["thompson_winner"] = None
        entry["thompson_confident"] = None
        entry["spearman"] = None
        out[cid] = entry
    return out


def _order(keys):
    known = [c for c in CHANNELS if c in keys]
    other = sorted(k for k in keys if k not in CHANNELS and k != UNASSIGNED)
    return known + other + ([UNASSIGNED] if UNASSIGNED in keys else [])


def dashboard(run, *, root=None):
    """Same object as GET /api/distribution/dashboard/{run}."""
    run_dir(run, root)
    cmp, missing = _compare(run, root)
    rows = []
    if cmp:
        for r in cmp.get("variants") or []:
            row = dict(r)
            row["channel"] = _cid(r.get("channel"))
            rows.append(row)
    rows_by_id = {r.get("id"): r for r in rows}
    if cmp and cmp.get("by_channel"):
        groups, source = _by_channel_compare(cmp["by_channel"], rows_by_id), "compare"
    else:
        groups, source = _by_channel_grouped(rows), "grouped"
    try:
        export = load_export(run, root=root)
    except DistributionError:
        export = None
    pub = publish_status(run, root=root)
    exp_rows = {r["channel"]: r for r in (export or {}).get("channels", [])}
    keys = set(groups) | set(exp_rows) | set(pub["entries"])
    channels = []
    for cid in _order(keys):
        g = groups.get(cid) or dict(variants=[], n=0, winner_actual=None, predicted_top=None, agree=None,
                                    thompson_winner=None, thompson_confident=None, spearman=None)
        e = exp_rows.get(cid)
        p = pub["entries"].get(cid)
        channels.append(dict(
            channel=cid, label=CHANNELS[cid]["label"] if cid in CHANNELS else cid,
            **g,
            export=None if e is None else dict(
                export_id=export["export_id"], variant_id=e["variant_id"], selection=e["selection"],
                title=e["copy"].get("title"), caption=e["copy"]["caption"], hashtags=e["copy"]["hashtags"],
                copy_source=e["copy"]["source"], video_url=e["video"]["url"], video_status=e["video"]["status"], ready=e["ready"]),
            publish=None if p is None else {k: p.get(k) for k in ("status", "state", "demo", "url", "video_id", "scheduled_for", "error", "at", "variant_id")},
        ))
    top = _best(rows, "actual")
    overall = None
    if cmp:
        overall = dict(
            thompson_winner=cmp.get("thompson_winner"), thompson_confident=cmp.get("thompson_confident"),
            top_actual=None if top is None else top.get("id"),
            spearman=cmp.get("spearman"), spearman_calibrated=cmp.get("spearman_calibrated"),
            n=cmp.get("n"), n_paired=cmp.get("n_paired"),
        )
    return dict(
        run=run,
        metrics_available=cmp is not None,
        metrics_note=None if cmp else f"{missing}. Upload metrics with POST /api/persona/metrics.",
        simulated=None if cmp is None else cmp.get("simulated"),
        simulated_reason=None if cmp is None else cmp.get("simulated_reason"),
        disclaimer=None if cmp is None else cmp.get("disclaimer"),
        outcome=None if cmp is None else cmp.get("outcome"),
        overall=overall,
        predicted_vs_actual=rows,
        by_channel_source=source,
        by_channel_note=NOTE_COMPARE if source == "compare" else NOTE_GROUPED,
        channels=channels,
        export_id=None if export is None else export["export_id"],
        publish_updated_at=pub.get("updated_at"),
        youtube_configured=pub["youtube_configured"],
        publish_disclaimer=pub["disclaimer"],
    )
