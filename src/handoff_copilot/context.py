"""What a connector hands the copilot for one screen.

Records keep Harvest v3's documented field names, because Greenhouse is the first
connector. Another ATS maps its records into the same names.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ScreenContext:
    application: dict
    candidate: dict
    screen: dict
    panel: list[dict]
    interviews: list[dict]
    notes: list[dict]
    transcript: dict | None
    ai_opt_out: bool | None
    fields_seen: dict[str, list[str]] = field(default_factory=dict)

    @property
    def candidate_names(self) -> list[str]:
        c = self.candidate
        first, last, preferred = c.get("first_name"), c.get("last_name"), c.get("preferred_name")
        full = " ".join(p for p in (first, last) if p)
        alt = " ".join(p for p in (preferred, last) if p)
        return [n for n in (full, alt, first, preferred) if n]
