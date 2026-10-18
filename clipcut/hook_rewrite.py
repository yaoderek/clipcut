"""Template hook rewrites, with an optional LLM pass.

Heuristic suggestions rephrase the variant's own hook line (or, if that is
missing, the opening captions or transcript). Four frames are used: question,
number-first, direct "you", and bold claim. They are not trained and they are
not validated on viewers.

predicted_delta rescores the rewritten line with persona_models.score_persona.
That number is a heuristic estimate. The LLM path, when a key is set and the
reply is usable JSON, replaces the line text only. The delta is still the
heuristic rescore, never a figure from the model.

See docs/PERSONA_API.md.
"""
from __future__ import annotations

import json
import re

from . import llm_persona
from .channels import CHANNEL_SPECS, hook_strength
from .persona_models import PERSONAS, score_persona

DISCLAIMER = (
    'These hook suggestions are template rewrites, or LLM-simulated lines when source is "llm" or "mixed". '
    "They are not trained and they are not validated on real viewers. "
    "predicted_delta is a heuristic rescore of the rewritten opening with the persona scorer "
    '(predicted_delta_label is "heuristic_estimate"), not a measured lift.'
)
HOOK_VERSION = {
    "heuristic": "hook-heuristic-v1",
    "llm": "llm-hook-v1",
    "mixed": "hook-mixed-v1",
}
DELTA_LABEL = "heuristic_estimate"
DELTA_NOTE = "predicted_delta is a heuristic rescore, not a measured lift."

# Persona order of the four frames. n takes a prefix of this tuple.
PERSONA_PATTERNS = {
    "fast_swiper": ("question", "you_address", "bold_claim", "number_first"),
    "casual_browser": ("you_address", "question", "number_first", "bold_claim"),
    "completionist": ("number_first", "bold_claim", "question", "you_address"),
    "sharer_engager": ("bold_claim", "question", "you_address", "number_first"),
}
PATTERN_LABEL = {
    "question": "question-form",
    "number_first": "number-first",
    "you_address": 'direct-"you"',
    "bold_claim": "bold-claim",
}

# Channel tone overrides the persona voice. Other channels keep the persona voice.
SNAP = {"tiktok", "ig_stories"}
INFORM = {"linkedin", "youtube"}
SHARE = {"x", "fb_feed"}
CHANNEL_LABEL = {name.lower(): name for name in CHANNEL_SPECS}
VOICE = {
    "fast_swiper": "a fast-swiper voice (short and immediate)",
    "casual_browser": "a casual-browser voice",
    "completionist": "a completionist voice (plain and informative)",
    "sharer_engager": "a sharer voice (claim-shaped)",
}

# A single clause up to this many words is kept whole. Longer openings are cut
# on a comma / "but" and the piece with the highest hand-set hook_strength is used.
MAX_WHOLE = 18
_LEADING = re.compile(r"^(?:look|well|so|um|uh|okay|ok|and|but)\b[,:\s]*", re.I)
_LEADING_NUMBER = re.compile(
    r"^(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten|first|top)\b", re.I
)
_LEADING_CLAIM = re.compile(r"^(?:never|always|most|stop|actually|wrong|the truth|nobody|everyone)\b", re.I)
_BAD_CLAIM = re.compile(r"\b(trained|validated|proven|a/b tested|measured lift)\b", re.I)
_NEGATION = re.compile(r"\b(not|never|no|n't)\b", re.I)
_STOP = {
    "a", "an", "the", "just", "really", "sort", "of", "to", "and", "or", "but", "so", "that",
    "this", "it", "was", "is", "i", "me", "my", "in", "on", "for", "with", "there", "theres",
    "be", "been", "can", "put", "you", "your", "about", "being", "into", "from", "than", "then",
    "when", "have", "has", "had", "are", "were", "at", "as", "if", "we", "she", "he", "they",
    "them", "her", "his", "our", "do", "did", "does", "got", "get", "look", "said", "its",
}


