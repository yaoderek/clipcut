# Persona scoring API

Contract for the audience-persona lane. The canvas / classic UI should call this and should not depend on `POST /api/score`.

`POST /api/score` stays as it is (external editor). It still returns the legacy `clipcut.retention.score` payload (`overall`, `personas` as a map of `p_swipe3s` / `watch_pct` / `p_share`, `channels` with display names such as `"TikTok"`).

Python entry point:

```python
from clipcut.persona_api import score_variants

score_variants(run="v2_niosh_multi", variant_ids=["v07"], mode="auto")
score_variants(manifests=[variant_dict], mode="heuristic")
```

`score_variants` returns the same object as the HTTP response. Invalid input raises `clipcut.persona_api.PersonaScoreError` (`.status` is the HTTP status, `str(err)` is the `error` message).

## What a score is

Scores are **rule-based persona heuristics**, or **LLM-simulated judgments** when a variant's `source` is `"llm"`.

They are not trained models. They are not real A/B results, watch-time predictions, or platform analytics. Feature weights are hand-set interpretations of short-form editing practice (hook in the first seconds, cut rate, captions, length versus a platform norm, topic keywords, loop ending). See `clipcut/persona_models.py` for the coefficients.

The four persona ids match the live heuristic scorer: `fast_swiper`, `casual_browser`, `completionist`, `sharer_engager`.

## `POST /api/persona/score`

`Content-Type: application/json`.

### Request

Provide **either** `run` **or** `manifests`, not both.

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `run` | string | one of `run` / `manifests` | Directory name under `runs/`, e.g. `"v2_niosh_multi"`. Only a single path segment is accepted (`v2_niosh_multi`, not `../x`). Variants are the `vNN.json` files in that directory (same files `rank_v2.py` scores). |
| `manifests` | array of objects | one of `run` / `manifests` | Variant manifests. A cutter-v2 `vNN.json` object, or a v1 variant object (`length_s`, `hook_text`, `caption_style`, …). If an object has a `variants` array and no `variant_id`, each child is scored (v1 `manifest.json`). |
| `variant_ids` | array of strings | no | Subset to score. Default: every variant. With `run`, ids must exist or the call is 404. Order of this array is the order of `variants` in the response. When omitted, response order is `v01`, `v02`, … |
| `mode` | string | no | `"heuristic"`, `"llm"`, or `"auto"`. Default `"auto"`. |

`mode`:

- `"heuristic"` — rule-based personas only. No network.
- `"llm"` — LLM role-plays each persona on the variant transcript and metadata. Requires `XAI_API_KEY` or `GROK_API_KEY`. If the key is missing, **503**. If a call fails or the reply is not usable JSON, that variant falls back to the heuristic and its `source` is `"heuristic"` (the pipeline does not raise).
- `"auto"` — `"llm"` when a key is set **and** the call returns usable JSON; otherwise `"heuristic"`. No key is not an error.

`model` in the legacy sense (`CLIPCUT_SCORER=model`, KuaiRand / MicroLens pickles) is **not** used here. `data/` and `models/` are not in git. `"model"` remains a legal `source` value for forward compatibility and is not emitted by this endpoint.

### Response `200`

```json
{
  "source": "heuristic",
  "model_version": "persona-heuristic-v1",
  "disclaimer": "These scores are rule-based persona heuristics, or LLM-simulated judgments when a variant's source is \"llm\". They are not trained models and they are not real A/B or platform results.",
  "winner": "v07",
  "variants": [
    {
      "id": "v07",
      "source": "heuristic",
      "aggregate": 0.71,
      "personas": [
        {
          "id": "fast_swiper",
          "label": "Fast swiper",
          "description": "Young swipe-feed viewer. Leaves unless the hook lands immediately and shots stay short.",
          "score": 0.64,
          "reasons": [
            "hook lands by 2.3s",
            "shots sit in a 1–3s window"
          ],
          "contributions": {
            "hook_timing": 0.08,
            "hook_strength": 0.02,
            "pacing": 0.06,
            "captions": 0.04,
            "length": 0.05,
            "topic_match": 0.0,
            "loop": 0.03,
            "silence": 0.02,
            "hook_title": 0.01
          }
        }
      ],
      "channels": [
        {
          "id": "tiktok",
          "label": "TikTok",
          "fit": 0.62,
          "reasons": [
            "aspect 9:16 is accepted",
            "32s is inside the 15–34s norm"
          ]
        }
      ]
    }
  ]
}
```

