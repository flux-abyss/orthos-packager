"""Expert system verdict model.

A verdict is the output of one rule evaluation. Rules are kept in separate
modules (e.g. compat.py); this module only defines the shared data shape.

Design notes:
  - confidence is a float in [0.0, 1.0]. Rules must be conservative; a rule
    that cannot confirm all of its preconditions should return nothing rather
    than emit a low-confidence verdict.
  - evidence is a list of concrete strings (symbol names, file paths, log
    lines) that the human can verify independently.
  - suggested_action is plain English. It is intentionally not executable —
    the expert system describes; the human decides.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ExpertVerdict:
    """Result of a single expert rule evaluation.

    Fields
    ------
    rule_id:
        Stable identifier for the rule that produced this verdict.
        Use snake_case. Example: ``source_too_new_for_target_api``.
    category:
        Broad failure class. Currently: ``compatibility``, ``missing_dep``.
        Kept open-ended; new categories can be added without schema changes.
    confidence:
        Float in [0.0, 1.0]. Reflects how certain the rule is, given the
        evidence gathered. Rules should be conservative.
    summary:
        One sentence describing what the rule concluded.
    evidence:
        List of concrete observations that caused the rule to fire
        (symbol names, missing file paths, matching log lines, etc.).
        Must not be empty when confidence > 0.
    suggested_action:
        Plain English next step for the human maintainer.
        Not executable; describes intent only.
    """

    rule_id: str
    category: str
    confidence: float
    summary: str
    evidence: list[str] = field(default_factory=list)
    suggested_action: str = ""

    def as_dict(self) -> dict:
        """Serialize to a plain dict suitable for JSON output."""
        return {
            "rule_id": self.rule_id,
            "category": self.category,
            "confidence": self.confidence,
            "summary": self.summary,
            "evidence": self.evidence,
            "suggested_action": self.suggested_action,
        }
