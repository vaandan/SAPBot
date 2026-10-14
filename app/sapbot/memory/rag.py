"""RAG Trajectory Retrieval Engine (FSD 3.6) - one store per client workspace (ADR A13).

  index_trajectory()             only gold runs (final_status S or verified read task)
  search_similar_trajectories()  top-k by cosine similarity, optional T-code filter, S-only
  build_augmented_prompt()       few-shot block injected between system prompt and the user task
  get_stats()                    counts per T-code for the dashboard
Backed by ChromaDB in local persistent mode at <ws>/chroma (FSD: ./chroma_store/). Embeddings computed by SAPBot
(ONNX MiniLM or offline fallback) and passed explicitly, so the store never downloads anything by itself.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from .embeddings import get_embedder
from .trajectory import Run

log = logging.getLogger(__name__)


class TrajectoryRAG:
    def __init__(self, chroma_dir: Path, collection: str = "sap_trajectories") -> None:
        import chromadb
        from chromadb.config import Settings as ChromaSettings
        self.embedder = get_embedder()
        chroma_dir.mkdir(parents=True, exist_ok=True)
        self.client = chromadb.PersistentClient(path=str(chroma_dir),
                                                settings=ChromaSettings(anonymized_telemetry=False))
        self.col = self.client.get_or_create_collection(f"{collection}_{self.embedder.name}",
                                                        metadata={"hnsw:space": "cosine"}, embedding_function=None)

    # ---------------------------------------------------------------- index
    def index_trajectory(self, run: Run) -> bool:
        if run.quality != "gold":
            return False
        steps = [{"tool": s.tool, "args": {k: v for k, v in s.args.items() if k != "reasoning"},
                  "status": (s.status_bar or {}).get("type", ""), "msg": (s.status_bar or {}).get("text", "")[:160],
                  "why": s.reasoning[:200]} for s in run.steps]
        capped_steps = steps[:30]
        while len(capped_steps) > 1 and len(json.dumps(capped_steps, ensure_ascii=False)) > 20000:
            capped_steps = capped_steps[:-1]
        final = run.final_status or "S"
        meta = {"task_prompt": run.task[:2000], "t_code": (run.t_codes[0] if run.t_codes else ""),
                "t_codes": ",".join(run.t_codes), "step_count": len(run.steps),
                "final_status": "S" if final in ("S", "") else final, "timestamp": run.ended,
                "documents": ",".join(run.document_numbers), "summary": run.summary[:500],
                "serialized_steps_json": json.dumps(capped_steps, ensure_ascii=False)}
        self.col.upsert(ids=[run.run_id], documents=[run.task], metadatas=[meta],
                        embeddings=self.embedder.embed([run.task]))
        return True

    # ---------------------------------------------------------------- search
    def search_similar_trajectories(self, query: str, top_k: int = 3, filter_t_code: str | None = None,
                                    min_similarity: float = 0.0) -> list[dict[str, Any]]:
        n = self.col.count()
        if n == 0:
            return []
        top_k = max(1, min(int(top_k or 3), 5))
        where: dict[str, Any] = {"final_status": "S"}
        if filter_t_code:
            where = {"$and": [where, {"t_code": filter_t_code.upper()}]}
        try:
            res = self.col.query(query_embeddings=self.embedder.embed([query]), n_results=min(top_k, n),
                                 where=where)
        except Exception as e:  # noqa: BLE001
            log.warning("RAG query failed: %s", e)
            return []
        out = []
        for i, rid in enumerate(res["ids"][0]):
            meta = res["metadatas"][0][i]
            sim = 1.0 - float(res["distances"][0][i])
            if sim < min_similarity:
                continue
            raw_steps = meta.get("serialized_steps_json")
            try:
                parsed_steps = json.loads(raw_steps) if raw_steps else []
            except Exception:  # noqa: BLE001
                parsed_steps = []
            out.append({"run_id": rid, "similarity": round(sim, 3), "task": meta.get("task_prompt"),
                        "t_codes": meta.get("t_codes"), "documents": meta.get("documents"),
                        "summary": meta.get("summary"),
                        "steps": parsed_steps})
        return out

    def build_augmented_prompt(self, task_prompt: str, top_k: int = 3, min_similarity: float = 0.0) -> tuple[str, list]:
        hits = self.search_similar_trajectories(task_prompt, top_k, min_similarity=min_similarity)
        return format_examples(hits), hits

    # ---------------------------------------------------------------- stats
    def get_stats(self) -> dict[str, Any]:
        got = self.col.get(include=["metadatas"])
        per: dict[str, int] = {}
        for m in got.get("metadatas") or []:
            for t in (m.get("t_codes") or m.get("t_code") or "-").split(","):
                per[t or "-"] = per.get(t or "-", 0) + 1
        return {"indexed": self.col.count(), "per_t_code": dict(sorted(per.items(), key=lambda kv: -kv[1])),
                "embedder": self.embedder.name}

    def delete(self, run_id: str) -> None:
        self.col.delete(ids=[run_id])


def format_examples(hits: list[dict[str, Any]]) -> str:
    """FSD 3.6.4 few-shot block."""
    if not hits:
        return ""
    parts = ["The following past SAP tasks in this client's system were completed successfully. Follow the same "
             "pattern where it fits; always re-check the live screen."]
    for i, h in enumerate(hits, 1):
        lines = [f"--- PAST EXAMPLE {i} (Similarity: {round(h['similarity'] * 100)}%) ---", f"Task: {h['task']}",
                 "Steps Taken:"]
        for j, s in enumerate(h["steps"][:30], 1):
            args = ", ".join(f"{k}={json.dumps(v, ensure_ascii=False)}" for k, v in s["args"].items())
            st = f"  [{s['status']}] {s['msg']}" if s.get("status") else ""
            lines.append(f"  Step {j} -> {s['tool']}({args}){st}")
        if h.get("summary"):
            lines.append(f"SAP Confirmed: {h['summary']}")
        lines.append("--- END ---")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)
