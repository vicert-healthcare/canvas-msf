"""Tests for the per note detail builder.

The builder reads the same selection the engine counts, then shapes one row per
note from its patient, note type, active billing line items, and reason for
visit command. The selection is replaced with stand in notes carrying stand in
line items and commands, so the shaping, the active only code filter, and the
reason for visit fallbacks are exercised without a database.
"""

import datetime
from types import SimpleNamespace

from canvas_sdk.v1.data.billing import BillingLineItemStatus

from provider_note_dashboard.aggregation import detail

ZONE = "UTC"


def _line_item(status, cpt, description):
    return SimpleNamespace(status=status, cpt=cpt, description=description)


def _command(schema_key, data):
    return SimpleNamespace(schema_key=schema_key, data=data)


def _note(line_items, commands, patient=None, note_type=None):
    return SimpleNamespace(
        id="note-1",
        patient=patient,
        note_type_version=note_type,
        datetime_of_service=datetime.datetime(2026, 7, 22, 9, 0, 0),
        billing_line_items=SimpleNamespace(all=lambda: line_items),
        commands=SimpleNamespace(all=lambda: commands),
    )


def _patch_selection(mocker, notes):
    """Replace select_notes so the whole read chain returns the given notes."""
    queryset = SimpleNamespace()
    queryset.filter = lambda *a, **k: queryset
    queryset.select_related = lambda *a, **k: queryset
    queryset.prefetch_related = lambda *a, **k: queryset
    queryset.order_by = lambda *a, **k: notes
    mocker.patch.object(detail, "select_notes", return_value=queryset)


def test_only_active_line_items_become_codes(mocker) -> None:
    note = _note(
        line_items=[
            _line_item(BillingLineItemStatus.ACTIVE, "99213", "Office visit"),
            _line_item("removed", "99999", "Dropped code"),
        ],
        commands=[],
    )
    _patch_selection(mocker, [note])

    rows = detail.build_detail("a", None, None, ZONE)

    assert rows[0]["codes"] == [{"cpt": "99213", "description": "Office visit"}]


def test_reason_for_visit_prefers_the_structured_coding_text(mocker) -> None:
    note = _note(
        line_items=[],
        commands=[
            _command("reasonForVisit", {"coding": {"text": "Chest pain"}, "comment": "typed"})
        ],
    )
    _patch_selection(mocker, [note])

    rows = detail.build_detail("a", None, None, ZONE)

    assert rows[0]["reason_for_visit"] == "Chest pain"


def test_reason_for_visit_falls_back_to_the_free_text_comment(mocker) -> None:
    note = _note(
        line_items=[],
        commands=[_command("reasonForVisit", {"comment": "Follow up"})],
    )
    _patch_selection(mocker, [note])

    rows = detail.build_detail("a", None, None, ZONE)

    assert rows[0]["reason_for_visit"] == "Follow up"


def test_reason_for_visit_is_blank_when_no_reason_command_is_present(mocker) -> None:
    note = _note(
        line_items=[],
        commands=[_command("somethingElse", {"comment": "ignored"})],
    )
    _patch_selection(mocker, [note])

    rows = detail.build_detail("a", None, None, ZONE)

    assert rows[0]["reason_for_visit"] == ""


def test_missing_patient_and_note_type_degrade_to_blanks(mocker) -> None:
    note = _note(line_items=[], commands=[], patient=None, note_type=None)
    _patch_selection(mocker, [note])

    rows = detail.build_detail("a", None, None, ZONE)

    assert rows[0]["patient_id"] == ""
    assert rows[0]["patient_name"] == ""
    assert rows[0]["note_type"] == ""


def test_row_carries_the_patient_note_type_and_localized_time(mocker) -> None:
    patient = SimpleNamespace(id="p1", full_name="Jane Roe")
    note_type = SimpleNamespace(name="Office Visit")
    note = _note(line_items=[], commands=[], patient=patient, note_type=note_type)
    _patch_selection(mocker, [note])

    rows = detail.build_detail("a", None, None, ZONE)

    row = rows[0]
    assert row["note_id"] == "note-1"
    assert row["patient_id"] == "p1"
    assert row["patient_name"] == "Jane Roe"
    assert row["note_type"] == "Office Visit"
    assert row["datetime_of_service"].startswith("2026-07-22T09:00:00")