def rewrite_variant(manifest, personas, channels, n, client):
    """Return ({id, original_hook, suggestions}, origin). origin is heuristic, llm, or mixed."""
    original = original_hook(manifest)
    if not original:
        suggestions = [_empty_row(pid, ch, pat) for pid, ch, pat in _slots(personas, channels, n)]
        return _pack(manifest, original, suggestions), "heuristic"
    body, trimmed = opening_body(original)
    if not body:
        suggestions = [_empty_row(pid, ch, pat) for pid, ch, pat in _slots(personas, channels, n)]
        return _pack(manifest, original, suggestions), "heuristic"
    if client is not None:
        parsed = _call_llm(client, _prompt(manifest, original, personas, channels, n))
        merged = _merge_llm(parsed, manifest, personas, channels, n, body, trimmed) if parsed else None
        if merged is not None:
            suggestions, origin = merged
            return _pack(manifest, original, _rank(suggestions, personas, channels, n)), origin
    suggestions = []
    for persona in personas:
        for channel in channels or [None]:
            suggestions.extend(_heuristic_group(manifest, persona, channel, n, body, trimmed))
    return _pack(manifest, original, suggestions), "heuristic"


def original_hook(manifest):
    """Hook line, else the opening captions, else a transcript excerpt. Empty when none exist."""
    if not isinstance(manifest, dict):
        return ""
    for key in ("hook_text", "hook_phrase"):
        val = manifest.get(key)
        if isinstance(val, str) and val.strip():
            return _squash(val)[:280]
    caps = _caption_excerpt(manifest)
    if caps:
        return caps[:280]
    return _transcript_excerpt(manifest)[:280]


def opening_body(original):
    """Clause the templates rewrite. Second value is True when a longer line was cut down."""
    text = _squash(original).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    if not text:
        return "", False
    if len(text.split()) <= MAX_WHOLE:
        body = _clean_clause(text.rstrip(".!? "))
        return (body or text.rstrip(".!? ")), False
    parts = re.split(r"\s*[,;:—–]\s+|\s+\bbut\b\s+", text, flags=re.I)
    fits = []
    for i, part in enumerate(parts):
        cleaned = _clean_clause(part)
        n = len(cleaned.split())
        if 4 <= n <= MAX_WHOLE:
            fits.append((i, cleaned))
    if fits:
        _i, best = min(fits, key=lambda ip: (-hook_strength(ip[1])[0], ip[0]))
        return best, True
    clipped = " ".join(text.split()[:MAX_WHOLE])
    return _clean_clause(clipped) or clipped, True


def title_card(body, channel=None):
    """A few source words, uppercased. Not a new claim."""
    words = re.findall(r"[A-Za-z0-9']+", body or "")
    content = [w for w in words if w.lower().replace("'", "") not in _STOP]
    long = [w for w in content if len(w) >= 5]
    pool = long if len(long) >= 2 else content or words
    chosen = pool[: 2 if channel in SNAP else 3]
    while len(chosen) > 1 and len(" ".join(w.upper() for w in chosen)) > 32:
        shortest = min(range(len(chosen)), key=lambda i: (len(chosen[i]), -i))
        chosen.pop(shortest)
    return " ".join(w.upper() for w in chosen)


def predicted_delta(manifest, persona_id, hook):
    """Persona-score change after replacing hook_text. 0.0 when there is no line.

    Explicit hook_strength / hook_score on the copy are dropped so the scorer
    reads the new sentence. The caller's manifest is not modified.
    """
    if not hook or not isinstance(manifest, dict):
        return 0.0
    edited = dict(manifest)
    edited.pop("hook_strength", None)
    edited.pop("hook_score", None)
    edited["hook_text"] = hook
    old = score_persona(manifest, persona_id)["score"]
    new = score_persona(edited, persona_id)["score"]
    return round(new - old, 4)


def _slots(personas, channels, n):
    chs = channels or [None]
    for persona in personas:
        for channel in chs:
            for pattern in PERSONA_PATTERNS[persona][:n]:
                yield persona, channel, pattern


def _heuristic_group(manifest, persona, channel, n, body, trimmed):
    """Up to n frames. Repeats of the source line are used only to fill the count.

    Order is predicted_delta descending. Equal deltas keep the persona's frame order.
    """
    ranked = []
    tone = _tone(persona, channel)
    for index, pattern in enumerate(PERSONA_PATTERNS[persona]):
        hook, note = _FRAMES[pattern](body, tone)
        hook = _ensure_cap(hook)
        row = _finish_heuristic(manifest, persona, channel, hook, note, body, trimmed)
        ranked.append((_same_words(hook, body), -row["predicted_delta"], index, row))
    ranked.sort()
    return [row for _rep, _delta, _index, row in ranked[:n]]


