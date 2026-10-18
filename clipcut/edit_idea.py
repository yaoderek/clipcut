"""Turn a natural-language edit idea into cutter_v2 recipe settings.

Uses Grok/xAI when XAI_API_KEY or GROK_API_KEY is set (same keys as
clipcut/llm_persona.py). Otherwise a deterministic keyword template.
Settings stay inside the enums cutter_v2 already renders: hook, length,
pacing, captions, hook_title, ending, aspect, channel.
"""
import json, os, re, urllib.request

LENGTHS = (15, 30, 45)
PACINGS = ("raw", "tight", "raw_punch", "tight_punch")
CAPTIONS = ("none", "sentence", "word_pop")
ENDINGS = ("loop", "fade")
ASPECTS = ("9:16", "1:1", "4:5", "16:9")
HOOKS = ("strongest", "second", "earliest")
CHANNELS = ("TikTok", "Reels", "Shorts", "X", "LinkedIn", "YouTube", "IG_Feed", "IG_Stories", "FB_Reels", "FB_Feed")

LLM_DISCLAIMER = (
    "LLM draft from Grok (xAI). These are suggested cutter settings, not measured results "
    "and not a trained model. Each branch still has to render through cutter v2."
)

def default_settings():
    return dict(hook="strongest", length=30, pacing="tight_punch", captions="sentence",
                hook_title=False, ending="loop", aspect="9:16", channel="TikTok")

def _coerce(raw):
    d = default_settings()
    if not isinstance(raw, dict):
        return d
    if raw.get("hook") in HOOKS:
        d["hook"] = raw["hook"]
    try:
        length = int(raw.get("length"))
    except (TypeError, ValueError):
        length = None
    if length in LENGTHS:
        d["length"] = length
    if raw.get("pacing") in PACINGS:
        d["pacing"] = raw["pacing"]
    if raw.get("captions") in CAPTIONS:
        d["captions"] = raw["captions"]
    if isinstance(raw.get("hook_title"), bool):
        d["hook_title"] = raw["hook_title"]
    if raw.get("ending") in ENDINGS:
        d["ending"] = raw["ending"]
    if raw.get("aspect") in ASPECTS:
        d["aspect"] = raw["aspect"]
    if raw.get("channel") in CHANNELS:
        d["channel"] = raw["channel"]
    return d

def split_ideas(text):
    t = (text or "").strip()
    t = re.sub(r"^(please\s+)?(try|let's|lets|can you)\b[:,]?\s*", "", t, flags=re.I)
    parts = re.split(r"\s*(?:,|;|\n|\band\b)\s*", t, flags=re.I)
    out = []
    for p in parts:
        p = p.strip(" .")
        p = re.sub(r"^(one|a|an)\s+", "", p, flags=re.I)
        if len(p) > 2:
            out.append(p)
    return out[:4]

def _name(chunk):
    stop = {"a", "an", "the", "and", "with", "one", "try", "cut", "for", "of", "to"}
    words = re.sub(r"[^a-zA-Z0-9' ]", "", chunk).split()
    keep = [w for w in words if w.lower() not in stop]
    label = " ".join(w.capitalize() for w in (keep or words)[:4])
    return label or "Approach"

