"""SRT / WebVTT from cutter manifest captions ([{start, end, text}], output-timeline seconds)."""
from __future__ import annotations


def _cues(captions, duration=None):
    out = []
    for c in captions or []:
        if not isinstance(c, dict):
            continue
        try:
            start, end = float(c.get("start")), float(c.get("end"))
        except (TypeError, ValueError):
            continue
        text = " ".join(str(c.get("text") or "").split())
        if not text:
            continue
        if duration is not None:
            if start >= duration:
                continue
            end = min(end, duration)
        start = max(0.0, start)
        if end <= start:
            continue
        out.append((start, end, text))
    out.sort(key=lambda t: t[0])
    # Cutter cues overlap by ~50 ms; players handle it, but clean it up so each cue ends before the next.
    fixed = []
    for i, (s, e, t) in enumerate(out):
        if i + 1 < len(out) and e > out[i + 1][0] > s:
            e = out[i + 1][0]
        fixed.append((s, e, t))
    return fixed


def _ts(sec, sep):
    ms = int(round(sec * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def to_srt(captions, duration=None):
    blocks = [f"{i}\n{_ts(s, ',')} --> {_ts(e, ',')}\n{t}\n" for i, (s, e, t) in enumerate(_cues(captions, duration), 1)]
    return "\n".join(blocks)


def to_vtt(captions, duration=None):
    blocks = [f"{_ts(s, '.')} --> {_ts(e, '.')}\n{t}\n" for s, e, t in _cues(captions, duration)]
    return "WEBVTT\n\n" + "\n".join(blocks)


def cue_count(captions, duration=None):
    return len(_cues(captions, duration))
