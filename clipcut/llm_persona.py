"""Optional LLM-persona path (stub). Uses XAI_API_KEY or GROK_API_KEY if present; otherwise skipped."""
import os, json, urllib.request

PROMPT = """You are a simulated short-video viewer persona: {persona}.
Here is a candidate short-form video variant (features + opening caption):
{variant}
Reply ONLY with JSON: {{"swipe_away_in_3s_prob": float 0-1, "expected_watch_pct": float 0-100, "would_share": bool, "why": str}}"""

def available():
    return bool(os.environ.get("XAI_API_KEY") or os.environ.get("GROK_API_KEY"))

def rate(persona_desc, variant, model=os.environ.get("XAI_MODEL", "grok-4")):
    key = os.environ.get("XAI_API_KEY") or os.environ.get("GROK_API_KEY")
    if not key:
        return None
    v = {k: variant[k] for k in ("length_s", "hook_text", "jump_cuts", "cuts_per_min", "caption_style", "hook_zoom")}
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": PROMPT.format(persona=persona_desc, variant=json.dumps(v))}]}).encode()
    req = urllib.request.Request("https://api.x.ai/v1/chat/completions", body,
                                 {"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    txt = json.load(urllib.request.urlopen(req, timeout=60))["choices"][0]["message"]["content"]
    try: return json.loads(txt[txt.find("{"): txt.rfind("}") + 1])
    except Exception: return {"raw": txt}
