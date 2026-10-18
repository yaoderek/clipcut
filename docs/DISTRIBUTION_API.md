# Distribution API

Contract for the "after the edit" lane: per-channel export bundles, publish, and the campaign dashboard.
Code: `clipcut/distribution/` (mounted in `app.py` with one `include_router` line). Errors are always
`{"error": "<message>"}` with the HTTP status, same as the persona routes.

Channel ids are the lowercased `clipcut.channels.CHANNEL_SPECS` keys used everywhere else
(`docs/PERSONA_API.md`, metrics rows): **`tiktok`, `shorts`, `reels`, `x`, `linkedin`**.

`POST /api/deploy/{run}` is unchanged (legacy "copy winner.mp4").

Python entry points:

```python
from clipcut.distribution import export_run, load_export, build_zip
export_run("v2_niosh_multi", variant_id="v07", channels=["tiktok", "x"], mode="auto")
```

## `GET /api/distribution/channels`

`{"channels": [{id, label, platform, aspects, target_aspect, resolution, max_s, ideal_s, title_max, caption_max, hashtags_max, fixed_tags, post_fields, notes, captions_note}]}`.
Text limits are hand-collected platform limits as of 2026 (TikTok caption 2200, Shorts title 100 / description 5000,
Reels caption 2200, X post 280, LinkedIn post 3000).

## `POST /api/distribution/export`

Builds one bundle per channel under `runs/<run>/distribution/<export_id>/<channel>/`. Synchronous; with
`render: true` a reframe or trim runs ffmpeg in-request (seconds per channel on the Fly box).

### Request

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `run` | string | yes | Run directory under `runs/` (single path segment). Variants are its `vNN.json`. |
| `variant_id` | string | no | The variant to ship (canvas: `packet.chosen.id`). Omit to let each channel pick its own best variant. |
| `channels` | array of ids | no | Default all five, in the order above. Unknown id / duplicates / `[]` → 400. |
| `mode` | `"auto"` \| `"llm"` \| `"template"` | no | Copy writer. `auto` (default): Grok when `XAI_API_KEY`/`GROK_API_KEY` is set and the reply is usable, else templates. `llm` with no key → 503. |
| `render` | bool | no | Default `true`. `false` never runs ffmpeg (variants that need a reframe/trim report `video.status: "skipped"`). |

### Variant selection per channel (`channels[].selection`)

- With `variant_id`: `requested` when its aspect is accepted by the channel; else `sibling_aspect` — the best-ranked
  variant with the same hook (`recipe.hook`) in an accepted aspect (e.g. the 1:1 X cut of the same moment); else
  `requested_reframe` — ffmpeg reframes the requested variant to the accepted aspect closest to its own
  (9:16 → 1:1 / 4:5 crops top and bottom; much wider or narrower targets use a blurred fill).
- Without: `channel_target` (best-ranked variant cut for that channel), else `aspect_match`, else `top_reframe`.
- Ranking is `results.json` order when present, else the persona heuristic aggregate.
- Longer than the channel's `max_s` → trimmed (`trimmed: true`, X 140 s, Shorts/Reels 180 s).

### Response `200`

```json
{
  "run": "v2_niosh_multi",
  "export_id": "ex_20261017_201500_ab12",
  "created_at": "2026-10-17T20:15:00-0700",
  "requested_variant": "v07",
  "mode": "auto",
  "copy_source": "template",
  "disclaimer": "Export bundles are ready-to-upload files. ...",
  "bundle_url": "/api/distribution/bundle/v2_niosh_multi/ex_20261017_201500_ab12.zip",
  "channels": [
    {
      "channel": "x", "label": "X", "variant_id": "v13", "selection": "sibling_aspect",
      "aspect": "1:1", "target_aspect": "1:1", "needs_reframe": false, "reframed": false,
      "duration_s": 27.32, "max_s": 140, "trimmed": false,
      "video": {"status": "copied", "url": "/runs/v2_niosh_multi/distribution/ex_.../x/x_v13.mp4", "note": null},
      "captions": {"burned": true, "style": "sentence", "cues": 9,
                   "srt_url": "/runs/.../x/captions.srt", "vtt_url": "/runs/.../x/captions.vtt"},
      "copy": {
        "title": null,
        "caption": "Would you share this: you need to be a research assistant? #Assistant #Statistics",
        "hashtags": ["#Assistant", "#Statistics"],
        "hook": "Would you share this: you need to be a research assistant?",
        "title_card": "RESEARCH ASSISTANT",
        "source": "template", "seed": "hook_rewrite", "model_version": "dist-copy-template-v1",
        "limits": {"title_max": null, "caption_max": 280, "hashtags_max": 2},
        "lengths": {"title": 0, "caption": 82}
      },
      "post_url": "/runs/.../x/post.txt",
      "ready": true
    }
  ]
}
```

