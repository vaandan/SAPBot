"""Embedding functions for the RAG store (ADR D4).

Primary: all-MiniLM-L6-v2 via ONNX Runtime (Chroma's built-in ONNXMiniLM_L6_V2, 384-dim, CPU, no PyTorch).
The model files are fetched once (installer pre-fetches into <SAPBOT_HOME>/models/onnx); if they are unavailable
(offline laptop, blocked proxy) SAPBot falls back to a deterministic hashing embedder so RAG keeps working -
lower quality, but never breaks a run. The active embedder name is part of the collection name, so switching
embedders re-indexes cleanly.
"""
from __future__ import annotations

import hashlib
import logging
import math
import re
from typing import Any

import numpy as np

from .. import paths

log = logging.getLogger(__name__)
DIM = 384
_TOKEN = re.compile(r"[A-Za-z0-9_\-/]+")


class HashingEmbedder:
    """Feature-hashing of words + word bigrams + char trigrams -> L2-normalised 384-dim vector."""
    name = "hash384"

    def __init__(self) -> None:
        pass

    @staticmethod
    def _features(text: str) -> list[str]:
        words = [w.lower() for w in _TOKEN.findall(text)]
        feats = list(words)
        feats += [f"{a}_{b}" for a, b in zip(words, words[1:])]
        for w in words:
            ww = f"#{w}#"
            feats += [ww[i:i + 3] for i in range(len(ww) - 2)]
        return feats

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for t in texts:
            v = np.zeros(DIM, dtype=np.float32)
            for f in self._features(t):
                h = int.from_bytes(hashlib.blake2b(f.encode("utf-8"), digest_size=8).digest(), "little")
                v[h % DIM] += 1.0 if (h >> 63) & 1 else -1.0
            n = float(np.linalg.norm(v)) or 1.0
            out.append((v / n).tolist())
        return out



class OnnxMiniLM:
    """all-MiniLM-L6-v2 through Chroma's ONNX runtime wrapper (no PyTorch)."""
    name = "minilm384"

    def __init__(self) -> None:
        from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2
        models = paths.home() / "models" / "onnx"
        if models.exists():
            ONNXMiniLM_L6_V2.DOWNLOAD_PATH = models  # type: ignore[attr-defined]
        self._ef = ONNXMiniLM_L6_V2()
        self._ef(["probe"])  # forces download / load now

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [np.asarray(v, dtype=np.float32).tolist() for v in self._ef(list(texts))]


_cached: Any = None


def get_embedder(prefer_onnx: bool = True) -> Any:
    """Returns an embedder with .name and .embed(texts) -> list[list[float]]."""
    global _cached
    if _cached is not None:
        return _cached
    if prefer_onnx:
        try:
            _cached = OnnxMiniLM()
            return _cached
        except Exception as e:  # noqa: BLE001
            log.warning("ONNX MiniLM embedder unavailable (%s) - using hashing fallback", str(e)[:200])
    _cached = HashingEmbedder()
    return _cached


def cosine(a: list[float], b: list[float]) -> float:
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(x * x for x in b)) or 1.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)
