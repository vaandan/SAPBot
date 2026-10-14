"""Model dispatcher / router (FSD §2 'Router', ADR B7).

Claude by default. The local model is chosen only when ALL hold:
  * features local_model and router are enabled for the workspace
  * RAG finds a similar gold run (similarity >= 0.75) whose T-code has >= router.min_gold_examples gold runs
  * the workspace eval for that T-code passed (kv 'eval_pass:<ws>:<tcode>' = '1', set by the eval harness)
Otherwise Claude. If claude_api_mode is off but local_model is on, the local model is the only brain.
"""
from __future__ import annotations

import logging
from typing import Any

from ..config import get_settings

log = logging.getLogger(__name__)


def choose_brain(ex: Any, ws: Any, task: str) -> str:
    gov = ex.gov
    claude_ok = gov.feature("claude_api_mode", ws.id)
    local_ok = gov.feature("local_model", ws.id) and bool(get_settings().models.local_model.base_url)
    if ws.data_policy == "local-only":
        if not local_ok:
            raise PermissionError(f"workspace '{ws.id}' is local-only and no on-prem model is enabled")
        return "local"
    if not claude_ok and not local_ok:
        raise PermissionError("neither Claude API mode nor the local model is enabled (Admin > Features)")
    if not local_ok:
        return "claude"
    if not claude_ok:
        return "local"
    if not gov.feature("router", ws.id):
        return "claude"
    res = ex.resources(ws)
    if res.rag is None:
        return "claude"
    hits = res.rag.search_similar_trajectories(task, 1)
    if not hits or hits[0]["similarity"] < 0.75:
        return "claude"
    tcode = (hits[0].get("t_codes") or "").split(",")[0]
    stats = res.rag.get_stats()["per_t_code"]
    if stats.get(tcode, 0) < get_settings().router.min_gold_examples:
        return "claude"
    if gov.store.kv_get(f"eval_pass:{ws.id}:{tcode}") != "1":
        return "claude"
    return "local"
