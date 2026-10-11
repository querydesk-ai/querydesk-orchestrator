"""The catalog reads categories.json — the real file, no network."""

import json

import pytest

from querydesk.core.config import settings
from querydesk.services import catalog


def test_loads_every_category_from_categories_json():
    raw = json.loads(settings.categories_file.read_text(encoding="utf-8"))
    assert set(catalog.all_categories()) == {c["id"] for c in raw["categories"]}


def test_get_returns_typed_fields():
    restart = catalog.get("it_application_restart")
    assert restart.approval is catalog.Approval.CONDITIONAL
    assert restart.required_fields == ["application", "environment"]


def test_unknown_category_raises():
    with pytest.raises(catalog.UnknownCategoryError):
        catalog.get("not_a_category")


def test_every_approval_value_is_known():
    """A new approval value in categories.json must fail loudly, not be ignored."""
    raw = json.loads(settings.categories_file.read_text(encoding="utf-8"))
    assert set(raw["approval_values"]) == {a.value for a in catalog.Approval}
