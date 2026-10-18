"""Distribution lane: per-channel export bundles, publish, and the campaign dashboard.

See docs/DISTRIBUTION_API.md. Nothing here writes metrics; observed numbers go
through clipcut.persona_api.store_metrics / POST /api/persona/metrics.
"""
from .specs import CHANNELS, DEFAULT_CHANNELS, DistributionError, channel_id, spec
from .captions import to_srt, to_vtt
from .copygen import generate_copy
from .bundle import export_run, load_export, build_zip

__all__ = [
    "CHANNELS", "DEFAULT_CHANNELS", "DistributionError", "channel_id", "spec",
    "to_srt", "to_vtt", "generate_copy", "export_run", "load_export", "build_zip",
]