def apply_keywords(chunk):
    s = default_settings()
    t = chunk.lower()
    if re.search(r"\b(fast|punch|punchy|snappy|jump|tight)\b", t):
        s.update(pacing="tight_punch", length=15, captions="word_pop", hook="strongest", channel="TikTok", aspect="9:16")
    if re.search(r"\b(calm|story|storytelling|slow|documentary|quiet)\b", t):
        s.update(pacing="raw_punch", length=45, captions="sentence", hook="earliest", channel="Reels")
    if re.search(r"\b(caption|captions|subtitle|subtitles|silent|muted|no sound|big text)\b", t):
        s.update(captions="word_pop", hook_title=True)
    if re.search(r"\b(no captions?|without captions?|uncaptioned)\b", t):
        s.update(captions="none", hook_title=False)
    if re.search(r"\b(linkedin|professional)\b", t):
        s.update(aspect="4:5", channel="LinkedIn", length=45, hook="earliest")
    if re.search(r"\b(youtube|horizontal|landscape)\b", t):
        s.update(aspect="16:9", channel="YouTube", length=45)
    if re.search(r"\b(square|twitter)\b", t) or re.search(r"\bx\b", t):
        s.update(aspect="1:1", channel="X")
    if re.search(r"\b(15s|15 s|15-second|fifteen)\b", t):
        s["length"] = 15
    if re.search(r"\b(45s|45 s|forty-five|long)\b", t):
        s["length"] = 45
    if re.search(r"\b(30s|30 s|thirty)\b", t):
        s["length"] = 30
    if re.search(r"\bfade\b", t):
        s["ending"] = "fade"
    if re.search(r"\b(second hook|next hook)\b", t):
        s["hook"] = "second"
    return s

def template_branches(text):
    chunks = split_ideas(text) or ["a 30 second cut"]
    out = []
    for i, chunk in enumerate(chunks):
        out.append(dict(key=f"b{i+1}", name=_name(chunk), summary=chunk, settings=apply_keywords(chunk)))
    return out

def _llm_request(text):
    key = os.environ.get("XAI_API_KEY") or os.environ.get("GROK_API_KEY")
    model = os.environ.get("XAI_MODEL", "grok-4")
    prompt = (
        "Convert this short-form video edit idea into 2 to 4 branches. "
        "Reply ONLY with JSON: {\"branches\":[{\"name\":str,\"summary\":str,\"settings\":{"
        "\"hook\":\"strongest\"|\"second\"|\"earliest\","
        "\"length\":15|30|45,"
        "\"pacing\":\"raw\"|\"tight\"|\"raw_punch\"|\"tight_punch\","
        "\"captions\":\"none\"|\"sentence\"|\"word_pop\","
        "\"hook_title\":bool,"
        "\"ending\":\"loop\"|\"fade\","
        "\"aspect\":\"9:16\"|\"1:1\"|\"4:5\"|\"16:9\","
        "\"channel\":\"TikTok\"|\"Reels\"|\"Shorts\"|\"X\"|\"LinkedIn\"|\"YouTube\"|\"IG_Feed\"|\"IG_Stories\"|\"FB_Reels\"|\"FB_Feed\""
        "}}]}. Idea:\n" + text
    )
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(
        "https://api.x.ai/v1/chat/completions", data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as resp:
        data = json.load(resp)
    content = data["choices"][0]["message"]["content"]
    return model, content

def branches_from_llm_text(content):
    blob = content[content.find("{"): content.rfind("}") + 1]
    data = json.loads(blob)
    raw = data.get("branches") if isinstance(data, dict) else None
    if not isinstance(raw, list) or not raw:
        raise ValueError("model returned no branches")
    out = []
    for i, item in enumerate(raw[:4]):
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or f"Approach {i+1}")[:48]
        summary = str(item.get("summary") or name)[:160]
        out.append(dict(key=f"b{i+1}", name=name, summary=summary, settings=_coerce(item.get("settings") or {})))
    if not out:
        raise ValueError("model returned no usable branches")
    return out

def interpret(text):
    text = (text or "").strip()
    if not text:
        raise ValueError("Describe the cuts you want to try.")
    reason = None
    if os.environ.get("XAI_API_KEY") or os.environ.get("GROK_API_KEY"):
        try:
            model, content = _llm_request(text)
            return dict(source="llm", model=model, disclaimer=LLM_DISCLAIMER, branches=branches_from_llm_text(content))
        except Exception as e:
            reason = f"Grok call failed ({e.__class__.__name__})"
    else:
        reason = "no XAI_API_KEY or GROK_API_KEY"
    return dict(
        source="template", model=None,
        disclaimer=("Template fallback (" + reason + "). Keyword rules chose cutter settings "
                    "(length, pacing, captions, crop). This is not an LLM rewrite and not a measured result."),
        branches=template_branches(text))
