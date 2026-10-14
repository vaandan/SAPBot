"""Date/number formats (ADR B4). Firm standard is English logon + DD.MM.YYYY. `probe` detects the user's real
profile; values the bot types are converted from the standard to that profile (SU3 is never changed)."""
from __future__ import annotations

import datetime as dt
import re

FORMATS = {
    "DD.MM.YYYY": "%d.%m.%Y",
    "MM/DD/YYYY": "%m/%d/%Y",
    "MM-DD-YYYY": "%m-%d-%Y",
    "YYYY.MM.DD": "%Y.%m.%d",
    "YYYY/MM/DD": "%Y/%m/%d",
    "YYYY-MM-DD": "%Y-%m-%d",
}
STD_DATE = re.compile(r"^\d{2}\.\d{2}\.\d{4}$")
STD_RANGE_SEP = ".."


def detect_date_format(values: list[str], today: dt.date | None = None) -> str:
    """Return the format whose rendering of today's date appears among the given field values ('' if none)."""
    today = today or dt.date.today()
    vals = {v.strip() for v in values if v and v.strip()}
    for name, fmt in FORMATS.items():
        if today.strftime(fmt) in vals:
            # DD.MM.YYYY vs YYYY.MM.DD can't collide; MM/DD vs DD/MM ambiguity resolved by today's day > 12 if needed
            return name
    return ""


def convert_std_date(value: str, target: str) -> str:
    """Convert a firm-standard DD.MM.YYYY value to the user's format. Other values pass through unchanged."""
    if not target or target == "DD.MM.YYYY" or target not in FORMATS:
        return value
    if STD_DATE.match(value.strip()):
        try:
            return dt.datetime.strptime(value.strip(), "%d.%m.%Y").strftime(FORMATS[target])
        except ValueError:
            return value
    return value


def to_std(value: str, source: str) -> str:
    """Convert a value read from SAP in the user's format to DD.MM.YYYY (for logs)."""
    if not source or source == "DD.MM.YYYY" or source not in FORMATS:
        return value
    try:
        return dt.datetime.strptime(value.strip(), FORMATS[source]).strftime("%d.%m.%Y")
    except ValueError:
        return value
