"""Legacy variant scorer. `score` is clipcut.retention.score (hand-set heuristic).

POST /api/score and rank_v2.py keep that payload. New callers should use
clipcut.persona_api.score_variants (docs/PERSONA_API.md): rule-based persona
heuristics, or optional LLM-simulated judgments. Not a trained model.
"""
from .retention import score  # noqa: F401
