"""
The category catalog: data/categories.json, read once.

categories.json is the single source of truth for the 10 categories, shared
with the ticketing service (CLAUDE.md invariant #3). This module is the only
place app code reads it, so no category id, required field or approval rule
is ever hardcoded elsewhere — callers ask the catalog.
"""

import json
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel

from querydesk.core.config import settings


class Approval(StrEnum):
    NONE = "none"
    ALWAYS = "always"
    # Off by default; a decision rule (data/decision_rules.json) may switch it on.
    CONDITIONAL = "conditional"


class Category(BaseModel):
    id: str
    name: str
    domain: str
    approval: Approval
    required_fields: list[str]
    default_path: str
    description: str


class UnknownCategoryError(KeyError):
    """A category id that categories.json doesn't define."""


@lru_cache(maxsize=4)
def _load(path: Path) -> dict[str, Category]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    categories = [Category.model_validate(c) for c in raw["categories"]]
    return {c.id: c for c in categories}


def all_categories() -> dict[str, Category]:
    return _load(settings.categories_file)


def get(category_id: str) -> Category:
    try:
        return all_categories()[category_id]
    except KeyError as e:
        raise UnknownCategoryError(category_id) from e