Numbers above are illustrative. Implementations return JSON numbers rounded to 4 decimal places.

| Field | Type | Meaning |
| --- | --- | --- |
| `source` | `"heuristic"` \| `"llm"` \| `"model"` \| `"mixed"` | How this response was produced. `"mixed"` when variants (or personas inside the LLM path) did not all use the same method. `"model"` is reserved and not emitted. |
| `model_version` | string | `"persona-heuristic-v1"` (all heuristic), `"llm-persona-v1"` (all LLM), or `"persona-mixed-v1"`. |
| `disclaimer` | string | Always the sentence in the example. Do not present scores as trained or measured. |
| `winner` | string | Variant `id` with the highest `aggregate`. Tie: lexicographically smallest id (`v01` before `v02`). |
| `variants` | array | One object per scored variant. |
| `variants[].id` | string | `variant_id`, else `id`, else `m01`, `m02`, … in input order. |
| `variants[].source` | same enum as top-level `source` | Method used for this variant. |
| `variants[].aggregate` | float in \[0, 1\] | Audience-weighted mean of the four persona scores. Weights are hand-set: fast_swiper 0.30, casual_browser 0.30, completionist 0.30, sharer_engager 0.10 (same mix as `retention.py`). |
| `variants[].personas` | array of 4 | Fixed order: `fast_swiper`, `casual_browser`, `completionist`, `sharer_engager`. |
| `personas[].score` | float in \[0, 1\] | That persona's heuristic or simulated judgment. |
| `personas[].reasons` | array of strings | Short human-readable notes, strongest signals first. At least one string. |
| `personas[].contributions` | object of feature → signed float | Per-feature points. Heuristic mode always includes every key listed below. LLM mode includes the same keys when the model returns them; missing keys are `0`. |
| `variants[].channels` | array | One entry per `clipcut.channels.CHANNEL_SPECS` key, best fit first. Tie on `fit`: smaller `id` first. |

### Heuristic score math

For each persona, each feature has a quality in \[-1, 1\] and a hand-set weight. Contributions are `weight * quality` (signed). A persona-specific base sits near the middle of the scale:

Contributions are rounded to 4 decimal places and then added to the base. That sum is rounded to 4 decimal places and clamped:

`score = clamp(round(base + sum(rounded contributions), 4), 0, 1)`

Bases (hand-set, not learned): fast_swiper 0.40, casual_browser 0.46, completionist 0.52, sharer_engager 0.48.

Feature keys:

| Key | What it responds to |
| --- | --- |
| `hook_timing` | `hook_end_s` versus that persona's deadline (fast swipers ~2.5s, completionists ~6s). Unknown timing contributes 0. |
| `hook_strength` | Opening line: question, number, claim, "you" (`channels.hook_strength`), or an explicit `hook_strength` / `hook_score`. |
| `pacing` | Share of shots inside the persona's preferred gap (fast ~1–3s, completionist ~3–8s). No cuts hurts fast swipers most. |
| `captions` | `word_pop` > `sentence` / `karaoke` > `minimal` > none. Long caption cues are treated as harder to read. Casual browsers weight this most (muted autoplay). |
| `length` | Duration versus the persona's sweet spot, blended with the variant channel's `ideal_s` in `CHANNEL_SPECS` when `channel` is set. |
| `topic_match` | Keyword overlap between hook + caption text and that persona's interest list. No text contributes 0 (unknown, not a penalty). |
| `loop` | `loop_end` or `recipe.ending == "loop"`. |
| `silence` | `silence_removed_s`. Missing key contributes 0. |
| `hook_title` | `recipe.hook_title`. |

Persona sketches (ids stable, copy is enrichable):

| id | label | Who the rule is standing in for |
| --- | --- | --- |
| `fast_swiper` | Fast swiper | Younger swipe-feed viewer (TikTok / Stories). Hook and cut rate dominate. Sweet spot about 15–30s. |
| `casual_browser` | Casual browser | Lean-back Reels / Shorts viewer, often muted. Captions and a 20–45s length matter most. |
| `completionist` | Completionist | Will finish a longer informative clip (LinkedIn / YouTube). Punishes rush cuts and very short hooks less; punishes going past ~90s. |
| `sharer_engager` | Sharer | Forwards a claim-shaped, loopable clip (X / Facebook). Hook line and loop weigh more than raw cut rate. |