- `video.status`: `copied` (variant already fits; hard-linked), `reframed`, `trimmed`, `missing` (mp4 not on this
  server — mp4s are not in git; text, SRT/VTT and copy are still produced), `skipped` (`render: false`),
  `unavailable` (no ffmpeg), `failed` (ffmpeg error; `note` has the tail). `ready` is true only when the bundle has a video.
- `captions.burned`: the variant's `recipe.captions` is not `none` (cutter burns captions in). SRT and VTT are always
  written from the manifest's `captions` cues, clipped to the exported duration.
- `copy.source`: `"llm"` (Grok wrote it) or `"template"`. Templates only reuse words already on the variant: the hook
  line comes from `persona_api.rewrite_hooks(mode="heuristic", channels=[channel])` for that channel's audience
  persona (`seed: "hook_rewrite"`, falls back to raw `hook_text`), hashtags are frequent content words from the hook
  and captions. Server enforces limits on both paths (`lengths` ≤ `limits`; hashtags count toward the caption).
  `copy.note` appears when an LLM attempt fell back to templates. `copy_source` is `"mixed"` if channels differ.
- Each channel dir also has `copy.json` and `post.txt` (paste-ready title/caption).

Errors: 400 bad JSON / run segment / channels / mode; 404 unknown run, no `vNN.json`, unknown `variant_id`; 503 `mode: "llm"` without a key.

## `GET /api/distribution/export/{run}[?export_id=...]`

The latest export for the run (or the named one): the same object as POST, plus `exports` (all export ids, newest first).
404 when the run has no export yet.

## `GET /api/distribution/bundle/{run}/{export_id}.zip[?channel=x]`

Zip of `export.json` + every channel dir (`<run>_<export_id>/<channel>/{<channel>_<vid>.mp4, captions.srt, captions.vtt, copy.json, post.txt}`),
or a single channel with `?channel=`. mp4s are stored uncompressed. Built once and cached next to the export.

## Publish node contract (for derek-canvas)

Replace the current `publish` executor (`POST /api/deploy/<run>`) with:

1. `POST /api/distribution/export` with
   `{"run": packet.runId, "variant_id": packet.chosen && packet.chosen.id, "channels": node.config.channels || ["tiktok","shorts","reels","x","linkedin"], "mode": node.config.copy || "auto"}`.
   Omit `variant_id` when there is no `packet.chosen` (each channel then picks its own best cut).
2. Render one row per `channels[]`: label, `variant_id` (+ `selection` when it is not `requested`), `video.status`
   (video preview from `video.url` when present, else `video.note`), `copy.title`/`copy.caption`, a
   `copy.source` badge (`llm` / `template`), and links to `srt_url` and `post_url`.
3. A "Download bundle" link to `bundle_url` (per-channel: `bundle_url + "?channel=" + id`).
4. Suggested node config: `channels` (multi-select of the five ids), `copy` (`auto` | `template`), `action`
   (`export` | `publish`). `action: "publish"` additionally calls `POST /api/distribution/publish` (section below) with the returned `export_id`.

The node tip should say: exports are files; nothing is posted unless publish reports `uploaded (private)`.