def _same_words(a, b):
    return _norm(a) == _norm(b)


def _norm(text):
    return " ".join(re.findall(r"[a-z0-9']+", text.lower()))


def _next_fill(ranked, used_hooks):
    used = {_norm(hook) for hook in used_hooks}
    for row in ranked:
        if _norm(row["hook"]) not in used:
            return row
    return ranked[0]


def _rank(suggestions, personas, channels, n):
    """Within each persona (and channel), highest heuristic delta first. Ties keep frame order."""
    out, i = [], 0
    for _persona in personas:
        for _channel in channels or [None]:
            group = suggestions[i:i + n]
            i += n
            group.sort(key=lambda row: -row["predicted_delta"])
            out.extend(group)
    return out


def _pack(manifest, original, suggestions):
    vid = manifest.get("variant_id") or manifest.get("id") or "m01"
    return dict(id=str(vid), original_hook=original, suggestions=suggestions)


def _finish_heuristic(manifest, persona, channel, hook, note, body, trimmed):
    reason = _template_reason(note, channel, trimmed, persona)
    return _row(manifest, persona, channel, hook, title_card(body, channel), reason, "heuristic")


def _empty_row(persona, channel, pattern):
    reason = _with_delta_note(
        f"No hook line, caption text, or transcript on this variant, so the {PATTERN_LABEL[pattern]} "
        "template was not applied and no line was invented."
    )
    return _row_fields(persona, channel, "", None, reason, 0.0, "heuristic")


def _row(manifest, persona, channel, hook, card, reason, origin):
    return _row_fields(persona, channel, hook, card or None, _with_delta_note(reason), predicted_delta(manifest, persona, hook), origin)


def _row_fields(persona, channel, hook, card, reason, delta, origin):
    row = {"persona": persona}
    if channel:
        row["channel"] = channel
    row["hook"] = hook
    if card:
        row["title_card"] = card
    row["reason"] = reason
    row["predicted_delta"] = delta
    row["predicted_delta_label"] = DELTA_LABEL
    row["origin"] = origin
    return row


def _tone(persona, channel):
    if channel in SNAP:
        return "snap"
    if channel in INFORM:
        return "inform"
    if channel in SHARE:
        return "share"
    if persona == "fast_swiper":
        return "snap"
    if persona == "completionist":
        return "inform"
    if persona == "sharer_engager":
        return "share"
    return "plain"


def _template_reason(note, channel, trimmed, persona):
    bits = []
    if trimmed:
        bits.append("Built from the strongest short clause of the opening, chosen with the hand-set hook_strength rule.")
    bits.append(note.rstrip(".") + ".")
    if channel:
        bits.append(f"Shaped for {CHANNEL_LABEL.get(channel, channel)}.")
    else:
        bits.append(f"Written in {VOICE[persona]}.")
    bits.append("Template rewrite, not a trained or validated line.")
    return " ".join(bits)


def _with_delta_note(reason):
    text = re.sub(r"\s+", " ", reason).strip()
    if "heuristic rescore" in text.lower():
        return text
    if text and text[-1] not in ".!?":
        text += "."
    return (text + " " + DELTA_NOTE).strip()


def _question(body, tone):
    raw = body.rstrip()
    low = _lower_first(raw.rstrip("?"))
    if raw.endswith("?"):
        hook, kept = raw, True
    elif tone == "inform":
        hook, kept = f"Why does this matter: {low}?", False
    elif tone == "share":
        hook, kept = f"Would you share this: {low}?", False
    else:
        hook, kept = f"Did you know {low}?", False
    if tone == "snap" and not hook.lower().startswith("wait"):
        hook, kept = "Wait — " + hook[0].lower() + hook[1:], False
    note = "This opening is already a question, so the template keeps it" if kept else "Question form of this variant's hook line"
    return hook, note


