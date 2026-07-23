"""Tests for the shared note selection.

The window filtering runs against the database, so it is not exercised here. What
is exercised is the excluded state resolver, which must map the three configured
names to real members of the SDK note state enum and skip any name a future build
no longer carries. This is the safety net that keeps a renamed state from
crashing the tool.
"""

from canvas_sdk.v1.data.note import NoteStates

from provider_note_dashboard.aggregation import notes


def test_the_three_configured_names_resolve_to_real_enum_members() -> None:
    resolved = notes._excluded_states()
    assert set(resolved) == {
        NoteStates.DELETED,
        NoteStates.CANCELLED,
        NoteStates.NOSHOW,
    }


def test_an_unknown_state_name_is_skipped_rather_than_raising(monkeypatch) -> None:
    monkeypatch.setattr(
        notes, "_EXCLUDED_STATE_NAMES", ("DELETED", "NOT_A_REAL_STATE")
    )
    resolved = notes._excluded_states()
    assert resolved == [NoteStates.DELETED]
