"""Per-platform export specs. Channel ids are the lowercased clipcut.channels.CHANNEL_SPECS keys
(the same ids docs/PERSONA_API.md and metrics rows use: tiktok, shorts, reels, x, linkedin).

Text limits are hand-collected platform limits as of 2026; verify before relying on them.
"""
from __future__ import annotations

import re

from ..channels import CHANNEL_SPECS


class DistributionError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# id -> export rules. `aspects` comes from CHANNEL_SPECS (first one is preferred for a reframe).
_TEXT = {
    "tiktok": dict(title_max=None, caption_max=2200, hashtags_max=5, fixed_tags=[],
                   post_fields=["caption"], notes="Caption carries the hashtags. Captions are burned in; upload the SRT too if the app offers it."),
    "shorts": dict(title_max=100, caption_max=5000, hashtags_max=3, fixed_tags=["#Shorts"],
                   post_fields=["title", "caption"], notes="Title (100 chars) does the hooking in search; description holds the hashtags. Upload the SRT as captions."),
    "reels": dict(title_max=None, caption_max=2200, hashtags_max=5, fixed_tags=[],
                  post_fields=["caption"], notes="First ~125 chars of the caption show before 'more'. Instagram allows up to 30 hashtags; fewer works better."),
    "x": dict(title_max=None, caption_max=280, hashtags_max=2, fixed_tags=[],
              post_fields=["caption"], notes="280 characters including hashtags (free accounts). Upload the SRT; most autoplay is muted."),
    "linkedin": dict(title_max=None, caption_max=3000, hashtags_max=3, fixed_tags=[],
                     post_fields=["caption"], notes="First ~210 chars show before 'see more'; lead with the hook. Upload the SRT."),
}
DEFAULT_CHANNELS = ["tiktok", "shorts", "reels", "x", "linkedin"]
_LABEL = {k.lower(): k for k in CHANNEL_SPECS}


def _build():
    out = {}
    for cid in DEFAULT_CHANNELS:
        base = CHANNEL_SPECS[_LABEL[cid]]
        w, h = base["resolution"]
        aspects = list(base["aspect"])
        out[cid] = dict(
            id=cid,
            label=_LABEL[cid],
            platform=base["platform"],
            aspects=aspects,
            target_aspect=aspects[0],
            resolution=_resolution_for(aspects[0], (w, h)),
            max_s=base["max_s"],
            ideal_s=list(base["ideal_s"]),
            captions_note=base["captions"],
            **_TEXT[cid],
        )
    return out


def _resolution_for(aspect, fallback):
    return {"9:16": [1080, 1920], "1:1": [1080, 1080], "4:5": [1080, 1350], "16:9": [1280, 720]}.get(aspect, list(fallback))


CHANNELS = _build()


def channel_id(raw):
    """'TikTok' / 'tiktok' / ' Shorts ' -> 'tiktok' / 'shorts'. None when unknown."""
    if not isinstance(raw, str):
        return None
    key = raw.strip().lower()
    if key in CHANNELS:
        return key
    if key in _LABEL:  # a CHANNEL_SPECS id we do not export to (youtube, ig_feed, ...)
        return key
    return None


def spec(cid):
    return CHANNELS[cid]


def parse_channels(channels):
    if channels is None:
        return list(DEFAULT_CHANNELS)
    if not isinstance(channels, list) or not channels or not all(isinstance(c, str) and c.strip() for c in channels):
        raise DistributionError("channels must be a non-empty list of channel ids")
    out, unknown = [], []
    for raw in channels:
        key = raw.strip().lower()
        (out if key in CHANNELS else unknown).append(key if key in CHANNELS else raw)
    if unknown:
        raise DistributionError("unknown channels: " + ", ".join(unknown) + " (supported: " + ", ".join(DEFAULT_CHANNELS) + ")")
    if len(set(out)) != len(out):
        raise DistributionError("channels contains duplicates")
    return out


def aspect_of(manifest):
    a = manifest.get("aspect") or (manifest.get("recipe") or {}).get("aspect")
    if isinstance(a, str) and re.fullmatch(r"\d+:\d+", a):
        return a
    res = manifest.get("resolution")
    if isinstance(res, (list, tuple)) and len(res) == 2 and res[1]:
        r = res[0] / res[1]
        return min(("9:16", 9 / 16), ("4:5", 0.8), ("1:1", 1.0), ("16:9", 16 / 9), key=lambda kv: abs(kv[1] - r))[0]
    return None


def best_target(cid, src_aspect):
    """Accepted aspect closest to the source (so a reframe crops as little as possible) and its resolution."""
    import math
    accepted = CHANNELS[cid]["aspects"]
    if not src_aspect:
        a = accepted[0]
    else:
        a = min(accepted, key=lambda x: (abs(math.log(ratio(x) / ratio(src_aspect))), accepted.index(x)))
    return a, _resolution_for(a, CHANNELS[cid]["resolution"])


def ratio(aspect):
    w, h = aspect.split(":")
    return int(w) / int(h)
