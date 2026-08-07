"""Tests for the access policy.

The policy is a pure function of the caller key and the raw allowlist, so it
tests directly with plain strings and no runtime.
"""

from provider_note_dashboard.access import Scope, resolve_scope


def test_empty_allowlist_opens_the_tool_to_everyone() -> None:
    scope = resolve_scope("anyone", "")
    assert scope.cross_provider is True
    assert scope.allows("some-other-provider") is True


def test_none_allowlist_is_treated_as_empty() -> None:
    scope = resolve_scope("anyone", None)
    assert scope.cross_provider is True


def test_listed_lead_sees_across_providers() -> None:
    scope = resolve_scope("lead-key", "lead-key, other-lead")
    assert scope.cross_provider is True
    assert scope.allows("a-different-provider") is True


def test_non_lead_is_scoped_to_self() -> None:
    scope = resolve_scope("me", "someone-else")
    assert scope.cross_provider is False
    assert scope.allows("me") is True
    assert scope.allows("someone-else") is False


def test_membership_ignores_dashes_and_case() -> None:
    # The header carries a dashless lowercase staff key while the allowlist may
    # be pasted with dashes and mixed case, so a lead is still recognized.
    scope = resolve_scope("abcdef01abcdef01", "ABCD-EF01-ABCD-EF01")
    assert scope.cross_provider is True


def test_self_scope_matches_a_dashed_provider_id() -> None:
    # The aggregation groups by a provider UUID that may carry dashes while the
    # caller key does not, so the self scope must match across that difference.
    scope = resolve_scope("abcdef01abcdef01", "another-lead")
    assert scope.cross_provider is False
    assert scope.allows("ABCD-EF01-ABCD-EF01") is True


def test_blank_allowlist_entries_are_ignored() -> None:
    # A trailing comma or stray spaces must not create an empty lead entry that
    # would otherwise match a blank caller key.
    scope = resolve_scope("me", " , , ")
    assert scope.cross_provider is True


def test_scope_direct_cross_provider_allows_all() -> None:
    scope = Scope(True, "me")
    assert scope.allows("anyone") is True


def test_scope_direct_self_denies_others() -> None:
    scope = Scope(False, "me")
    assert scope.allows("me") is True
    assert scope.allows("other") is False
