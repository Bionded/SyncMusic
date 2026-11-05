# SPDX-License-Identifier: GPL-3.0-or-later
"""Rule engine: nsp-compatible criteria, evaluation, text formats, templates."""

from syncmusic.core.rules.evaluator import EvalContext, evaluate_rule, evaluate_ruleset
from syncmusic.core.rules.parser import RuleError, parse_criteria
from syncmusic.core.rules.text import (
    ValidationResult,
    rule_from_nsp,
    rule_from_text,
    rule_to_json,
    rule_to_nsp,
    rule_to_yaml,
    ruleset_from_text,
    validate_rule,
    validate_ruleset,
)

__all__ = [
    "EvalContext",
    "RuleError",
    "ValidationResult",
    "evaluate_rule",
    "evaluate_ruleset",
    "parse_criteria",
    "rule_from_nsp",
    "rule_from_text",
    "rule_to_json",
    "rule_to_nsp",
    "rule_to_yaml",
    "ruleset_from_text",
    "validate_rule",
    "validate_ruleset",
]
