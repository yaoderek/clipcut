"""Per-platform title / caption / hashtags.

source "llm": Grok wrote the text (llm_persona.default_client(), XAI_API_KEY or GROK_API_KEY).
source "template": deterministic templates seeded from the variant's hook_text and the
heuristic hook rewrite (clipcut.hook_rewrite via persona_api.rewrite_hooks, title_card).
Either way the server enforces each platform's character limits. Nothing here invents
facts: templates only reuse words already on the variant, and the LLM is told not to.
"""
from __future__ import annotations

import json
import re
from collections import Counter

from .. import llm_persona
from ..channels import CHANNEL_SPECS
from .specs import CHANNELS

COPY_VERSION = {"llm": "dist-copy-llm-v1", "template": "dist-copy-template-v1"}
_STOP = set("""about above after again against because before being below between both could does doing during each
from further have having here into itself just more most other ought ourselves over same should some such than that
their theirs them themselves then there these they this those through under until very were what when where which while
who whom will with would your yours yourself yourselves youre thats dont cant wont really thing things think going
gonna people every never always little actually there's that's don't can't i'm you're""".split())
_LABEL = {k.lower(): k for k in CHANNEL_SPECS}


def _squash(t):
    return " ".join(str(t or "").split())


def fit(text, limit):
    """Trim to `limit` characters on a word boundary, ending with an ellipsis when cut."""
    text = _squash(text) if "\n" not in str(text) else str(text).strip()
    if limit is None or len(text) <= limit:
        return text
    cut = text[: max(0, limit - 1)]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:-—") + "…"


def _sentence(text):
    t = _squash(text)
    if not t:
        return ""
    if t.isupper():
        t = t.lower()
    t = re.sub(r"\bi\b", "I", t)
    t = re.sub(r"\bi'(m|ve|ll|d)\b", lambda m: "I'" + m.group(1), t)
    return t[0].upper() + t[1:]


def caption_text(manifest, max_words=40):
    caps = manifest.get("captions") or []
    words = []
    for c in caps:
        if isinstance(c, dict):
            words.extend(str(c.get("text") or "").split())
        if len(words) >= max_words:
            break
    return _sentence(" ".join(words[:max_words]))


def hashtags(manifest, cid, extra_text=""):
    s = CHANNELS[cid]
    text = " ".join([str(manifest.get("hook_text") or ""), caption_text(manifest, 200), extra_text])
    words = [w.strip("'").lower() for w in re.findall(r"[A-Za-z][A-Za-z']+", text)]
    words = [w for w in words if len(w) >= 5 and w.replace("'", "") not in _STOP and w not in _STOP and "'" not in w
             and not w.endswith(("ed", "ly", "ing"))]
    counts = Counter(words)
    ranked = sorted(counts, key=lambda w: (-counts[w], text.lower().find(w)))
    tags = list(s["fixed_tags"])
    for w in ranked:
        if len(tags) >= s["hashtags_max"]:
            break
        tag = "#" + w.capitalize()
        if tag.lower() not in {t.lower() for t in tags}:
            tags.append(tag)
    return tags[: max(s["hashtags_max"], len(s["fixed_tags"]))]


def _clean_tags(raw, cid):
    s = CHANNELS[cid]
    out = list(s["fixed_tags"])
    for t in raw or []:
        t = re.sub(r"[^A-Za-z0-9_]", "", str(t))
        if t and ("#" + t).lower() not in {x.lower() for x in out}:
            out.append("#" + t)
        if len(out) >= s["hashtags_max"]:
            break
    return out


def _with_tags(body, tags, limit, sep):
    tag_str = " ".join(tags)
    if not tag_str:
        return fit(body, limit)
    room = None if limit is None else limit - len(sep) - len(tag_str)
    if room is not None and room < 20:  # too tight: drop tags rather than the hook
        return fit(body, limit)
    return fit(body, room) + sep + tag_str


def seed_hook(manifest, cid):
    """(hook, title_card, seed_source) from the heuristic hook rewrite for this channel's audience."""
    from ..hook_rewrite import original_hook
    from ..persona_api import rewrite_hooks

    original = original_hook(manifest)
    audience = CHANNEL_SPECS[_LABEL[cid]]["audience"]
    persona = audience[0] if isinstance(audience, list) else max(audience, key=audience.get)
    try:
        out = rewrite_hooks(manifests=[manifest], personas=[persona], channels=[cid], n=1, mode="heuristic")
        sug = (out["variants"][0]["suggestions"] or [{}])[0]
        hook = sug.get("hook") or original
        return hook, sug.get("title_card") or "", "hook_rewrite"
    except Exception:  # noqa: BLE001 - a broken seed must not break an export
        return original, "", "hook_text"


