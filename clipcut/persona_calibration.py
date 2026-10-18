"""Fit a small, explainable nudge of the hand-set persona weights from observed metrics.

The outcome is a hand-set blend of avg_watch_pct and share rate. Feature multipliers
and the persona mix are ridge-regressed toward the hand-set weights (delta 0),
then clipped. Under 8 variants the prior and the clip both tighten. Simulated
metrics are stored and labeled, and they are never used to fit or to report
source "calibrated".

Nothing here is a trained platform model. In-sample fit quality is reported as
in-sample. See docs/PERSONA_API.md.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import threading
import time
from pathlib import Path

import numpy as np

from .ab import analyze, variant_metrics
from .persona_api import PersonaScoreError, _RUN_RE, _load_run, _runs_root
from .persona_models import FEATURE_KEYS, PERSONA_ORDER, PERSONAS, feature_qualities, heuristic_variant

CALIBRATED_DISCLAIMER = (
    "Feature and persona weights were adjusted from uploaded per-variant metrics "
    "(watch percentage and shares) with a ridge prior toward the hand-set weights. "
    "The fit is in-sample and bounded. It is not a trained platform model, it does not "
    "predict future views, and this path is refused when the metrics file is simulated."
)
COMPARE_DISCLAIMER = (
    "predicted is the hand-set persona aggregate. predicted_calibrated is present only "
    "when a fit was made from metrics that are not simulated. actual is a hand-set blend "
    "of observed avg_watch_pct and share rate (0.6 watch, 0.4 share; share rate saturates "
    "at 5%). It is not a platform ranking. thompson_winner is the Beta posterior Thompson "
    "estimate from clipcut/ab.py."
)
COMPARE_DISCLAIMER_SIMULATED = (
    "These metrics are simulated, not platform results. predicted is the hand-set persona "
    "aggregate. Weights were not calibrated on this file. thompson_winner is the Beta "
    "posterior Thompson estimate from clipcut/ab.py on the simulated rows, not a real test winner."
)

# Same 0.6/0.4 split as clipcut/ab.py. 5% share rate saturates the share term.
# That ceiling is a hand-set scale, not a measured benchmark.
WATCH_WEIGHT = 0.6
SHARE_WEIGHT = 0.4
SHARE_RATE_FULL = 0.05

MIN_FIT = 2
MIN_UNSHRUNK = 8
_SCOPES = ("run", "global", "both")
_BODY_KEYS = {"run", "simulated", "scope", "metrics", "_meta"}
_VARIANT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
_LOCK = threading.RLock()
_MAX_CSV_BYTES = 1_000_000
_MAX_VARIANTS = 500
_MAX_VIEWS = 10**12

_GENERATORS = ("make_mock_metrics",)


def store_metrics(*, run=None, payload=None, csv_bytes=None, filename=None, simulated=None, scope=None, runs_root=None):
    """Merge per-variant metrics into runs/<run>/metrics.json and maybe fit weights."""
    with _LOCK:
        return _store(run=run, payload=payload, csv_bytes=csv_bytes, filename=filename, simulated=simulated, scope=scope, runs_root=runs_root)


def compare_run(run, *, runs_root=None):
    """Predicted vs actual, Spearman, and the Thompson winner from clipcut/ab.py."""
    with _LOCK:
        return _compare(run, runs_root=runs_root)


def load_for_score(scope, run, runs_root):
    """Calibrated weights for score_variants. Refuses simulated metrics."""
    with _LOCK:
        return _load_for_score(scope, run, runs_root)


def watch_share_outcome(metric):
    """Return watch_frac, share_rate, and the blended actual in [0, 1]."""
    views = float(metric["views"])
    watch = float(metric["avg_watch_pct"])
    if watch > 1.0:
        watch = watch / 100.0
    watch = 0.0 if watch < 0 else 1.0 if watch > 1 else watch
    share_rate = (float(metric["shares"]) / views) if views else 0.0
    share_term = share_rate / SHARE_RATE_FULL
    share_term = 0.0 if share_term < 0 else 1.0 if share_term > 1 else share_term
    actual = WATCH_WEIGHT * watch + SHARE_WEIGHT * share_term
    return watch, share_rate, actual


def _store(*, run, payload, csv_bytes, filename, simulated, scope, runs_root):
    if payload is not None and csv_bytes is not None:
        raise PersonaScoreError("send JSON or a CSV upload, not both")
    if csv_bytes is not None:
        parsed = _parse_csv(csv_bytes)
        incoming_meta = {}
        declared = simulated
        scope_val = scope
        run_val = run
        filename_val = filename or ""
    else:
        if not isinstance(payload, dict):
            raise PersonaScoreError("JSON object required")
        parsed, incoming_meta, declared, scope_val, run_val = _parse_json_body(payload, run, simulated, scope)
        filename_val = filename or str(incoming_meta.get("filename") or "")
    run_name = run_val
    if not isinstance(run_name, str) or not run_name:
        raise PersonaScoreError("run is required")
    directory = _run_dir(run_name, runs_root)
    scope_name = _scope(scope_val)
    rows = _validate_rows(parsed)
    detected, reasons = _detect_simulated(rows, incoming_meta, filename_val, declared)
    path = directory / "metrics.json"
    old_meta, old_rows, extra = _read_existing(path)
    if old_meta.get("simulated") is True:
        detected = True
        reasons.append("already marked simulated")
    for row in old_rows.values():
        if isinstance(row, dict) and row.get("simulated") is True:
            detected = True
            reasons.append("already marked simulated")
            break
    reason = "; ".join(_unique(reasons))
    merged = dict(old_rows)
    added, updated = [], []
    for vid, row in rows.items():
        stored = dict(row)
        if detected:
            stored["simulated"] = True
        else:
            stored.pop("simulated", None)
        if vid in merged:
            updated.append(vid)
        else:
            added.append(vid)
        merged[vid] = stored
    meta = {
        "simulated": detected,
        "simulated_reason": reason or None,
        "generator": incoming_meta.get("generator"),
        "source": "csv" if csv_bytes is not None else "json",
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "n": len(merged),
    }
    if not meta["generator"]:
        meta.pop("generator")
    if meta["simulated_reason"] is None:
        meta.pop("simulated_reason")
    _write_metrics(path, meta, extra, merged)
    calibration = _maybe_fit(directory, run_name, merged, meta, scope_name, runs_root)
    return {
        "run": run_name,
        "file": "metrics.json",
        "n": len(merged),
        "added": added,
        "updated": updated,
        "simulated": detected,
        "simulated_reason": reason or None,
        "calibration": calibration,
    }


def _compare(run, *, runs_root):
    directory = _run_dir(run, runs_root)
    stored = _load_metrics_file(directory)
    meta, rows, _extra = stored
    simulated, reason = _file_is_simulated(meta, rows)
    manifests = _manifests(directory, run, runs_root)
    by_id = {}
    for item in manifests:
        by_id.setdefault(str(item.get("variant_id") or ""), item)
    ab_rows = variant_metrics(rows)
    _check_ab_rows(ab_rows)
    report = analyze(ab_rows, seed=0)
    calibrated = None
    if not simulated:
        paired_n = sum(1 for vid in ab_rows if vid in by_id)
        if paired_n >= MIN_FIT:
            calibrated = _ensure_run_fit(directory, run, rows, runs_root)
    variants = []
    pred_pairs, cal_pairs = [], []
    for vid, metric in ab_rows.items():
        watch, share_rate, actual = watch_share_outcome(metric)
        manifest = by_id.get(vid)
        predicted = None
        predicted_cal = None
        if manifest is not None:
            predicted = heuristic_variant(manifest)["aggregate"]
            pred_pairs.append((predicted, round(actual, 4)))
            if calibrated is not None:
                predicted_cal = heuristic_variant(
                    manifest,
                    feature_weights=calibrated["feature_weights"],
                    persona_weights=calibrated["persona_weights"],
                )["aggregate"]
                cal_pairs.append((predicted_cal, round(actual, 4)))
        variants.append({
            "id": vid,
            "predicted": predicted,
            "predicted_calibrated": predicted_cal,
            "actual": round(actual, 4),
            "views": metric["views"],
            "avg_watch_pct": metric["avg_watch_pct"],
            "shares": metric["shares"],
            "likes": metric.get("likes"),
            "channel": metric.get("channel"),
            "share_rate": round(share_rate, 6),
            "watch_frac": round(watch, 6),
        })
    variants.sort(key=lambda row: row["id"])
    spearman = _round4(_spearman([p for p, _a in pred_pairs], [a for _p, a in pred_pairs])) if len(pred_pairs) >= 2 else None
    spearman_cal = _round4(_spearman([p for p, _a in cal_pairs], [a for _p, a in cal_pairs])) if len(cal_pairs) >= 2 else None
    out = {
        "run": run,
        "simulated": simulated,
        "simulated_reason": reason,
        "disclaimer": COMPARE_DISCLAIMER_SIMULATED if simulated else COMPARE_DISCLAIMER,
        "n": len(variants),
        "n_paired": len(pred_pairs),
        "spearman": spearman,
        "spearman_calibrated": spearman_cal,
        "thompson_winner": report["winner"],
        "thompson_confident": bool(report["confident"]),
        "ab_seed": 0,
        "ab_draws": 20000,
        "thompson": [_plain_row(row) for row in report["table"]],
        "variants": variants,
        "outcome": _outcome_note(),
    }
    if calibrated is not None:
        out["calibration"] = {
            "guard": calibrated["guard"],
            "n_samples": calibrated["n_samples"],
            "fit_quality": calibrated["fit_quality"],
            "feature_multipliers": calibrated["feature_multipliers"],
            "persona_weights": calibrated["persona_weights"],
            "hand_set_persona_weights": calibrated["hand_set_persona_weights"],
        }
    else:
        out["calibration"] = None
    return out


def _load_for_score(scope, run, runs_root):
    if scope == "run":
        if not isinstance(run, str) or not run:
            raise PersonaScoreError("calibration 'run' requires a run")
        directory = _run_dir(run, runs_root)
        _meta, rows, _extra = _load_metrics_file(directory)
        simulated, reason = _file_is_simulated(_meta, rows)
        if simulated:
            detail = f" ({reason})" if reason else ""
            raise PersonaScoreError(f"metrics are simulated{detail}; refusing to report source 'calibrated'", 400)
        fitted = _ensure_run_fit(directory, run, rows, runs_root)
        if fitted is None:
            raise PersonaScoreError(f"need at least {MIN_FIT} variants with manifests and views to calibrate", 400)
        return fitted
    if scope == "global":
        fitted = _ensure_global_fit(runs_root)
        if fitted is None:
            raise PersonaScoreError("no global calibration. Upload real metrics for at least 2 variants first.", 404)
        return fitted
    raise PersonaScoreError("calibration must be one of none, run, global")


def _maybe_fit(directory, run, rows, meta, scope_name, runs_root):
    if meta.get("simulated"):
        _remove(directory / "persona_calibration.json")
        # Drop this run from the pooled fit so a previous real upload cannot linger.
        _ensure_global_fit(runs_root, force=True)
        return {"fitted": False, "reason": "simulated metrics are not used to fit persona weights"}
    summaries = []
    if scope_name in ("run", "both"):
        fitted = _ensure_run_fit(directory, run, rows, runs_root, force=True)
        if fitted is None:
            summaries.append({"scope": "run", "fitted": False, "reason": f"need at least {MIN_FIT} variants that also have a vNN.json manifest"})
        else:
            summaries.append(_fit_summary(fitted))
    if scope_name in ("global", "both"):
        fitted = _ensure_global_fit(runs_root, force=True)
        if fitted is None:
            summaries.append({"scope": "global", "fitted": False, "reason": f"need at least {MIN_FIT} real variants with manifests across runs"})
        else:
            summaries.append(_fit_summary(fitted))
    if len(summaries) == 1:
        return summaries[0]
    return {"fitted": any(item.get("fitted") for item in summaries), "scopes": summaries}


def _fit_summary(fitted):
    return {
        "fitted": True,
        "scope": fitted["scope"],
        "guard": fitted["guard"],
        "n_samples": fitted["n_samples"],
        "fit_quality": fitted["fit_quality"],
        "feature_multipliers": fitted["feature_multipliers"],
        "persona_weights": fitted["persona_weights"],
        "file": fitted["file"],
    }


def _ensure_run_fit(directory, run, rows, runs_root, force=False):
    samples = _samples_for_run(directory, run, rows, runs_root)
    if len(samples) < MIN_FIT:
        _remove(directory / "persona_calibration.json")
        return None
    sha = _metrics_sha({s["variant_id"]: s["metric"] for s in samples})
    path = directory / "persona_calibration.json"
    if not force and path.is_file():
        existing = _read_json_object(path)
        if existing and existing.get("metrics_sha256") == sha and existing.get("scope") == "run" and not existing.get("simulated"):
            if _weights_ok(existing):
                return existing
    fitted = _fit(samples, scope="run", run=run, sha=sha)
    fitted["file"] = "persona_calibration.json"
    _write_json(path, fitted)
    return fitted


def _ensure_global_fit(runs_root, force=False):
    root = _runs_root(runs_root)
    grouped = []
    samples = []
    if root.is_dir():
        for directory in sorted(p for p in root.iterdir() if p.is_dir() and _RUN_RE.match(p.name)):
            metrics_path = directory / "metrics.json"
            if not metrics_path.is_file():
                continue
            try:
                meta, rows, _extra = _read_existing(metrics_path)
            except PersonaScoreError:
                continue
            simulated, _reason = _file_is_simulated(meta, rows)
            if simulated or not rows:
                continue
            run_samples = _samples_for_run(directory, directory.name, rows, runs_root)
            if not run_samples:
                continue
            sha = _metrics_sha({s["variant_id"]: s["metric"] for s in run_samples})
            grouped.append({"run": directory.name, "n": len(run_samples), "metrics_sha256": sha})
            samples.extend(run_samples)
    path = root / "_calibration" / "global.json"
    if len(samples) < MIN_FIT:
        _remove(path)
        return None
    fingerprint = hashlib.sha256("\n".join(f"{g['run']}:{g['metrics_sha256']}" for g in grouped).encode()).hexdigest()
    if not force and path.is_file():
        existing = _read_json_object(path)
        if existing and existing.get("metrics_sha256") == fingerprint and existing.get("scope") == "global" and _weights_ok(existing):
            return existing
    fitted = _fit(samples, scope="global", run=None, sha=fingerprint)
    fitted["file"] = "_calibration/global.json"
    fitted["runs"] = grouped
    _write_json(path, fitted)
    return fitted


def _samples_for_run(directory, run, rows, runs_root):
    manifests = _manifests(directory, run, runs_root)
    by_id = {}
    for item in manifests:
        by_id.setdefault(str(item.get("variant_id") or ""), item)
    samples = []
    for vid, metric in rows.items():
        manifest = by_id.get(vid)
        if manifest is None:
            continue
        _watch, _share, actual = watch_share_outcome(metric)
        samples.append({
            "run": run,
            "variant_id": vid,
            "manifest": manifest,
            "metric": metric,
            "actual": actual,
            "views": float(metric["views"]),
        })
    return samples


def _fit(samples, *, scope, run, sha):
    n = len(samples)
    guard, n_prior, feat_lim, pers_lim = _limits(n)
    hand_features = {pid: dict(PERSONAS[pid]["weights"]) for pid in PERSONA_ORDER}
    hand_mix = {pid: float(PERSONAS[pid]["weight"]) for pid in PERSONA_ORDER}
    qualities = [{pid: feature_qualities(s["manifest"], pid) for pid in PERSONA_ORDER} for s in samples]
    y = np.array([s["actual"] for s in samples], dtype=float)
    views = np.array([s["views"] for s in samples], dtype=float)
    y_hand = np.array([_unclamped_aggregate(q, hand_features, hand_mix) for q in qualities], dtype=float)
    feature_cols = np.column_stack([
        np.array([_feature_column(q, key, hand_features, hand_mix) for q in qualities], dtype=float)
        for key in FEATURE_KEYS
    ])
    delta, active_idx = _solve(feature_cols, y - y_hand, views, n_prior)
    multipliers = {}
    feature_weights = {}
    active_features = []
    for i, key in enumerate(FEATURE_KEYS):
        if i in active_idx:
            active_features.append(key)
            raw = 1.0 + float(delta[i])
        else:
            raw = 1.0
        mult = _clip(raw, 1.0 - feat_lim, 1.0 + feat_lim)
        multipliers[key] = round(mult, 6)
    for pid in PERSONA_ORDER:
        feature_weights[pid] = {key: round(hand_features[pid][key] * multipliers[key], 6) for key in FEATURE_KEYS}
    persona_scores = []
    for sample, quals in zip(samples, qualities):
        scored = heuristic_variant(sample["manifest"], feature_weights=feature_weights, persona_weights=hand_mix)
        persona_scores.append([next(p["score"] for p in scored["personas"] if p["id"] == pid) for pid in PERSONA_ORDER])
    persona_scores = np.array(persona_scores, dtype=float)
    y_feat = np.array([
        heuristic_variant(s["manifest"], feature_weights=feature_weights, persona_weights=hand_mix)["aggregate"]
        for s in samples
    ], dtype=float)
    mix_cols = _persona_columns(persona_scores, hand_mix)
    gamma, active_p = _solve(mix_cols, y - y_feat, views, n_prior)
    persona_mult = {}
    active_personas = []
    raw_mix = {}
    for i, pid in enumerate(PERSONA_ORDER):
        if i in active_p:
            active_personas.append(pid)
            raw = 1.0 + float(gamma[i])
        else:
            raw = 1.0
        persona_mult[pid] = round(_clip(raw, 1.0 - pers_lim, 1.0 + pers_lim), 6)
        raw_mix[pid] = hand_mix[pid] * persona_mult[pid]
    persona_weights = _normalize_mix(raw_mix)
    pred = [
        heuristic_variant(s["manifest"], feature_weights=feature_weights, persona_weights=persona_weights)["aggregate"]
        for s in samples
    ]
    hand_pred = [heuristic_variant(s["manifest"])["aggregate"] for s in samples]
    actual = [s["actual"] for s in samples]
    fit_quality = _fit_quality(pred, hand_pred, actual, guard, n_prior, active_features, active_personas)
    return {
        "schema": "persona-calibration-v1",
        "scope": scope,
        "run": run,
        "simulated": False,
        "n_samples": n,
        "guard": guard,
        "n_prior": round(float(n_prior), 4),
        "limits": {
            "feature_multiplier": [round(1.0 - feat_lim, 4), round(1.0 + feat_lim, 4)],
            "persona_multiplier": [round(1.0 - pers_lim, 4), round(1.0 + pers_lim, 4)],
        },
        "feature_multipliers": multipliers,
        "persona_multipliers": persona_mult,
        "feature_weights": feature_weights,
        "hand_set_feature_weights": hand_features,
        "persona_weights": persona_weights,
        "hand_set_persona_weights": {pid: round(hand_mix[pid], 4) for pid in PERSONA_ORDER},
        "bases": {pid: PERSONAS[pid]["base"] for pid in PERSONA_ORDER},
        "bases_note": "Persona bases stay at the hand-set values. Calibration adjusts feature weights and the persona mix only.",
        "outcome": _outcome_note(),
        "fit_quality": fit_quality,
        "metrics_sha256": sha,
        "disclaimer": CALIBRATED_DISCLAIMER,
    }


def _limits(n):
    if n < MIN_UNSHRUNK:
        n_prior = 48.0 * (MIN_UNSHRUNK / n) ** 2
        return "shrink", n_prior, 0.08, 0.05
    return "ok", 8.0, 0.50, 0.35


def _unclamped_aggregate(quals, feature_weights, mix):
    total = sum(mix[pid] for pid in PERSONA_ORDER) or 1.0
    acc = 0.0
    for pid in PERSONA_ORDER:
        raw = PERSONAS[pid]["base"] + sum(feature_weights[pid][key] * quals[pid][key] for key in FEATURE_KEYS)
        acc += mix[pid] * raw
    return acc / total


def _feature_column(quals, key, feature_weights, mix):
    total = sum(mix[pid] for pid in PERSONA_ORDER) or 1.0
    acc = sum(mix[pid] * feature_weights[pid][key] * quals[pid][key] for pid in PERSONA_ORDER)
    return acc / total


def _persona_columns(scores, mix):
    """Derivative of the mix at the hand-set weights. One column per persona."""
    mix_v = np.array([mix[pid] for pid in PERSONA_ORDER], dtype=float)
    mix_v = mix_v / mix_v.sum()
    # d(aggregate)/d(gamma_q) at 0 = a_q * (s_q - sum_p a_p s_p)
    blended = scores @ mix_v
    cols = []
    for i in range(len(PERSONA_ORDER)):
        cols.append(mix_v[i] * (scores[:, i] - blended))
    return np.column_stack(cols)


def _solve(X, target, views, n_prior):
    """View-weighted, column-centered ridge. Returns (beta, kept column indexes)."""
    X = np.asarray(X, dtype=float)
    target = np.asarray(target, dtype=float)
    views = np.asarray(views, dtype=float)
    p = X.shape[1]
    beta = np.zeros(p)
    if X.shape[0] == 0 or p == 0:
        return beta, []
    weight = views / views.sum()
    scale = np.sqrt(views)
    scale = scale / scale.mean()
    mean_x = (weight[:, None] * X).sum(axis=0)
    mean_t = float((weight * target).sum())
    centered = (X - mean_x) * scale[:, None]
    centered_t = (target - mean_t) * scale
    energy = (centered ** 2).sum(axis=0)
    keep = energy > 1e-10
    kept = [i for i, flag in enumerate(keep) if flag]
    if not kept:
        return beta, []
    n = float(X.shape[0])
    rms = np.sqrt(energy[keep] / n)
    rms = np.maximum(rms, 1e-12)
    Z = centered[:, keep] / rms
    gram = Z.T @ Z + float(n_prior) * np.eye(Z.shape[1])
    try:
        coef = np.linalg.solve(gram, Z.T @ centered_t)
    except np.linalg.LinAlgError:
        return beta, []
    beta[keep] = coef / rms
    return beta, kept


def _fit_quality(pred, hand_pred, actual, guard, n_prior, active_features, active_personas):
    cal = _stats(pred, actual)
    hand = _stats(hand_pred, actual)
    return {
        "r2": cal["r2"],
        "spearman": cal["spearman"],
        "rmse": cal["rmse"],
        "hand_set_r2": hand["r2"],
        "hand_set_spearman": hand["spearman"],
        "in_sample": True,
        "guard": guard,
        "n_prior": round(float(n_prior), 4),
        "weighted_by": "views",
        "active_features": active_features,
        "active_personas": active_personas,
        "mean_predicted": cal["mean_predicted"],
        "mean_actual": cal["mean_actual"],
    }


def _stats(pred, actual):
    pred_r = [round(float(p), 4) for p in pred]
    actual_r = [round(float(a), 4) for a in actual]
    resid = [(p - a) ** 2 for p, a in zip(pred_r, actual_r)]
    rmse = (sum(resid) / len(resid)) ** 0.5 if resid else None
    return {
        "r2": _round4(_r2(pred_r, actual_r)),
        "spearman": _round4(_spearman(pred_r, actual_r)),
        "rmse": _round4(rmse),
        "mean_predicted": _round4(sum(pred_r) / len(pred_r)) if pred_r else None,
        "mean_actual": _round4(sum(actual_r) / len(actual_r)) if actual_r else None,
    }


def _r2(pred, actual):
    if len(actual) < 2:
        return None
    mean = sum(actual) / len(actual)
    ss_tot = sum((a - mean) ** 2 for a in actual)
    if ss_tot < 1e-12:
        return None
    ss_res = sum((a - p) ** 2 for a, p in zip(actual, pred))
    return 1.0 - ss_res / ss_tot


def _spearman(left, right):
    if len(left) < 2 or len(left) != len(right):
        return None
    rx, ry = _ranks(left), _ranks(right)
    return _pearson(rx, ry)


def _ranks(values):
    order = sorted(range(len(values)), key=lambda i: (values[i], i))
    ranks = [0.0] * len(values)
    i = 0
    while i < len(values):
        j = i
        while j + 1 < len(values) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _pearson(left, right):
    x = [float(v) for v in left]
    y = [float(v) for v in right]
    mx = sum(x) / len(x)
    my = sum(y) / len(y)
    xc = [v - mx for v in x]
    yc = [v - my for v in y]
    denom = (sum(v * v for v in xc) * sum(v * v for v in yc)) ** 0.5
    if denom < 1e-12:
        return None
    return sum(a * b for a, b in zip(xc, yc)) / denom


def _normalize_mix(raw):
    total = sum(raw[pid] for pid in PERSONA_ORDER)
    if total <= 0:
        total = 1.0
        raw = {pid: 1.0 for pid in PERSONA_ORDER}
    rounded = {pid: round(raw[pid] / total, 6) for pid in PERSONA_ORDER}
    drift = round(1.0 - sum(rounded.values()), 6)
    top = max(PERSONA_ORDER, key=lambda pid: (rounded[pid], pid))
    rounded[top] = round(rounded[top] + drift, 6)
    return rounded


def _outcome_note():
    return {
        "watch_weight": WATCH_WEIGHT,
        "share_weight": SHARE_WEIGHT,
        "share_rate_full": SHARE_RATE_FULL,
        "note": "actual = 0.6*watch_frac + 0.4*min(1, share_rate/0.05). share_rate_full is a hand-set saturation point, not a measured benchmark. Likes are stored and not used.",
    }


def _parse_json_body(body, run, simulated, scope):
    if "metrics" in body:
        unknown = [key for key in body if key not in _BODY_KEYS]
        if unknown:
            raise PersonaScoreError("unknown fields: " + ", ".join(sorted(unknown)))
        metrics = body["metrics"]
        if not isinstance(metrics, dict) or not metrics:
            raise PersonaScoreError("metrics must be an object of variant_id -> {views, avg_watch_pct, shares}")
        meta = body.get("_meta") if isinstance(body.get("_meta"), dict) else {}
        declared = body.get("simulated", simulated)
        scope_val = body.get("scope", scope)
        run_val = body.get("run", run)
        return metrics, meta, declared, scope_val, run_val
    meta = body.get("_meta") if isinstance(body.get("_meta"), dict) else {}
    declared = body.get("simulated", simulated)
    scope_val = body.get("scope", scope)
    run_val = body.get("run", run)
    metrics = {key: value for key, value in body.items() if key not in _BODY_KEYS}
    if not metrics:
        raise PersonaScoreError("no variant metrics. Pass an ab-format object or {run, metrics}.")
    return metrics, meta, declared, scope_val, run_val


def _parse_csv(raw):
    if isinstance(raw, str):
        raw = raw.encode()
    if not isinstance(raw, (bytes, bytearray)):
        raise PersonaScoreError("CSV upload is missing")
    if len(raw) > _MAX_CSV_BYTES:
        raise PersonaScoreError("CSV upload is too large")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise PersonaScoreError("CSV must be UTF-8")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise PersonaScoreError("CSV header is required")
    fields = [(name or "").strip().lower() for name in reader.fieldnames]
    if len(fields) != len(set(fields)):
        raise PersonaScoreError("CSV header has duplicate columns")
    required = {"variant_id", "views", "avg_watch_pct", "shares"}
    missing = sorted(required - set(fields))
    if missing:
        raise PersonaScoreError("CSV is missing columns: " + ", ".join(missing))
    out = {}
    for number, row in enumerate(reader, start=2):
        if row is None:
            continue
        lowered = {(key or "").strip().lower(): (value.strip() if isinstance(value, str) else value) for key, value in row.items()}
        if not any(lowered.values()):
            continue
        vid = lowered.get("variant_id") or ""
        if vid in out:
            raise PersonaScoreError(f"CSV row {number}: duplicate variant_id {vid}")
        item = {
            "views": lowered.get("views"),
            "avg_watch_pct": lowered.get("avg_watch_pct"),
            "shares": lowered.get("shares"),
        }
        channel = lowered.get("channel")
        if channel:
            item["channel"] = channel
        likes = lowered.get("likes")
        if likes not in (None, ""):
            item["likes"] = likes
        try:
            checked = _validate_rows({vid: item})
        except PersonaScoreError as exc:
            raise PersonaScoreError(f"CSV row {number}: {exc}")
        out.update(checked)
        if len(out) > _MAX_VARIANTS:
            raise PersonaScoreError(f"at most {_MAX_VARIANTS} variants")
    if not out:
        raise PersonaScoreError("CSV has no variant rows")
    return out


def _validate_rows(metrics):
    if not isinstance(metrics, dict) or not metrics:
        raise PersonaScoreError("metrics must be a non-empty object")
    if len(metrics) > _MAX_VARIANTS:
        raise PersonaScoreError(f"at most {_MAX_VARIANTS} variants")
    out = {}
    for vid, row in metrics.items():
        if not isinstance(vid, str) or not _VARIANT_RE.match(vid) or vid.startswith("_") or vid in _BODY_KEYS:
            raise PersonaScoreError(f"invalid variant id: {vid!r}")
        if not isinstance(row, dict):
            raise PersonaScoreError(f"{vid}: metrics must be an object")
        views = _as_int(f"{vid}.views", row.get("views"), minimum=1, maximum=_MAX_VIEWS)
        shares = _as_int(f"{vid}.shares", row.get("shares"), minimum=0, maximum=views)
        watch = _as_float(f"{vid}.avg_watch_pct", row.get("avg_watch_pct"))
        if watch < 0 or watch > 100:
            raise PersonaScoreError(f"{vid}.avg_watch_pct must be between 0 and 100 (or a fraction from 0 to 1)")
        stored = {"views": views, "avg_watch_pct": watch, "shares": shares}
        if "channel" in row and row["channel"] not in (None, ""):
            channel = row["channel"]
            if not isinstance(channel, str) or len(channel) > 40 or any(ch in channel for ch in "\n\r"):
                raise PersonaScoreError(f"{vid}.channel must be a short string")
            stored["channel"] = channel.strip()
        if "likes" in row and row["likes"] not in (None, ""):
            stored["likes"] = _as_int(f"{vid}.likes", row["likes"], minimum=0, maximum=_MAX_VIEWS)
        if row.get("simulated") is True:
            stored["simulated"] = True
        out[vid] = stored
    return out


def _as_int(name, value, *, minimum, maximum):
    if isinstance(value, bool) or value is None:
        raise PersonaScoreError(f"{name} must be an integer")
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"-?\d+", text):
            value = int(text)
        else:
            raise PersonaScoreError(f"{name} must be an integer")
    if isinstance(value, float):
        if not value.is_integer():
            raise PersonaScoreError(f"{name} must be an integer")
        value = int(value)
    if not isinstance(value, int):
        raise PersonaScoreError(f"{name} must be an integer")
    if value < minimum or value > maximum:
        raise PersonaScoreError(f"{name} must be between {minimum} and {maximum}")
    return value


def _as_float(name, value):
    if isinstance(value, bool) or value is None:
        raise PersonaScoreError(f"{name} must be a number")
    if isinstance(value, str):
        text = value.strip()
        try:
            value = float(text)
        except ValueError:
            raise PersonaScoreError(f"{name} must be a number")
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise PersonaScoreError(f"{name} must be a number")
    if value != value or value in (float("inf"), float("-inf")):
        raise PersonaScoreError(f"{name} must be a finite number")
    return value


def _scope(value):
    if value is None or value == "":
        return "run"
    if not isinstance(value, str) or value not in _SCOPES:
        raise PersonaScoreError("scope must be one of run, global, both")
    return value


def _detect_simulated(rows, meta, filename, declared):
    reasons = []
    generator = str(meta.get("generator") or "")
    low = generator.lower()
    if any(token in low for token in _GENERATORS) or "mock" in low or "simul" in low:
        reasons.append(f"generator {generator}")
    name = Path(filename).name.lower() if filename else ""
    if name and ("mock" in name or "simul" in name):
        reasons.append(f"filename {Path(filename).name}")
    if _matches_known_mock(rows):
        reasons.append("matches make_mock_metrics.py output")
    if any(row.get("simulated") is True for row in rows.values()):
        reasons.append("variant simulated flag")
    flag = _parse_bool(declared, "simulated")
    if flag is True:
        reasons.append("declared")
    if meta.get("simulated") is True and "generator" not in " ".join(reasons):
        reasons.append("payload marked simulated")
    return bool(reasons), reasons


def _parse_bool(value, name):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        key = value.strip().lower()
        if key in {"true", "1", "yes"}:
            return True
        if key in {"false", "0", "no"}:
            return False
    raise PersonaScoreError(f"{name} must be a boolean")


def _matches_known_mock(rows):
    known = _known_mock_signature()
    if not known:
        return False
    return _signature(rows) == known


def _known_mock_signature():
    path = Path(__file__).resolve().parent.parent / "runs" / "demo" / "mock_metrics.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return _signature(data)


def _signature(metrics):
    rows = []
    for key in sorted(metrics):
        row = metrics[key]
        if not isinstance(row, dict) or "views" not in row or "avg_watch_pct" not in row or "shares" not in row:
            continue
        try:
            rows.append((str(key), int(row["views"]), round(float(row["avg_watch_pct"]), 6), int(row["shares"])))
        except (TypeError, ValueError):
            return None
    return tuple(rows)


def _file_is_simulated(meta, rows):
    meta = meta if isinstance(meta, dict) else {}
    _detected, reasons = _detect_simulated(rows, meta, None, None)
    if meta.get("simulated") is True and not reasons:
        reasons = [meta.get("simulated_reason") or "marked simulated"]
    reasons = _unique(reasons)
    return bool(reasons) or meta.get("simulated") is True, ("; ".join(reasons) or None)


def _read_existing(path):
    if not path.exists():
        return {}, {}, {}
    if not path.is_file():
        raise PersonaScoreError(f"{path.name} exists and is not a file; refusing to overwrite", 409)
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError:
        raise PersonaScoreError(f"{path.name} exists and is not valid JSON; refusing to overwrite", 409)
    if not isinstance(data, dict):
        raise PersonaScoreError(f"{path.name} exists and is not a JSON object; refusing to overwrite", 409)
    meta = data.get("_meta") if isinstance(data.get("_meta"), dict) else {}
    rows, extra = {}, {}
    for key, value in data.items():
        if key == "_meta":
            continue
        if isinstance(value, dict) and "views" in value and "avg_watch_pct" in value and "shares" in value:
            rows[key] = value
        else:
            extra[key] = value
    return meta, rows, extra


def _load_metrics_file(directory):
    path = directory / "metrics.json"
    if not path.is_file():
        raise PersonaScoreError(f"run {directory.name} has no metrics.json", 404)
    meta, rows, extra = _read_existing(path)
    if not rows:
        raise PersonaScoreError(f"run {directory.name} metrics.json has no variant rows", 404)
    return meta, rows, extra


def _write_metrics(path, meta, extra, rows):
    # _meta stays first so a person opening the file sees simulated before the numbers.
    ordered = {"_meta": meta}
    ordered.update(extra)
    for key in list(rows):
        ordered[key] = rows[key]
    _write_json(path, ordered)


def _write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2) + "\n")
    tmp.replace(path)


def _read_json_object(path):
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _weights_ok(doc):
    features = doc.get("feature_weights")
    mix = doc.get("persona_weights")
    if not isinstance(features, dict) or not isinstance(mix, dict):
        return False
    for pid in PERSONA_ORDER:
        row = features.get(pid)
        if not isinstance(row, dict):
            return False
        for key in FEATURE_KEYS:
            try:
                value = float(row[key])
            except (KeyError, TypeError, ValueError):
                return False
            if value != value:
                return False
        try:
            weight = float(mix[pid])
        except (KeyError, TypeError, ValueError):
            return False
        if weight <= 0:
            return False
    return True


def _manifests(directory, run, runs_root):
    if not any(directory.glob("v[0-9][0-9].json")):
        return []
    return _load_run(run, runs_root)


def _run_dir(run, runs_root):
    if not isinstance(run, str) or not _RUN_RE.match(run):
        raise PersonaScoreError("run must be a single path segment (letters, digits, ., _, -)")
    directory = _runs_root(runs_root) / run
    if not directory.is_dir():
        raise PersonaScoreError(f"no such run: {run}", 404)
    return directory


def _check_ab_rows(rows):
    for vid, row in rows.items():
        try:
            views = int(row["views"])
            shares = int(row["shares"])
            watch = float(row["avg_watch_pct"])
        except (TypeError, ValueError, KeyError):
            raise PersonaScoreError(f"{vid}: metrics are missing views, avg_watch_pct, or shares")
        if views < 1 or shares < 0 or shares > views or watch < 0 or watch > 100:
            raise PersonaScoreError(f"{vid}: stored metrics are outside the views/shares/watch bounds")


def _metrics_sha(rows):
    payload = {
        key: {"views": rows[key]["views"], "avg_watch_pct": rows[key]["avg_watch_pct"], "shares": rows[key]["shares"]}
        for key in sorted(rows)
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _plain_row(row):
    out = {}
    for key, value in row.items():
        if isinstance(value, float):
            out[key] = round(value, 6)
        else:
            out[key] = value
    return out


def _clip(value, lo, hi):
    return lo if value < lo else hi if value > hi else value


def _round4(value):
    if value is None:
        return None
    return round(float(value), 4)


def _unique(items):
    out = []
    for item in items:
        if item and item not in out:
            out.append(item)
    return out


def _remove(path):
    if path.is_file():
        path.unlink()
