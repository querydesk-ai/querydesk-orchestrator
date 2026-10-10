"""
The labelled eval set (PRD "Evaluation and success metrics").

60 rows: 30 action requests (3 per category), 20 informational questions
(2 per category) and 10 out-of-scope or ambiguous queries. Each row carries
the decision the system *should* make, so routing accuracy can be measured
and thresholds calibrated against it.
"""

import json
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, model_validator

from querydesk.core.config import BASE_DIR

EVAL_SET_PATH = BASE_DIR / "eval" / "eval_set.jsonl"


class Kind(StrEnum):
    ACTION = "action"
    INFORMATIONAL = "informational"
    OUT_OF_SCOPE = "out_of_scope"


class Decision(StrEnum):
    """The decision-layer outcomes (PRD Day 4) an eval row can expect."""

    ANSWER = "ANSWER"
    ANSWER_WITH_DISCLAIMER = "ANSWER_WITH_DISCLAIMER"
    COLLECT_DETAILS = "COLLECT_DETAILS"
    RAISE_SR = "RAISE_SR"
    ESCALATE = "ESCALATE"
    STATUS_LOOKUP = "STATUS_LOOKUP"


class EvalRow(BaseModel):
    id: str
    query: str
    kind: Kind
    expected_decision: Decision
    category_id: str | None = None
    # Action rows only: are all of the category's required fields in the message?
    fields_present: bool | None = None
    # Informational rows only: the KB file that should be cited.
    expected_source: str | None = None
    # The answer hinges on an exact token (error code, hostname) — BM25's job.
    lexical: bool = False

    @model_validator(mode="after")
    def _consistent(self) -> "EvalRow":
        if self.kind is Kind.OUT_OF_SCOPE:
            if self.category_id is not None:
                raise ValueError(f"{self.id}: out-of-scope rows have no category")
            if self.expected_decision is not Decision.ESCALATE:
                raise ValueError(f"{self.id}: out-of-scope rows must expect ESCALATE")
        else:
            if self.category_id is None:
                raise ValueError(f"{self.id}: {self.kind} rows need a category_id")

        if self.kind is Kind.ACTION:
            if self.fields_present is None:
                raise ValueError(f"{self.id}: action rows need fields_present")
            expected = Decision.RAISE_SR if self.fields_present else Decision.COLLECT_DETAILS
            if self.expected_decision is not expected:
                raise ValueError(f"{self.id}: fields_present implies {expected}")

        if self.kind is Kind.INFORMATIONAL and not self.expected_source:
            raise ValueError(f"{self.id}: informational rows need expected_source")
        return self


def load_eval_set(path: Path = EVAL_SET_PATH) -> list[EvalRow]:
    rows = []
    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.strip():
            try:
                rows.append(EvalRow.model_validate(json.loads(line)))
            except ValueError as e:
                raise ValueError(f"{path.name}:{line_no}: {e}") from e
    return rows
