# Edit-chip branch API

Contract for drag-and-drop edit chips on the node canvas. The canvas calls this. It does not render chips itself.

Python entry point:

```python
from clipcut.chip_branch import create_branch, get_branch

create_branch({"run": "demo", "parent_variant_id": "source", "chip": {"type": "crop", "params": {"aspect": "9:16"}}}, runs_root="runs", root=".")
get_branch("c_ab12cd34", runs_root="runs")
```

`create_branch` returns the same object as `POST /api/branch`. Invalid input raises `clipcut.chip_branch.ChipError` (`.status` is the HTTP status, `str(err)` is the `detail` string).

This is separate from `POST /api/branch/queue` and `GET /api/branch/status`, which still render full cutter-v2 recipes one at a time. Chip drops do not use that queue.

## What a chip render is

Each branch applies **one** chip to its parent video with ffmpeg. Stacking is a new branch whose `parent_variant_id` is another branch id, so the second render starts from the first branch's file. Crop, then captions, then length is three branches, not one request with three chips.

These files are real ffmpeg outputs. The score is not. See [Score](#score).

A music bed is a generated tone when no royalty-free audio file is shipped with the app. The response says so. It is not a licensed track.

## `POST /api/branch`

`Content-Type: application/json`. Returns as soon as the job is queued. Rendering happens on a pool of **3** workers and does **not** take the one-job lock used by `POST /api/cut` (`/api/status` can stay `busy` while chips render, and a busy cut does not reject a chip).

### Request

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `run` | string | yes | Directory name under `runs/`. One path segment: letters, digits, `_`, `-`. The directory must already exist. |
| `parent_variant_id` | string | yes | `"source"`, a variant id such as `"v07"` that has `runs/{run}/{id}.mp4`, or a previous chip branch id `c_` + 8 hex chars. |
| `chip` | object | yes | `{ "type", "params" }`. `params` defaults to `{}`. |

`parent_variant_id: "source"` uses the mp4 named by `runs/{run}/manifest.json` (`source`), then `samples/{filename}`, then `runs/{run}/{filename}`, then `runs/{run}/src_*.mp4`. A missing file is **400**. A variant id whose `vNN.json` exists but whose mp4 does not is **400**. An unknown id is **404**.

### Chip types

| `chip.type` | `params` | Default | What ffmpeg does |
| --- | --- | --- | --- |
| `captions` | `style`: `bold` \| `minimal` \| `karaoke` | `bold` | Burns an ASS line. Text comes from the parent variant's captions, else its hook line, else `This is the cut`. Karaoke uses `\kf` word timings. |
| `zoom` | `punch_ins`: bool, `intensity`: number 0–3 | `true`, `1` | Punch-in crop about every 2.6s. `intensity` 1 zooms to about 1.18. `0` or `punch_ins: false` leaves the frame unzoomed. |
| `length` | `seconds`: `15` \| `30` \| `45` | `30` | Keeps the start of the parent, up to that many seconds. A shorter parent stays shorter. |
| `hook` | `text`: string, optional, max 140 chars | none | Title card for the first 2.8s. See [Hook text](#hook-text). |
| `crop` | `aspect`: `9:16` \| `1:1` \| `16:9` | `9:16` | Cover-scale and center crop. Full frame is 720×1280, 1080×1080, or 1280×720. |
| `music` | `track`: `upbeat` \| `chill` \| `none` | `upbeat` | Mixes a generated tone under the parent audio, or adds it if the parent is silent. `none` leaves the audio as it is. |

Unknown type or an out-of-range param is **400**.

### Response `200`

```json
{
  "branch_id": "c_ab12cd34",
  "status_url": "/api/branch/c_ab12cd34",
  "preview_url": "/runs/demo/branches/c_ab12cd34/preview.mp4"
}
```

`preview_url` is stable immediately. The file is there once `GET` reports `preview` or `done`. Poll `status_url`. Do not block the drop handler on the render.

### Example

```json
{
  "run": "demo",
  "parent_variant_id": "source",
  "chip": {"type": "captions", "params": {"style": "karaoke"}}
}
```

Stack a length trim on that branch:

```json
{
  "run": "demo",
  "parent_variant_id": "c_ab12cd34",
  "chip": {"type": "length", "params": {"seconds": 15}}
}
```

## `GET /api/branch/{branch_id}`

`branch_id` is the id from the POST. `GET /api/branch/status` is still the legacy batch route, not a chip id.

### Response `200`

```json
{
  "branch_id": "c_ab12cd34",
  "run": "demo",
  "parent_variant_id": "source",
  "parent_kind": "source",
  "status": "preview",
  "preview_url": "/runs/demo/branches/c_ab12cd34/preview.mp4",
  "video_url": null,
  "thumb_url": "/runs/demo/branches/c_ab12cd34/thumb.jpg",
  "score": {
    "value": 0.71,
    "source": "heuristic",
    "disclaimer": "Heuristic persona score from the chip stack. Not a measured result."
  },
  "chip": {"type": "captions", "params": {"style": "karaoke"}},
  "stack": [
    {"type": "captions", "params": {"style": "karaoke"}, "text": "This is the cut"}
  ],
  "error": null,
  "timing": {"preview_s": 1.2}
}
```

| Field | Meaning |
| --- | --- |
| `status` | `queued`, `preview`, `done`, or `error`. |
| `preview_url` | Low-res mp4. First 8 seconds (or the whole clip if it is shorter), long side ≤ 480, libx264 `ultrafast`, 12 fps. Ready when status is `preview` or `done`. |
| `video_url` | Full render. `null` until `done`. Long side ≤ 1280, `veryfast`, 30 fps, except a length or `music: none` full render, which stream-copies. Crop uses the fixed frames in the table above. |
| `thumb_url` | Poster jpeg. `null` until the poster exists (same time as `preview`, or just before). |
| `stack` | Chips from the root parent to this branch. `stack[0]` was applied first. `stack[-1]` is this branch. |
| `parent_kind` | `source`, `variant`, or `branch`. |
| `timing.preview_s` | Seconds from start of work until the preview file was ready. Present from `preview` onward. |
| `timing.full_s` | Seconds spent on the full render only. Present when `done`. |
| `error` | `null`, or a short ffmpeg / parent failure string when `status` is `error`. |
| `hook` | Only for a hook chip: `{ "text", "source" }`. `source` is `request`, `persona-hooks`, or `template`. |
| `music_placeholder` | `true` when this branch or an ancestor mixed a generated tone. |
| `music_label` | `ffmpeg-generated sine bed (placeholder, not a licensed track)` when `music_placeholder` is true. |

`video_url` and `thumb_url` are either `null` or a path under `/runs/`. The static mount is the existing `/runs` app mount. Nothing in this API writes outside `runs/{run}/branches/{branch_id}/`.

Poll about twice a second. A preview on a few-second parent is typically ready in a few seconds; the full file follows. If the parent is another branch that is still `queued`, this branch stays `queued` until that parent reaches `preview` (for this branch's preview) and `done` (for this branch's full file). The full file is always encoded from the parent's `video.mp4`, not from the parent's preview.

### Score

`score.value` is a float in `[0, 1]`. `score.source` is `heuristic` or `simulated`.

- `heuristic` — `clipcut.persona_api.score_variants(..., mode="heuristic", calibration="none")`, the same scorer as `POST /api/persona/score` in heuristic mode. No network. The manifest is built from the chip stack (aspect, length, caption style, hook line, punch-in cut times). This is the usual source, including while the job is still `queued`.
- `simulated` — used only if that call throws. Weights, from a 0.52 base: captions karaoke +0.09, bold +0.07, minimal +0.03; zoom `0.05 * min(intensity, 2)` when punch-ins are on; length 15/30/45 → +0.06/+0.05/+0.02; hook +0.08 with text else +0.04; crop 9:16/1:1/16:9 → +0.07/+0.04/+0.02; music upbeat/chill/none → +0.04/+0.03/0. Clamped to `[0, 1]`.

Neither number is a trained model, a watch-time prediction, or an A/B result. `score.disclaimer` says that. The value can change between `queued` and `done` once the real duration is known.

## Hook text

For `chip.type: "hook"`:

1. `params.text`, when it is a non-empty string, is used as-is (`source: "request"`).
2. Otherwise, if the parent variant (or ancestor branch) has a hook line, caption, or transcript excerpt, the server calls the same heuristic rewriter as `POST /api/persona/hooks` (`rewrite_hooks`, `mode: "heuristic"`, persona `fast_swiper`, `n: 1`). No network. `source` is `persona-hooks`.
3. If there is no line to rewrite, or the rewriter returns nothing, the card is `Wait — this is the part worth keeping.` (`source: "template"`).

## Files

```
runs/{run}/branches/{branch_id}/preview.mp4
runs/{run}/branches/{branch_id}/video.mp4
runs/{run}/branches/{branch_id}/thumb.jpg
runs/{run}/branches/{branch_id}/branch.json
runs/{run}/branches/{branch_id}/bed.m4a    # music chip only, the generated tone
```

`branch.json` is the job record `GET` reads, so a restart can still report a finished branch. Do not commit the mp4s.

## Errors

FastAPI's default shape: `{"detail": "<message>"}`.

| Status | When |
| --- | --- |
| 400 | Body is not an object. Missing `run`, `parent_variant_id`, or `chip`. Bad chip type or params. Run name is not a single safe segment. Source mp4 is not on disk. Variant json exists but the mp4 does not. |
| 404 | No such run. No such branch. `parent_variant_id` is not `source`, not a file in the run, and not a known branch. |

A render failure does not change the HTTP status of `GET`. `GET` stays **200** with `status: "error"` and `error` set. `preview_url` may still play if the preview finished before the full render failed.

## Worker pool

Three chip renders run at the same time. A fourth waits. Jobs whose parent branch is not ready yet wait without occupying a worker, so a chain of drops cannot fill the pool with blocked jobs.

`POST /api/cut` and `POST /api/branch/queue` still run one cutter job at a time. Chip renders do not register in that lock, and they do not wait for it.

## Canvas drop payload

`canvas.html` already posts a different body and falls back to `POST /api/branch/queue` if this route rejects it. This route accepts that body so the fallback is not used.

```json
{
  "run": "demo",
  "variant_id": "v07",
  "parent": "src",
  "chips": ["len15", "zoom", "captions"],
  "video": "/runs/demo/v07.mp4",
  "settings": {"length": 15, "captions": "word_pop", "aspect": "9:16"}
}
```

| Canvas field | How it is read |
| --- | --- |
| `chips` | Chip names, in order: `captions`, `zoom`, `len15`, `len30`, `len45`, `hook`, `crop`, `music`. Several names queue a chain (length, then zoom, then captions) and the response is the **leaf** branch. `parent` is the canvas node id and is not a variant id. |
| `variant_id` | Used when `video` is not already a branch file. Needs `runs/{run}/{variant_id}.mp4`. |
| `video` | Public path of the clip the node is showing. `/runs/{run}/branches/{id}/preview.mp4` or `video.mp4` parents the new chip on that branch and applies only the chips past that branch's stack, so captions are not burned twice. |
| `settings` | Optional hints. `captions: "word_pop"` → bold, `"sentence"` → minimal, `"karaoke"` → karaoke. `aspect` overrides crop. `track` overrides music. `intensity` overrides zoom. |

Chip names map to params: captions bold, zoom punch-ins at intensity 1, len15/30/45, hook with no text (persona line or the template), crop 9:16, music upbeat.

The same GET object also carries the fields the current canvas reads:

| Alias | When |
| --- | --- |
| `id` | Same as `branch_id`. |
| `video` | Playable URL. The input clip while `queued` (so the node keeps its current frame), `preview_url` while `preview`, `video_url` when `done`. |
| `preview_video` | `preview_url` once a preview exists. |
| `poster`, `thumbnail` | Same as `thumb_url` once the poster exists. |
| `source` | Same as `score.source` (`heuristic` or `simulated`). The canvas prints this on the node. |

`video_url` itself stays `null` until `done`. `preview_url` is always the preview path, including on the POST response, which is what a client that waits for `status: "preview"` should load.
