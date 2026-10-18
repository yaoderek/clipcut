"""Scoring entry point for the canvas UI. See docs/PERSONA_API.md.

score_variants() is deterministic and offline in heuristic mode. mode \"auto\"
uses an LLM-simulated judgment only when a key is configured and the call
returns usable JSON; otherwise it keeps the rule-based personas. Neither path
is a trained model or a real A/B result.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import llm_persona
from .persona_models import FEATURE_KEYS, PERSONA_ORDER, PERSONAS, channel_fits, heuristic_variant, score_persona

DISCLAIMER = (
    'These scores are rule-based persona heuristics, or LLM-simulated judgments when a variant\'s source is "llm". '
    "They are not trained models and they are not real A/B or platform results."
)
MODES = ("heuristic", "llm", "auto")
_RUN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")


class PersonaScoreError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def score_variants(run=None, variant_ids=None, manifests=None, mode="auto", *, runs_root=None, llm_client=None):
    """Score variant manifests. Same object as POST /api/persona/score.

    Pass either run (a directory name under runs/) or manifests (a list of
    variant dicts). llm_client is an optional complete(prompt) object for tests;
    it is not part of the JSON contract.
    """
    if not isinstance(mode, str) or mode not in MODES:
        raise PersonaScoreError("mode must be one of heuristic, llm, auto")
    if variant_ids is not None:
        if not isinstance(variant_ids, list) or not all(isinstance(v, str) and v for v in variant_ids):
            raise PersonaScoreError("variant_ids must be a list of strings")
        if not variant_ids:
            raise PersonaScoreError("variant_ids is empty")
    has_run, has_manifests = run is not None, manifests is not None
    if has_run == has_manifests:
        raise PersonaScoreError("provide exactly one of run or manifests")
    items = _load_run(run, runs_root) if has_run else _expand_manifests(manifests)
    items = _assign_ids(items)
    if variant_ids is not None:
        items = _select(items, variant_ids)
    if not items:
        raise PersonaScoreError("no variants to score")
    use_llm = _use_llm(mode, llm_client)
    client = llm_client if use_llm else None
    variants = [_score_one(m, client) for m in items]
    return _envelope(variants)


def _use_llm(mode, llm_client):
    if mode == "heuristic":
        return False
    if llm_client is not None or llm_persona.available():
        return True
    if mode == "llm":
        raise PersonaScoreError("LLM persona scoring requires XAI_API_KEY or GROK_API_KEY", 503)
    return False


def _runs_root(runs_root):
    if runs_root is not None:
        return Path(runs_root)
    return Path(__file__).resolve().parent.parent / "runs"


def _load_run(run, runs_root):
    if not isinstance(run, str) or not _RUN_RE.match(run):
        raise PersonaScoreError("run must be a single path segment (letters, digits, ., _, -)")
    directory = _runs_root(runs_root) / run
    if not directory.is_dir():
        raise PersonaScoreError(f"no such run: {run}", 404)
    files = sorted(directory.glob("v[0-9][0-9].json"))
    if not files:
        raise PersonaScoreError(f"run {run} has no vNN.json variants", 404)
    out = []
    for path in files:
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            raise PersonaScoreError(f"{path.name} is not valid JSON")
        if not isinstance(data, dict):
            raise PersonaScoreError(f"{path.name} is not a JSON object")
        data.setdefault("variant_id", path.stem)
        out.append(data)
    return out


def _expand_manifests(manifests):
    if not isinstance(manifests, list) or not manifests:
        raise PersonaScoreError("manifests must be a non-empty list of objects")
    out = []
    for item in manifests:
        if not isinstance(item, dict):
            raise PersonaScoreError("manifests must be a list of objects")
        children = item.get("variants")
        if isinstance(children, list) and not item.get("variant_id") and not item.get("id"):
            if not children:
                raise PersonaScoreError("manifests did not contain any variants")
            for child in children:
                if not isinstance(child, dict):
                    raise PersonaScoreError("manifests must be a list of objects")
                out.append(child)
        else:
            out.append(item)
    return out


def _assign_ids(items):
    out = []
    for i, item in enumerate(items):
        copied = dict(item)
        vid = copied.get("variant_id") or copied.get("id") or f"m{i + 1:02d}"
        copied["variant_id"] = str(vid)
        out.append(copied)
    return out


def _select(items, variant_ids):
    by_id = {}
    for item in items:
        by_id.setdefault(item["variant_id"], item)
    missing = [vid for vid in variant_ids if vid not in by_id]
    if missing:
        raise PersonaScoreError("unknown variant_ids: " + ", ".join(missing), 404)
    return [by_id[vid] for vid in variant_ids]


def _score_one(manifest, client):
    vid = manifest["variant_id"]
    if client is None:
        scored = heuristic_variant(manifest)
        scored["id"] = vid
        return scored
    personas = []
    saw_llm = saw_heuristic = False
    for pid in PERSONA_ORDER:
        persona = PERSONAS[pid]
        desc = (
            f"{persona['label']} (id: {pid}). {persona['description']} "
            f"Interests: {', '.join(persona['interests'])}. "
            f"Preferred length {persona['sweet'][0]:.0f}-{persona['sweet'][1]:.0f}s."
        )
        judgment = llm_persona.rate(desc, manifest, client=client)
        score = llm_persona.judgment_score(judgment) if judgment else None
        if score is None:
            saw_heuristic = True
            personas.append(score_persona(manifest, pid))
        else:
            saw_llm = True
            personas.append(_from_llm(pid, judgment, score))
    if saw_llm and saw_heuristic:
        source = "mixed"
    elif saw_llm:
        source = "llm"
    else:
        source = "heuristic"
    scores = {p["id"]: p["score"] for p in personas}
    weight = sum(PERSONAS[pid]["weight"] for pid in PERSONA_ORDER)
    aggregate = round(sum(PERSONAS[p["id"]]["weight"] * p["score"] for p in personas) / weight, 4)
    return dict(id=vid, source=source, aggregate=aggregate, personas=personas, channels=channel_fits(scores, manifest))


def _from_llm(persona_id, judgment, score):
    persona = PERSONAS[persona_id]
    reasons = judgment.get("reasons") if isinstance(judgment, dict) else None
    if isinstance(reasons, str):
        reasons = [reasons]
    if not isinstance(reasons, list):
        reasons = []
    reasons = [str(r) for r in reasons if str(r).strip()][:6]
    why = judgment.get("why") if isinstance(judgment, dict) else None
    if why and not reasons:
        reasons = [str(why)]
    if not reasons:
        reasons = ["LLM-simulated judgment (no reason returned)"]
    raw = judgment.get("contributions") if isinstance(judgment, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    contributions = {}
    for key in FEATURE_KEYS:
        try:
            contributions[key] = round(float(raw.get(key, 0.0)), 4)
        except (TypeError, ValueError):
            contributions[key] = 0.0
    return dict(
        id=persona_id,
        label=persona["label"],
        description=persona["description"],
        score=round(score, 4),
        reasons=reasons,
        contributions=contributions,
    )


def _envelope(variants):
    sources = {v["source"] for v in variants}
    if sources == {"heuristic"}:
        source, version = "heuristic", "persona-heuristic-v1"
    elif sources == {"llm"}:
        source, version = "llm", "llm-persona-v1"
    elif sources == {"model"}:
        source, version = "model", "persona-model-v1"
    else:
        source, version = "mixed", "persona-mixed-v1"
    winner = min(variants, key=lambda v: (-v["aggregate"], v["id"]))["id"]
    return dict(source=source, model_version=version, disclaimer=DISCLAIMER, winner=winner, variants=variants)
