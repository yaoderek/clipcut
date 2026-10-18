# Persona scoring API

Contract for the audience-persona lane. The canvas / classic UI should call this and should not depend on `POST /api/score`.

Opening-line suggestions are a separate call, [`POST /api/persona/hooks`](#post-apipersonahooks).

Observed per-variant metrics are [`POST /api/persona/metrics`](#post-apipersonametrics). Predicted versus actual, Spearman, and the Thompson winner are [`GET /api/persona/compare/<run>`](#get-apipersonacomparerun).

`POST /api/score` stays as it is (external editor). It still returns the legacy `clipcut.retention.score` payload (`overall`, `personas` as a map of `p_swipe3s` / `watch_pct` / `p_share`, `channels` with display names such as `"TikTok"`).

Python entry point:

```python
from clipcut.persona_api import compare_run, score_variants, store_metrics

score_variants(run="v2_niosh_multi", variant_ids=["v07"], mode="auto")
score_variants(manifests=[variant_dict], mode="heuristic")
score_variants(run="v2_niosh_multi", mode="heuristic", calibration="run")
store_metrics(run="v2_niosh_multi", payload={"metrics": {"v01": {"views": 1200, "avg_watch_pct": 54.3, "shares": 31}}})
compare_run("v2_niosh_multi")
```

`score_variants` returns the same object as the HTTP response. Invalid input raises `clipcut.persona_api.PersonaScoreError` (`.status` is the HTTP status, `str(err)` is the `error` message).

## What a score is

Scores are **rule-based persona heuristics**, or **LLM-simulated judgments** when a variant's `source` is `"llm"`.

They are not trained models. They are not real A/B results, watch-time predictions, or platform analytics. Feature weights are hand-set interpretations of short-form editing practice (hook in the first seconds, cut rate, captions, length versus a platform norm, topic keywords, loop ending). See `clipcut/persona_models.py` for the coefficients.

`calibration: "none"` (the default) does not change that. `calibration: "run"` or `"global"` is the only path that adjusts feature weights and the persona mix, and only from uploaded metrics that are not marked simulated. Persona bases stay hand-set either way.

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
| `calibration` | string | no | `"none"` (default), `"run"`, or `"global"`. `"none"` is the response documented below. `"run"` and `"global"` rescore with weights fit from uploaded metrics. `null` means `"none"`. |

`mode`:

- `"heuristic"` — rule-based personas only. No network.
- `"llm"` — LLM role-plays each persona on the variant transcript and metadata. Requires `XAI_API_KEY` or `GROK_API_KEY`. If the key is missing, **503**. If a call fails or the reply is not usable JSON, that variant falls back to the heuristic and its `source` is `"heuristic"` (the pipeline does not raise).
- `"auto"` — `"llm"` when a key is set **and** the call returns usable JSON; otherwise `"heuristic"`. No key is not an error.

`calibration` other than `"none"` does not call the LLM, including when `mode` is `"auto"`. It rescores with the heuristic math and the fitted weights. `mode: "llm"` together with `calibration: "run"` or `"global"` is **400**. There is no network on that path.

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
| `source` | `"heuristic"` \| `"llm"` \| `"model"` \| `"mixed"` \| `"calibrated"` | How this response was produced. `"mixed"` when variants (or personas inside the LLM path) did not all use the same method. `"model"` is reserved and not emitted. `"calibrated"` only when `calibration` is `"run"` or `"global"`. |
| `model_version` | string | `"persona-heuristic-v1"` (all heuristic), `"llm-persona-v1"` (all LLM), `"persona-mixed-v1"`, or `"persona-calibrated-v1"` when `calibration` is `"run"` or `"global"`. |
| `disclaimer` | string | The sentence in the example when `calibration` is `"none"`. The calibrated disclaimer, below, when weights were fit from uploaded metrics. Do not present either score as a trained platform model. |
| `winner` | string | Variant `id` with the highest `aggregate`. Tie: lexicographically smallest id (`v01` before `v02`). |
| `variants` | array | One object per scored variant. |
| `variants[].id` | string | `variant_id`, else `id`, else `m01`, `m02`, … in input order. |
| `variants[].source` | same enum as top-level `source` | Method used for this variant. |
| `variants[].aggregate` | float in \[0, 1\] | Audience-weighted mean of the four persona scores. Default mix is hand-set: fast_swiper 0.30, casual_browser 0.30, completionist 0.30, sharer_engager 0.10 (same mix as `retention.py`). A calibrated score uses the fitted mix. |
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
| 400 | Not a JSON object. Both `run` and `manifests` set, or neither. `mode` not in `heuristic` / `llm` / `auto`. `manifests` not a list of objects. `variant_ids` not a list of strings. `run` is not a single safe path segment. Empty variant list. `calibration` not in `none` / `run` / `global`. `calibration` is `run` without a `run`. `calibration` is set and `mode` is `llm`. Metrics for that run are simulated. Fewer than 2 variants have both a manifest and metrics. |
| 404 | `run` directory does not exist, has no `vNN.json`, or a requested `variant_id` is missing. `calibration` is `run` and the run has no `metrics.json`. `calibration` is `global` and there are not enough real metrics anywhere under `runs/` to fit. |
| 503 | `mode` is `"llm"` and neither `XAI_API_KEY` nor `GROK_API_KEY` is set. `calibration` is not involved. |

### When `calibration` is `"run"` or `"global"`

Omitted or `"none"`: the fields above, and only those fields. `"run"` and `"global"` add three fields and change `source`, `model_version`, and `disclaimer`. Each variant's `source` is `"calibrated"`. Contributions use the fitted feature weights. Persona bases in `clipcut/persona_models.py` are not changed, and the in-memory hand-set weights are not written back.

```json
{
  "source": "calibrated",
  "model_version": "persona-calibrated-v1",
  "disclaimer": "Feature and persona weights were adjusted from uploaded per-variant metrics (watch percentage and shares) with a ridge prior toward the hand-set weights. The fit is in-sample and bounded. It is not a trained platform model, it does not predict future views, and this path is refused when the metrics file is simulated.",
  "winner": "v07",
  "variants": [],
  "calibration": "run",
  "n_samples": 12,
  "fit_quality": {
    "r2": -0.4,
    "spearman": 0.31,
    "rmse": 0.22,
    "hand_set_r2": -0.8,
    "hand_set_spearman": 0.2,
    "in_sample": true,
    "guard": "ok",
    "n_prior": 8.0,
    "weighted_by": "views",
    "active_features": ["hook_timing"],
    "active_personas": ["completionist"],
    "mean_predicted": 0.71,
    "mean_actual": 0.48
  }
}
```

Numbers above are illustrative. `variants` has the same shape as the heuristic response.

| Field | Meaning |
| --- | --- |
| `calibration` | `"run"` or `"global"`, echoing the request. |
| `n_samples` | Variants that were in the fit (a manifest and a metrics row). Not the number of variants in this response. Global is the pool across runs. |
| `fit_quality.r2` | In-sample R² of the calibrated aggregate against the watch/share blend. Often negative: the heuristic lives on its own 0–1 scale, and this fit does **not** add an intercept to hide that gap. `null` when actuals do not vary. |
| `fit_quality.spearman` | Spearman rank correlation of calibrated aggregate vs the blend. This is the ranking check. |
| `fit_quality.hand_set_r2` / `hand_set_spearman` | The same stats with the hand-set weights, so a reader can see whether the nudge beat the prior. |
| `fit_quality.rmse` | In-sample RMSE on that same pair of scales. |
| `fit_quality.in_sample` | Always `true`. There is no holdout. |
| `fit_quality.guard` | `"ok"` at 8 or more samples. `"shrink"` below 8: the prior is much stronger and the multiplier clip is tight. |
| `fit_quality.n_prior` | Ridge strength in pseudo-observations. 8 when `guard` is `"ok"`. `48 * (8 / n)²` when `guard` is `"shrink"`. |
| `fit_quality.weighted_by` | Always `"views"`. A variant with more views pulls the fit harder. |
| `fit_quality.active_features` | Feature columns that varied across the sample. Flat features stay at multiplier 1, because a flat column cannot be told apart from an intercept and this fit does not learn an intercept. |
| `fit_quality.active_personas` | Persona-mix columns that varied. |
| `fit_quality.mean_predicted` / `mean_actual` | Means of the calibrated aggregate and the blend. The gap is reported instead of being absorbed into a scale factor. |

`calibration: "run"` reads `runs/<run>/persona_calibration.json`, refitting when the metrics hash changed. `calibration: "global"` reads `runs/_calibration/global.json` (pooled real runs). Simulated metrics are **400**, not a calibrated score.

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

## `POST /api/persona/hooks`

Suggest stronger opening lines for a variant, one set per persona, with an optional on-screen title card and a short reason. This does not render video and it does not change the file on disk.

Python entry point:

```python
from clipcut.persona_api import rewrite_hooks

rewrite_hooks(run="v2_niosh_multi", variant_ids=["v07"], mode="heuristic")
rewrite_hooks(manifests=[variant_dict], personas=["sharer_engager"], channels=["x"], n=2, mode="auto")
```

`rewrite_hooks` returns the same object as the HTTP response. Invalid input raises `clipcut.persona_api.PersonaScoreError` (`.status` is the HTTP status, `str(err)` is the `error` message).

`Content-Type: application/json`.

### Request

Provide **either** `run` **or** `manifests`, not both. Loading rules match [`POST /api/persona/score`](#post-apipersonascore) (`runs/<run>/vNN.json`, v1 `variants` arrays, safe single path segment).

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `run` | string | one of `run` / `manifests` | Directory name under `runs/`. |
| `manifests` | array of objects | one of `run` / `manifests` | Same shapes as the score endpoint. |
| `variant_ids` | array of strings | no | Subset, in this order. Unknown id is 404. |
| `personas` | array of strings | no | Persona ids. Default: `fast_swiper`, `casual_browser`, `completionist`, `sharer_engager`, in that order. Unknown id, duplicates, or an empty list is 400. |
| `channels` | array of strings | no | Channel ids (`tiktok`, `reels`, `shorts`, `x`, `linkedin`, `youtube`, `ig_feed`, `ig_stories`, `fb_reels`, `fb_feed`). Display labels such as `"TikTok"` are accepted and returned as the id. Omit the field for persona-only lines. Unknown id, duplicates, or `[]` is 400. |
| `n` | integer | no | Suggestions per persona. When `channels` is set, this count is per persona **and** per channel. Default 3. Allowed 1–4. `null` means the default. |
| `mode` | string | no | `"heuristic"`, `"llm"`, or `"auto"`. Default `"auto"`. |

`mode` matches the score endpoint:

- `"heuristic"` — templates only. No network, even if `XAI_API_KEY` or `GROK_API_KEY` is set.
- `"llm"` — ask the model for the lines. Requires `XAI_API_KEY` or `GROK_API_KEY`. Missing key is **503**. A failed call or unusable JSON falls back to the templates for that variant (the pipeline does not raise).
- `"auto"` — `"llm"` when a key is set and the reply has usable suggestions; otherwise `"heuristic"`. No key is not an error.

### Response `200`

```json
{
  "source": "heuristic",
  "model_version": "hook-heuristic-v1",
  "disclaimer": "These hook suggestions are template rewrites, or LLM-simulated lines when source is \"llm\" or \"mixed\". They are not trained and they are not validated on real viewers. predicted_delta is a heuristic rescore of the rewritten opening with the persona scorer (predicted_delta_label is \"heuristic_estimate\"), not a measured lift.",
  "variants": [
    {
      "id": "v07",
      "original_hook": "There's just no price you can put on that.",
      "suggestions": [
        {
          "persona": "fast_swiper",
          "hook": "Wait — did you know there's just no price you can put on that?",
          "title_card": "NO PRICE",
          "reason": "Question form of this variant's hook line. Written in a fast-swiper voice (short and immediate). Template rewrite, not a trained or validated line. predicted_delta is a heuristic rescore, not a measured lift.",
          "predicted_delta": 0.0967,
          "predicted_delta_label": "heuristic_estimate",
          "origin": "heuristic"
        }
      ]
    }
  ]
}
```

The object above is the shape. A full heuristic response for `runs/v2_niosh_multi` variant `v07` is in the example at the end of this section. There is no `winner`. These lines are not a ranking of variants.

| Field | Type | Meaning |
| --- | --- | --- |
| `source` | `"heuristic"` \| `"llm"` \| `"mixed"` | `"llm"` only when every suggestion in the response came from the model. `"mixed"` when a reply filled some slots and templates filled the rest, or variants disagreed. |
| `model_version` | string | `"hook-heuristic-v1"`, `"llm-hook-v1"`, or `"hook-mixed-v1"`. |
| `disclaimer` | string | Always the sentence in the example. Do not present suggestions as trained or validated. |
| `variants[].id` | string | Same id rules as the score endpoint. |
| `variants[].original_hook` | string | `hook_text`, else `hook_phrase`, else the opening caption cues (start ≤ 8s), else a transcript excerpt. `""` when none of those exist. Truncated at 280 characters. |
| `suggestions[].persona` | string | One of the four ids. |
| `suggestions[].channel` | string | Present only when the request included `channels`. |
| `suggestions[].hook` | string | The suggested spoken open. `""` when there was no source text to rewrite. |
| `suggestions[].title_card` | string | Optional. A few words from the source line, uppercased, for an on-screen card. Omitted when there is no source text. |
| `suggestions[].reason` | string | Why this frame was used. Heuristic reasons say they are a template, not a trained or validated line. |
| `suggestions[].predicted_delta` | float | Change in that persona's score from `clipcut.persona_models.score_persona` after replacing `hook_text` with `hook`. Rounded to 4 decimal places. Can be negative. `0.0` when `hook` is empty. |
| `suggestions[].predicted_delta_label` | string | Always `"heuristic_estimate"`. This is not a measured lift, including when `origin` is `"llm"`. The model is not asked for a delta, and a delta in the model JSON is ignored. |
| `suggestions[].origin` | `"heuristic"` \| `"llm"` | Which writer produced this line. |

Order: personas in request order (or the default four), then channels in request order, then `predicted_delta` descending inside that group. Equal deltas keep the persona's frame preference.

### Heuristic templates

The heuristic does not invent events, names, or statistics. It rewrites words that are already on the variant.

Source line, in order: `hook_text`, `hook_phrase`, opening captions, transcript. A line longer than 18 words is cut on a comma, semicolon, dash, or `but`. The kept clause is the one with the highest `channels.hook_strength` (a hand-set rule). The reason says when that cut happened.

Four frames:

| Frame | What it does |
| --- | --- |
| Question | `Did you know …?`, or a voice-specific question. An opening that is already a question can be kept. |
| Number-first | `One thing: …` when the line does not already lead with a number. The word "one" is the template. Digits that are not in the source are not added. |
| Direct "you" | `This is for you: …` or `You — …`. A line that already opens on "you" is not wrapped as "You — you …". |
| Bold claim | `The truth: …`, `Stop — …`, or `Here's why: …`. |

Voice when `channels` is omitted:

| Persona | Voice | Question | Number-first | "you" | Claim |
| --- | --- | --- | --- | --- | --- |
| `fast_swiper` | short, immediate | `Wait — did you know …?` | `One thing: …` | `You — …` | `Stop — …` |
| `casual_browser` | plain | `Did you know …?` | `One thing: …` | `This is for you: …` | `The truth: …` |
| `completionist` | informative | `Why does this matter: …?` | `One reason this matters: …` | `For you: …` | `Here's why: …` |
| `sharer_engager` | claim-shaped | `Would you share this: …?` | `One thing worth passing on: …` | `You could pass this on: …` | `The truth: …` |

A channel overrides that voice. `tiktok` and `ig_stories` use the fast-swiper frames. `linkedin` and `youtube` use the completionist frames. `x` and `fb_feed` use the sharer frames. `reels`, `shorts`, `ig_feed`, and `fb_reels` keep the persona voice. The reason names the channel when one was requested (`Shaped for TikTok.`).

Each persona scores all four frames. `n` is the count kept, highest `predicted_delta` first. A frame that only repeats the source words is used only when fewer than `n` frames actually change the line. That is why the default three lines for one persona are not always question, then "you", then claim: a frame the heuristic rescore does not like falls out of the top `n`.

`predicted_delta` is `round(new_score - old_score, 4)` from `score_persona` on that persona. The copy used for `new_score` changes `hook_text` only. `hook_strength` and `hook_score`, if present, are removed on the copy so the new sentence is what gets read. The caller's manifest is not modified. Title-card text is not fed back into the score. A template word can move `hook_strength` and also `topic_match` when it hits that persona's interest list (`Wait` for a fast swiper, `share` for a sharer). That is still the heuristic rescore, not a measured lift.

No source text: `n` suggestions per requested slot, `hook` `""`, no `title_card`, `predicted_delta` `0.0`, and a reason that says no line was invented. The LLM is not called for that variant.

### LLM hook layer

Same client as the [LLM persona layer](#llm-persona-layer): `XAI_API_KEY` or `GROK_API_KEY`, `POST https://api.x.ai/v1/chat/completions`, model `XAI_MODEL` or `grok-4`. The prompt includes `variant_brief` (v1 and v2 manifests) plus the original hook, and it tells the model not to invent facts and not to call a line trained or validated.

The model is asked for:

```json
{"suggestions": [{"persona": "fast_swiper", "channel": "tiktok", "hook": "...", "title_card": "NO PRICE", "reason": "..."}]}
```

`channel` is omitted when the request did not ask for channels. The server rescores whatever hook text comes back. A reason that claims the line is trained or validated, without saying it is not, is replaced with `LLM-simulated rewrite. Not a trained or validated line.` Missing title cards are filled from source words with the same title-card helper as the templates.

If the reply is missing, unusable, or has no row that matches a requested persona (and channel, when channels were requested), that variant is entirely templates. If some slots match and others do not, the gaps are templates and `source` is `"mixed"`. Nothing in this layer is a measurement of real viewers.

### Errors

Body is always `{"error": "<message>"}`. No `detail` wrapper.

| Status | When |
| --- | --- |
| 400 | Not a JSON object. Both `run` and `manifests` set, or neither. `mode` not in `heuristic` / `llm` / `auto`. `manifests` not a list of objects. `variant_ids` not a list of strings. `run` is not a single safe path segment. Empty variant list. `personas` or `channels` not a list of known ids, empty, or containing duplicates. `n` not an integer from 1 to 4. |
| 404 | `run` directory does not exist, has no `vNN.json`, or a requested `variant_id` is missing. |
| 503 | `mode` is `"llm"` and neither `XAI_API_KEY` nor `GROK_API_KEY` is set. |

### Example: `v2_niosh_multi` / `v07`, heuristic

`rewrite_hooks(run="v2_niosh_multi", variant_ids=["v07"], mode="heuristic")` returns this object. The same call without `variant_ids` returns `v01` through `v16` in that order, each with the same envelope. Re-running it is byte-for-byte the same.

```json
{
  "source": "heuristic",
  "model_version": "hook-heuristic-v1",
  "disclaimer": "These hook suggestions are template rewrites, or LLM-simulated lines when source is \"llm\" or \"mixed\". They are not trained and they are not validated on real viewers. predicted_delta is a heuristic rescore of the rewritten opening with the persona scorer (predicted_delta_label is \"heuristic_estimate\"), not a measured lift.",
  "variants": [
    {
      "id": "v07",
      "original_hook": "There's just no price you can put on that.",
      "suggestions": [
        {
          "persona": "fast_swiper",
          "hook": "Wait — did you know there's just no price you can put on that?",
          "title_card": "NO PRICE",
          "reason": "Question form of this variant's hook line. Written in a fast-swiper voice (short and immediate). Template rewrite, not a trained or validated line. predicted_delta is a heuristic rescore, not a measured lift.",
          "predicted_delta": 0.0967,
          "predicted_delta_label": "heuristic_estimate",
          "origin": "heuristic"
        },
        {
          "persona": "fast_swiper",
          "hook": "One thing: there's just no price you can put on that.",
          "title_card": "NO PRICE",
          "reason": "Number-first frame in front of this variant's hook line. The word \"one\" is the template, not a count from the video. Written in a fast-swiper voice (short and immediate). Template rewrite, not a trained or validated line. predicted_delta is a heuristic rescore, not a measured lift.",
          "predicted_delta": 0.0444,
          "predicted_delta_label": "heuristic_estimate",
          "origin": "heuristic"
        },
        {
          "persona": "fast_swiper",
          "hook": "Stop — there's just no price you can put on that.",
          "title_card": "NO PRICE",
          "reason": "Bold-claim frame (\"stop\") around this variant's hook line. Written in a fast-swiper voice (short and immediate). Template rewrite, not a trained or validated line. predicted_delta is a heuristic rescore, not a measured lift.",
          "predicted_delta": 0.0333,
          "predicted_delta_label": "heuristic_estimate",
          "origin": "heuristic"
        },
        {
          "persona": "casual_browser",
          "hook": "Did you know there's just no price you can put on that?",
          "title_card": "NO PRICE",
          "reason": "Question form of this variant's hook line. Written in a casual-browser voice. Template rewrite, not a trained or validated line. predicted_delta is a heuristic rescore, not a measured lift.",
          "predicted_delta": 0.0533,
          "predicted_delta_label": "heuristic_estimate",
          "origin": "heuristic"
        },
        {
          "persona": "casual_browser",
          "hook": "One thing: there's just no price you can put on that.",
          "title_card": "NO PRICE",
          "reason": "Number-first frame in front of this variant's hook line. The word \"one\" is the template, not a count from the video. Written in a casual-browser voice. Template rewrite, not a trained or validated line. predicted_delta is a heuristic rescore, not a measured lift.",
          "predicted_delta": 0.0356,
          "predicted_delta_label": "heuristic_estimate",
          "origin": "heuristic"
        },
        {
          "persona": "casual_browser",
          "hook": "The truth: there's just no price you can put on that.",
          "title_card": "NO PRICE",
          "reason": "Bold-claim frame (\"the truth\") around this variant's hook line. Written in a casual-browser voice. Template rewrite, not a trained or validated line. predicted_delta is a heuristic rescore, not a measured lift.",
          "predicted_delta": 0.0267,
          "predicted_delta_label": "heuristic_estimate",
          "origin": "heuristic"
        },
        {
          "persona": "completionist",
          "hook": "Why does this matter: there's just no price you can put on that?",
          "title_card": "NO PRICE",
          "reason": "Question form of this variant's hook line. Written in a completionist voice (plain and informative). Template rewrite, not a trained or validated line. predicted_delta is a heuristic rescore, not a measured lift.",
          "predicted_delta": 0.0533,
          "predicted_delta_label": "heuristic_estimate",
          "origin": "heuristic"
        },
        {
          "persona": "completionist",
          "hook": "One reason this matters: there's just no price you can put on that.",
          "title_card": "NO PRICE",
          "reason": "Number-first frame in front of this variant's hook line. The word \"one\" is the template, not a count from the video. Written in a completionist voice (plain and informative). Template rewrite, not a trained or validated line. predicted_delta is a heuristic rescore, not a measured lift.",
          "predicted_delta": 0.0356,
          "predicted_delta_label": "heuristic_estimate",
          "origin": "heuristic"
        },
        {
          "persona": "completionist",
          "hook": "Here's why: there's just no price you can put on that.",
          "title_card": "NO PRICE",
          "reason": "Bold-claim frame (\"here's why\") around this variant's hook line. Written in a completionist voice (plain and informative). Template rewrite, not a trained or validated line. predicted_delta is a heuristic rescore, not a measured lift.",
          "predicted_delta": 0.0267,
          "predicted_delta_label": "heuristic_estimate",
          "origin": "heuristic"
        },
        {
          "persona": "sharer_engager",
          "hook": "Would you share this: there's just no price you can put on that?",
          "title_card": "NO PRICE",
          "reason": "Question form of this variant's hook line. Written in a sharer voice (claim-shaped). Template rewrite, not a trained or validated line. predicted_delta is a heuristic rescore, not a measured lift.",
          "predicted_delta": 0.1534,
          "predicted_delta_label": "heuristic_estimate",
          "origin": "heuristic"
        },
        {
          "persona": "sharer_engager",
          "hook": "The truth: there's just no price you can put on that.",
          "title_card": "NO PRICE",
          "reason": "Bold-claim frame (\"the truth\") around this variant's hook line. Written in a sharer voice (claim-shaped). Template rewrite, not a trained or validated line. predicted_delta is a heuristic rescore, not a measured lift.",
          "predicted_delta": 0.1067,
          "predicted_delta_label": "heuristic_estimate",
          "origin": "heuristic"
        },
        {
          "persona": "sharer_engager",
          "hook": "One thing worth passing on: there's just no price you can put on that.",
          "title_card": "NO PRICE",
          "reason": "Number-first frame in front of this variant's hook line. The word \"one\" is the template, not a count from the video. Written in a sharer voice (claim-shaped). Template rewrite, not a trained or validated line. predicted_delta is a heuristic rescore, not a measured lift.",
          "predicted_delta": 0.0623,
          "predicted_delta_label": "heuristic_estimate",
          "origin": "heuristic"
        }
      ]
    }
  ]
}
```

## `POST /api/persona/metrics`

Store observed per-variant metrics for one run. The file is `runs/<run>/metrics.json`. Other files in that directory (`vNN.json`, `results.json`, `manifest.json`, …) are not modified.

Python:

```python
store_metrics(run="v2_niosh_multi", payload={"metrics": {"v07": {"views": 1200, "avg_watch_pct": 54.3, "shares": 31, "likes": 80, "channel": "tiktok"}}})
store_metrics(run="v2_niosh_multi", csv_bytes=open("metrics.csv", "rb").read(), filename="metrics.csv")
```

`Content-Type` is either `application/json` or `multipart/form-data`.

### JSON body

Two shapes. `run` may instead be the query `?run=`.

Wrapper:

```json
{"run": "v2_niosh_multi", "simulated": false, "scope": "run", "metrics": {"v01": {"views": 1200, "avg_watch_pct": 54.3, "shares": 31}}}
```

Ab format (same object `python cli.py ab` accepts), plus `run` in the body or the query. `_meta` is optional metadata, not a variant:

```json
{"run": "v2_niosh_multi", "v01": {"views": 1200, "avg_watch_pct": 54.3, "shares": 31}}
```

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `run` | string | yes | Single path segment under `runs/`. The directory must already exist. |
| `metrics` | object | wrapper shape | Map of variant id → metric row. Unknown sibling fields are 400. |
| `simulated` | boolean | no | `true` marks the file simulated. `false` does **not** clear a detection (see below). `null` means unspecified. Strings `"true"` / `"false"` are accepted. |
| `scope` | string | no | `"run"` (default), `"global"`, or `"both"`. Which calibration file to write now. |
| `_meta` | object | no | Honors `simulated: true` and `generator`. A generator containing `make_mock_metrics`, `mock`, or `simul` forces `simulated: true`. |

A metric row:

| Field | Required | Meaning |
| --- | --- | --- |
| `views` | yes | Integer ≥ 1. |
| `avg_watch_pct` | yes | Number from 0 to 100. Values from 0 to 1 are fractions (so `1` means 100% watch, matching `clipcut/ab.py`). Values above 1 are percents. Above 100 is 400. |
| `shares` | yes | Integer ≥ 0 and ≤ `views`. |
| `channel` | no | Short string, stored and returned by compare. Not a fit feature. |
| `likes` | no | Integer ≥ 0. Stored. Not used in the outcome or the fit. |
| `simulated` | no | `true` on any row marks the whole file simulated. |

Variant ids match `^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$` and must not start with `_`.

### CSV upload

Multipart fields: `file` (required), `run` (required), `simulated` (optional), `scope` (optional). UTF-8. Header required. Columns: `variant_id`, `views`, `avg_watch_pct`, `shares`, and optional `channel`, `likes`. A filename containing `mock` or `simul` (for example `mock_metrics.csv`) forces `simulated: true`.

### Stored file

`_meta` is written first so the flag is visible before the numbers. Existing variant rows not mentioned in the upload are kept. Mentioned ids are replaced. Unknown top-level keys already in the file (anything that is not `_meta` and not a metric row) are kept. `python cli.py ab runs/<run>/metrics.json` still works: `clipcut/ab.py` skips keys that are not `{views, avg_watch_pct, shares}`.

```json
{
  "_meta": {"simulated": false, "source": "json", "n": 2, "updated_at": "2026-10-18T00:00:00Z"},
  "v01": {"views": 1200, "avg_watch_pct": 54.3, "shares": 31, "channel": "tiktok", "likes": 80}
}
```

When the file is simulated, `_meta.simulated` is `true` and each variant row also has `"simulated": true`.

### What counts as simulated

The file is simulated when any of these are true. A caller-supplied `simulated: false` does not override them.

- The upload says `simulated: true`, or `_meta.simulated` is true, or a row says `simulated: true`.
- `_meta.generator` mentions `make_mock_metrics`, `mock`, or `simul` (`make_mock_metrics.py` and `cli.py demo` both write this).
- The CSV filename contains `mock` or `simul`.
- The variant rows match `runs/demo/mock_metrics.json` on id, views, avg_watch_pct, and shares. That file is the checked-in output of `make_mock_metrics.py`.
- `metrics.json` was already simulated. A later upload cannot relabel it as real. Delete the file to start over.

Simulated metrics are stored. They are not used to fit weights. `POST /api/persona/score` with `calibration` set is 400 on that run. Compare still returns, with `simulated: true` and no `predicted_calibrated`.

`retention.simulate_ab` and `GET /api/channels/{run}` are a different mocked path. They do not write `metrics.json` and they are not calibration data.

### Response `200`

```json
{
  "run": "v2_niosh_multi",
  "file": "metrics.json",
  "n": 12,
  "added": ["v07"],
  "updated": ["v01"],
  "simulated": false,
  "simulated_reason": null,
  "calibration": {
    "fitted": true,
    "scope": "run",
    "guard": "ok",
    "n_samples": 12,
    "fit_quality": {},
    "feature_multipliers": {"hook_timing": 1.0},
    "persona_weights": {"fast_swiper": 0.3},
    "file": "persona_calibration.json"
  }
}
```

`fit_quality` has the same fields as the score response. `simulated_reason` is a string when `simulated` is true (`generator make_mock_metrics.py`, `matches make_mock_metrics.py output`, `filename mock_metrics.csv`, `declared`, `already marked simulated`, …) and `null` otherwise.

`calibration.fitted` is false when the metrics are simulated, or when fewer than 2 uploaded variants also have a `vNN.json` manifest. The reason string says which.

### Errors

| Status | When |
| --- | --- |
| 400 | Not a JSON object (and not a CSV upload). Missing `run`. Bad `scope`. Bad row (views, shares, watch, likes, channel, variant id). CSV missing required columns, duplicate ids, or not UTF-8. Unknown fields next to `metrics`. Empty metrics. |
| 404 | Run directory does not exist. |
| 409 | `metrics.json` exists and is not a JSON object. The file is left untouched. |

## Calibration

Given a run's real metrics and its `vNN.json` manifests, fit a small adjustment of the **feature weights** (one multiplier shared by all four personas) and the **persona mix**. Two stages, both view-weighted and column-centered ridge regressions toward the hand-set weights (delta 0):

1. Feature multipliers. The column for a feature is the derivative of the unclamped audience aggregate with respect to a shared multiplier, at the hand-set weights.
2. Persona-mix multipliers, using persona scores after step 1. The mix is then renormalized to sum to 1.

Columns that do not vary are left at multiplier 1. After the solve, multipliers are clipped:

| Samples in the fit | `guard` | Feature multiplier | Persona multiplier | `n_prior` |
| --- | --- | --- | --- | --- |
| 2–7 | `"shrink"` | 0.92–1.08 | 0.95–1.05 | `48 * (8 / n)²` |
| 8 or more | `"ok"` | 0.50–1.50 | 0.65–1.35 | 8 |

Fewer than 2 paired variants: no calibration file. The hand-set weights in code are never overwritten. Persona **bases** are not parameters.

The target is not the persona aggregate. It is a hand-set blend, so rank agreement is meaningful and a level gap stays visible:

```
watch_frac = avg_watch_pct / 100   if avg_watch_pct > 1 else avg_watch_pct
share_rate = shares / views
actual = 0.6 * watch_frac + 0.4 * min(1, share_rate / 0.05)
```

`0.05` (a 5% share rate) saturates the share term. That ceiling is a hand-set scale, not a measured benchmark. The 0.6/0.4 split matches `clipcut/ab.py`. Likes are not in the blend.

Fit quality (`r2`, `spearman`, `rmse`) is **in-sample** on the clamped aggregate versus `actual`. Spearman is the ranking check. R² can be negative because no intercept is fit.

### Where the weights are written

| `scope` on the metrics POST | File |
| --- | --- |
| `"run"` (default) | `runs/<run>/persona_calibration.json` |
| `"global"` | `runs/_calibration/global.json`, pooled across every run whose `metrics.json` is not simulated |
| `"both"` | Both files. The POST response then has `calibration.fitted` plus `calibration.scopes` (one summary per file) instead of a single summary. |

`_calibration` is not a run name (it starts with `_`). A later `POST /api/persona/score` with `calibration: "run"` refits that run if the file is missing or the metrics hash changed, as long as the metrics are real and at least two variants pair with manifests. `calibration: "global"` does the same for the pool. Marking a run simulated deletes that run's calibration file and refits or deletes the global file so the run cannot linger in the pool.

The calibration JSON includes `schema` (`persona-calibration-v1`), `simulated: false`, `n_samples`, `guard`, `feature_multipliers`, `persona_multipliers`, `feature_weights`, `hand_set_feature_weights`, `persona_weights`, `hand_set_persona_weights`, `bases` (unchanged), `outcome`, `fit_quality`, `metrics_sha256`, and the calibrated disclaimer. `run` is null on the global file; `runs` lists `{run, n, metrics_sha256}` instead.

## `GET /api/persona/compare/<run>`

Predicted versus actual for the dashboard, plus the Thompson-sampling winner from `clipcut/ab.py` (`analyze`, seed 0, 20,000 draws — the same defaults as `python cli.py ab`).

```python
compare_run("v2_niosh_multi")
```

### Response `200`

```json
{
  "run": "v2_niosh_multi",
  "simulated": false,
  "simulated_reason": null,
  "disclaimer": "predicted is the hand-set persona aggregate. predicted_calibrated is present only when a fit was made from metrics that are not simulated. actual is a hand-set blend of observed avg_watch_pct and share rate (0.6 watch, 0.4 share; share rate saturates at 5%). It is not a platform ranking. thompson_winner is the Beta posterior Thompson estimate from clipcut/ab.py.",
  "n": 12,
  "n_paired": 12,
  "spearman": 0.2,
  "spearman_calibrated": 0.31,
  "thompson_winner": "v07",
  "thompson_confident": false,
  "ab_seed": 0,
  "ab_draws": 20000,
  "thompson": [
    {"variant_id": "v07", "views": 1200, "watch_mean": 0.54, "share_rate": 0.027, "p_best": 0.41, "expected_loss": 0.01}
  ],
  "variants": [
    {
      "id": "v01",
      "predicted": 0.71,
      "predicted_calibrated": 0.68,
      "actual": 0.48,
      "views": 1200,
      "avg_watch_pct": 54.3,
      "shares": 31,
      "likes": null,
      "channel": "tiktok",
      "share_rate": 0.025833,
      "watch_frac": 0.543
    }
  ],
  "outcome": {"watch_weight": 0.6, "share_weight": 0.4, "share_rate_full": 0.05, "note": "actual = 0.6*watch_frac + 0.4*min(1, share_rate/0.05). share_rate_full is a hand-set saturation point, not a measured benchmark. Likes are stored and not used."},
  "calibration": {
    "guard": "ok",
    "n_samples": 12,
    "fit_quality": {},
    "feature_multipliers": {},
    "persona_weights": {},
    "hand_set_persona_weights": {"fast_swiper": 0.3, "casual_browser": 0.3, "completionist": 0.3, "sharer_engager": 0.1}
  }
}
```

| Field | Meaning |
| --- | --- |
| `simulated` | From the rules above. When true, the disclaimer is the simulated sentence below, `predicted_calibrated` is null on every row, `spearman_calibrated` is null, and `calibration` is null. |
| `spearman` | Hand-set aggregate vs `actual`, over variants that have both a manifest and a metrics row. `null` if fewer than 2. |
| `spearman_calibrated` | Same, using the run fit. `null` when there is no real fit. |
| `predicted` | Hand-set persona aggregate. `null` when that id has metrics but no `vNN.json`. |
| `predicted_calibrated` | Aggregate with the run's fitted weights. `null` when metrics are simulated, when the fit does not exist, or when there is no manifest. |
| `actual` | The watch/share blend above, rounded to 4 decimals. |
| `thompson_winner` | `clipcut.ab.analyze` winner on the stored rows. This is not the persona-score winner. |
| `thompson_confident` | True when that winner's `p_best` is at least 0.95. |
| `thompson` | The `analyze` table, `p_best` descending. Floats rounded to 6 decimals. |
| `variants` | Metric rows, sorted by id. |

Simulated disclaimer (exact):

`These metrics are simulated, not platform results. predicted is the hand-set persona aggregate. Weights were not calibrated on this file. thompson_winner is the Beta posterior Thompson estimate from clipcut/ab.py on the simulated rows, not a real test winner.`

When the metrics are real and at least two variants pair with manifests, this GET refits `persona_calibration.json` if it is missing or stale. It does not refit on simulated metrics.

### Errors

Body is always `{"error": "<message>"}`.

| Status | When |
| --- | --- |
| 400 | `run` is not a single safe path segment, or a stored row is outside the views/shares/watch bounds. |
| 404 | No such run, no `metrics.json`, or the file has no variant rows. |