def _number(body, tone):
    raw = body.rstrip(".!? ")
    if _LEADING_NUMBER.match(raw):
        return raw, "This opening already leads with a number, so the template keeps the source wording"
    low = _lower_first(raw)
    if tone == "inform":
        hook = f"One reason this matters: {low}"
    elif tone == "share":
        hook = f"One thing worth passing on: {low}"
    else:
        hook = f"One thing: {low}"
    return hook, 'Number-first frame in front of this variant\'s hook line. The word "one" is the template, not a count from the video'


def _you(body, tone):
    raw = body.rstrip(".!? ")
    low = _lower_first(raw)
    has_you = "addresses 'you'" in _flags(raw)
    if tone == "inform":
        return f"For you: {low}", 'Direct "you" address in front of this variant\'s hook line'
    if tone == "share":
        return f"You could pass this on: {low}", 'Direct "you" address in front of this variant\'s hook line'
    if has_you and re.match(r"^you\b", raw, re.I):
        return raw, 'The source line already opens on "you", so this frame keeps that wording'
    if has_you:
        return f"You — {low}", 'Direct "you" frame. The source line already says "you"'
    return f"This is for you: {low}", 'Direct "you" address wrapped around this variant\'s hook line'


def _claim(body, tone):
    raw = body.rstrip(".!? ")
    if _LEADING_CLAIM.match(raw):
        return raw, "This opening already states a bold claim, so the template keeps the source wording"
    low = _lower_first(raw)
    if tone == "inform":
        return f"Here's why: {low}", 'Bold-claim frame ("here\'s why") around this variant\'s hook line'
    if tone == "snap":
        return f"Stop — {low}", 'Bold-claim frame ("stop") around this variant\'s hook line'
    return f"The truth: {low}", 'Bold-claim frame ("the truth") around this variant\'s hook line'


_FRAMES = {
    "question": _question,
    "number_first": _number,
    "you_address": _you,
    "bold_claim": _claim,
}


def _merge_llm(parsed, manifest, personas, channels, n, body, trimmed):
    buckets = {}
    for persona in personas:
        for channel in channels or [None]:
            buckets[(persona, channel)] = []
    usable = 0
    for raw in parsed.get("suggestions") or []:
        row = _clean_llm_row(raw, personas, channels, body)
        if row is None:
            continue
        key = (row["persona"], row["channel"])
        if key in buckets and len(buckets[key]) < n:
            buckets[key].append(row)
            usable += 1
    if not usable:
        return None
    suggestions = []
    used_llm = used_heuristic = False
    for persona in personas:
        for channel in channels or [None]:
            ranked = _heuristic_group(manifest, persona, channel, 4, body, trimmed)
            bucket = buckets[(persona, channel)]
            used_hooks = []
            group = []
            for _index in range(n):
                if bucket:
                    used_llm = True
                    picked = bucket.pop(0)
                    card = picked["title_card"] or title_card(body, channel)
                    group.append(_row(manifest, persona, channel, picked["hook"], card, picked["reason"], "llm"))
                    used_hooks.append(picked["hook"])
                else:
                    used_heuristic = True
                    fill = _next_fill(ranked, used_hooks)
                    group.append(fill)
                    used_hooks.append(fill["hook"])
            suggestions.extend(group)
    if not used_llm:
        return None
    return suggestions, "llm" if not used_heuristic else "mixed"


def _clean_llm_row(raw, personas, channels, body):
    if not isinstance(raw, dict):
        return None
    persona = raw.get("persona")
    if persona not in personas:
        return None
    hook = raw.get("hook")
    if not isinstance(hook, str):
        return None
    hook = _squash(hook)[:240]
    if not hook:
        return None
    if channels:
        ch = raw.get("channel")
        if not isinstance(ch, str):
            return None
        channel = ch.strip().lower()
        if channel not in channels:
            return None
    else:
        channel = None
    reason = raw.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        reason = "LLM-simulated rewrite. Not a trained or validated line."
    else:
        reason = _squash(reason)[:300]
        if _BAD_CLAIM.search(reason) and not _NEGATION.search(reason):
            reason = "LLM-simulated rewrite. Not a trained or validated line."
    title = raw.get("title_card")
    if isinstance(title, str) and title.strip():
        title = _squash(title)[:42]
    else:
        title = None
    if title is None:
        title = title_card(body, channel)
    return dict(persona=persona, channel=channel, hook=hook, title_card=title, reason=reason)