### Channel `id`s

`id` is the `CHANNEL_SPECS` key lowercased. `label` is that key, which is what the current UI already prints.

| `id` | `label` (`CHANNEL_SPECS` key) | Audience the fit uses |
| --- | --- | --- |
| `tiktok` | TikTok | fast_swiper |
| `reels` | Reels | casual_browser |
| `shorts` | Shorts | fast_swiper, casual_browser |
| `x` | X | sharer_engager |
| `linkedin` | LinkedIn | completionist |
| `youtube` | YouTube | completionist |
| `ig_feed` | IG_Feed | casual_browser |
| `ig_stories` | IG_Stories | fast_swiper |
| `fb_reels` | FB_Reels | casual_browser 0.6, completionist 0.4 |
| `fb_feed` | FB_Feed | completionist, sharer_engager |

```
fit = clamp(audience_score * format_mult * duration_mult, 0, 1)
```

- `audience_score` — persona scores mixed with that channel's `audience` weights (equal weights when `audience` is a list).
- `format_mult` — `1` when aspect is missing or listed on the channel, else `0.6`. Aspect is read from `aspect`, then `recipe.aspect`, then the nearest of `9:16`, `4:5`, `1:1`, `16:9` to `resolution` `[w, h]`. No ffprobe and no video decode.
- `duration_mult` — `1` inside `ideal_s` (or duration unknown), `0.85` otherwise but within `max_s`, `0.3` over `max_s`. Same buckets as `channels.fit_for_channel`.

`fit` is a heuristic channel match, not a predicted view count.

### Errors

Body is always `{"error": "<message>"}`. No `detail` wrapper.

| Status | When |
| --- | --- |
| 400 | Not a JSON object. Both `run` and `manifests` set, or neither. `mode` not in `heuristic` / `llm` / `auto`. `manifests` not a list of objects. `variant_ids` not a list of strings. `run` is not a single safe path segment. Empty variant list. |
| 404 | `run` directory does not exist, has no `vNN.json`, or a requested `variant_id` is missing. |
| 503 | `mode` is `"llm"` and neither `XAI_API_KEY` nor `GROK_API_KEY` is set. |

## LLM persona layer

`clipcut/llm_persona.py` is optional. With no key, `rate(...)` returns `None` and does not raise. Callers fall back to the heuristic.

When a key is set, the client `POST`s `https://api.x.ai/v1/chat/completions` (`XAI_MODEL`, default `grok-4`) and asks the model to role-play **one** persona. The prompt includes a text brief built from whichever of these exist (v1 and v2 manifests): duration, hook text, hook timing, cut count / cuts per minute, caption style, caption excerpt, loop, silence removed, channel, aspect. It does not assume v1-only keys (`length_s`, `jump_cuts`, …), which previously raised `KeyError` on cutter-v2 `vNN.json`.

The model is asked for only this JSON:

```json
{
  "score": 0.7,
  "reasons": ["short reason"],
  "contributions": {
    "hook_timing": 0.05,
    "hook_strength": 0.04,
    "pacing": 0.0,
    "captions": 0.03,
    "length": 0.02,
    "topic_match": 0.0,
    "loop": 0.01,
    "silence": 0.0,
    "hook_title": 0.0
  }
}
```

Unparseable replies, HTTP errors, and timeouts become a heuristic fallback for that variant. Nothing in this layer is a measurement of real viewers.

`cli.py score` still calls `llm_persona.rate` for its v1 path. A missing key skips the check, same as before. A bad reply prints nothing useful and does not abort the CLI.

## Later calibration (not implemented)

Real outcomes can be scored with the existing tool, without changing this contract:

```bash
python cli.py ab metrics.json
```

`metrics.json` shape (see README): `{"v01": {"views": 1200, "avg_watch_pct": 54.3, "shares": 31}}`. `clipcut/ab.py` fits Beta posteriors and a Thompson next-round split. Those observed watch and share rates could later rescale the hand-set persona weights or feature weights. That fit is **not** done. Until it is, `model_version` stays `persona-heuristic-v1` / `llm-persona-v1` and the disclaimer stands.

Simulated A/B inside the app (`retention.simulate_ab`, `/api/channels/{run}`) is a different, mocked path. Do not treat it as calibration data.
