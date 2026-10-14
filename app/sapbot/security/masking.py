"""Data masking for workspaces with data_policy 'masked' (ADR A15) and for anything written to trajectories
(project rule: never log passwords, tokens or personal data)."""
from __future__ import annotations

import re
from typing import Any

PATTERNS = [
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "<EMAIL>"),
    (re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"), "<IBAN>"),
    (re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), "<PAN>"),                       # Indian PAN
    (re.compile(r"(?<!\d)(?:\+?\d[\d \-]{9,14}\d)(?!\d)"), "<PHONE>"),
    (re.compile(r"""(?i)\b(password|passwort|pwd|token|api[_-]?key|secret)\b\s*[:=]\s*(?:"[^"]*"|'[^']*'|\S+)"""), r"\1=<SECRET>"),
]
SECRET_FIELD = re.compile(r"(?i)(pass|pwd|token|secret|bcode)")


def mask_text(text: str) -> str:
    if not text:
        return text
    for rx, rep in PATTERNS:
        text = rx.sub(rep, text)
    return text


def _field_hit(key: str, fields: list[str]) -> bool:
    k = key.upper()
    return any(f.upper() in k for f in fields)


def mask_obj(obj: Any, fields: list[str] | None = None, deep_text: bool = True) -> Any:
    """Mask a nested structure. Values whose key/id contains a sensitive SAP field name are replaced;
    password-like fields are always replaced; free text is regex-masked when deep_text."""
    fields = fields or []
    if isinstance(obj, dict):
        hint = f"{obj.get('id', '')} {obj.get('name', '')}"
        out = {}
        for k, v in obj.items():
            ks = str(k)
            scalar = isinstance(v, (str, int, float)) and not isinstance(v, bool)
            is_val = ks in ("value", "text")
            if scalar and v != "" and (SECRET_FIELD.search(ks) or (is_val and SECRET_FIELD.search(hint))):
                out[k] = "<SECRET>"
            elif scalar and v != "" and fields and (_field_hit(ks, fields) or (is_val and _field_hit(hint, fields))):
                out[k] = "<MASKED>"
            else:
                out[k] = mask_obj(v, fields, deep_text)
        return out
    if isinstance(obj, list):
        return [mask_obj(x, fields, deep_text) for x in obj]
    if isinstance(obj, str) and deep_text:
        return mask_text(obj)
    return obj


def scrub_secrets(obj: Any) -> Any:
    """Always applied before logging: only removes secrets (no personal-data masking)."""
    return mask_obj(obj, fields=[], deep_text=False) if not isinstance(obj, str) else \
        PATTERNS[-1][0].sub(PATTERNS[-1][1], obj)