def _prompt(manifest, original, personas, channels, n):
    persona_lines = []
    for pid in personas:
        persona = PERSONAS[pid]
        persona_lines.append(f"- {pid} ({persona['label']}): {persona['description']}")
    slot_lines = []
    for pid in personas:
        for channel in channels or [None]:
            for i in range(1, n + 1):
                if channel:
                    slot_lines.append(f"- persona={pid} channel={channel} suggestion={i}")
                else:
                    slot_lines.append(f"- persona={pid} suggestion={i} (omit the channel key)")
    brief = llm_persona.variant_brief(manifest if isinstance(manifest, dict) else {})
    return (
        "You rewrite the opening line of one short-form video. You are simulating a writing assistant. "
        "You cannot watch the video. This is not a measurement of real viewers.\n\n"
        "Rules:\n"
        "- Use only facts and wording already in the variant brief or the original hook. Do not invent numbers, names, or events.\n"
        "- Do not say a line is trained, validated, tested, or measured.\n"
        "- Each hook is one spoken sentence. title_card is a few words that could sit on screen.\n"
        "- Match the persona. When a slot names a channel, match that channel's tone too.\n"
        "- Return one object per slot, in the same order. Do not include predicted_delta; the server computes it.\n\n"
        "Personas:\n" + "\n".join(persona_lines) + "\n\n"
        "Slots:\n" + "\n".join(slot_lines) + "\n\n"
        "Variant brief:\n" + json.dumps(brief, ensure_ascii=False, default=str) + "\n\n"
        "Original hook:\n" + original + "\n\n"
        'Reply with ONLY a JSON object, no markdown:\n'
        '{"suggestions": [{"persona": "<id>", "channel": "<id, omit the key when the slot says so>", '
        '"hook": "<opening sentence>", "title_card": "<SHORT TITLE>", '
        '"reason": "<one short sentence>"}]}\n'
    )


def _call_llm(client, prompt):
    try:
        return llm_persona.parse_rewrites(client.complete(prompt))
    except Exception:
        return None


def _caption_excerpt(manifest):
    caps = manifest.get("captions")
    if not isinstance(caps, list):
        return ""
    parts = []
    for cap in caps:
        if isinstance(cap, dict):
            try:
                start = float(cap.get("start", cap.get("t0", 0)) or 0)
            except (TypeError, ValueError):
                start = 0.0
            if start > 8:
                break
            if cap.get("text"):
                parts.append(str(cap["text"]))
        elif isinstance(cap, str) and cap.strip():
            parts.append(cap)
        if len(parts) >= 8:
            break
    return _squash(" ".join(parts))


def _transcript_excerpt(manifest):
    transcript = manifest.get("transcript")
    if isinstance(transcript, str):
        return _squash(transcript)
    if not isinstance(transcript, list):
        return ""
    parts = []
    for seg in transcript:
        if isinstance(seg, str):
            parts.append(seg)
        elif isinstance(seg, dict) and seg.get("text"):
            parts.append(str(seg["text"]))
        if sum(len(p) for p in parts) > 400:
            break
    return _squash(" ".join(parts))


def _flags(text):
    _score, why = hook_strength(text or "")
    return set(why)


def _clean_clause(text):
    cleaned = text.strip(" \"'.;:")
    prev = None
    while cleaned and cleaned != prev:
        prev = cleaned
        cleaned = _LEADING.sub("", cleaned).strip(" ,;:")
    return cleaned


def _lower_first(text):
    text = text.strip()
    if not text or text.startswith("I ") or text.startswith("I'") or text.startswith("I’"):
        return text
    if text[0].isupper() and (len(text) == 1 or not text[1].isupper()):
        return text[0].lower() + text[1:]
    return text


def _ensure_cap(hook):
    hook = _squash(hook)
    if not hook:
        return hook
    if hook[0].islower():
        hook = hook[0].upper() + hook[1:]
    if hook[-1] not in ".!?":
        hook += "."
    return hook


def _squash(text):
    return re.sub(r"\s+", " ", str(text)).strip()