def template_copy(manifest, cid):
    s = CHANNELS[cid]
    hook, card, seed_src = seed_hook(manifest, cid)
    original = _squash(manifest.get("hook_text") or manifest.get("hook_phrase") or hook)
    body = caption_text(manifest)
    tags = hashtags(manifest, cid)
    title = None
    if cid == "tiktok":
        caption = _with_tags(hook, tags, s["caption_max"], " ")
    elif cid == "shorts":
        title = fit(hook, s["title_max"])
        desc = original if original and original != hook else body
        caption = _with_tags(desc or hook, tags, s["caption_max"], "\n\n")
    elif cid == "reels":
        lines = [hook] + ([body] if body and body.lower() != hook.lower() else [])
        caption = _with_tags("\n\n".join(lines), tags, s["caption_max"], "\n\n")
    elif cid == "x":
        caption = _with_tags(hook, tags, s["caption_max"], " ")
    else:  # linkedin
        lines = [hook] + ([body] if body else []) + ["What's your take?"]
        caption = _with_tags("\n\n".join(lines), tags, s["caption_max"], "\n\n")
    return dict(title=title, caption=caption, hashtags=tags, hook=hook, title_card=card or None,
                source="template", seed=seed_src)


_PROMPT = """You write social post copy for a short video clip. Platform: {label}.
Rules for {label}: {notes}
Limits: title {title_rule}; caption at most {cap} characters INCLUDING the hashtags we append (leave room for about {tag_room} characters of hashtags).
Do not invent facts, names, numbers, or claims that are not in the clip brief. Do not say anything was tested or proven.
Clip brief:
{brief}
Opening line to build on: {hook}
Reply with only this JSON: {{"title": "...or null", "caption": "post text without hashtags", "hashtags": ["word", "word"]}}
Give at most {ntags} hashtags, single words, no '#'."""


def _parse(text):
    if not isinstance(text, str):
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("caption"), str) or not data["caption"].strip():
        return None
    return data


def llm_copy(manifest, cid, client, seed=None):
    s = CHANNELS[cid]
    seed = seed or template_copy(manifest, cid)
    tag_room = 2 + 12 * s["hashtags_max"]
    prompt = _PROMPT.format(
        label=s["label"], notes=s["notes"], cap=s["caption_max"], tag_room=tag_room, ntags=s["hashtags_max"],
        title_rule=f"at most {s['title_max']} characters" if s["title_max"] else "null (this platform has no title)",
        brief=llm_persona.variant_brief(manifest), hook=seed["hook"],
    )
    try:
        data = _parse(client.complete(prompt))
    except Exception:  # noqa: BLE001 - network / HTTP / timeout -> template
        data = None
    if data is None:
        return None
    tags = _clean_tags(data.get("hashtags") if isinstance(data.get("hashtags"), list) else [], cid) or seed["hashtags"]
    sep = " " if cid in ("tiktok", "x") else "\n\n"
    caption = _with_tags(str(data["caption"]).strip(), tags, s["caption_max"], sep)
    title = None
    if s["title_max"]:
        raw_title = data.get("title") if isinstance(data.get("title"), str) and data.get("title").strip() else seed["title"]
        title = fit(raw_title, s["title_max"])
    return dict(title=title, caption=caption, hashtags=tags, hook=seed["hook"], title_card=seed.get("title_card"),
                source="llm", seed=seed["seed"])


def generate_copy(manifest, cid, mode="auto", client=None):
    """Copy for one channel. mode: template | llm | auto. Returns a dict with `source` llm|template and limits."""
    s = CHANNELS[cid]
    seed = template_copy(manifest, cid)
    out = seed
    note = None
    if mode != "template":
        c = client if client is not None else llm_persona.default_client()
        if c is not None:
            got = llm_copy(manifest, cid, c, seed)
            if got is not None:
                out = got
            else:
                note = "LLM reply was unusable or the call failed; template copy used."
        elif mode == "llm":
            note = "No XAI_API_KEY / GROK_API_KEY; template copy used."
    out = dict(out)
    out["model_version"] = COPY_VERSION[out["source"]]
    out["limits"] = dict(title_max=s["title_max"], caption_max=s["caption_max"], hashtags_max=s["hashtags_max"])
    out["lengths"] = dict(title=len(out["title"]) if out["title"] else 0, caption=len(out["caption"]))
    if note:
        out["note"] = note
    return out


def post_text(copy):
    parts = []
    if copy.get("title"):
        parts.append("TITLE:\n" + copy["title"])
    parts.append(("DESCRIPTION:\n" if copy.get("title") else "CAPTION:\n") + copy["caption"])
    return "\n\n".join(parts) + "\n"
