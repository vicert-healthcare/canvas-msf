"""Tests reading recurring_membership_payments/CANVAS_MANIFEST.json directly.

02-spec/SPEC.md criterion 16, the one manifest level acceptance criterion,
read from the file rather than from any handler, since nothing in this
plugin computes the manifest at runtime.
"""

import json
from pathlib import Path

_MANIFEST_PATH = (
    Path(__file__).resolve().parent.parent
    / "recurring_membership_payments"
    / "CANVAS_MANIFEST.json"
)


def _manifest() -> dict:
    return json.loads(_MANIFEST_PATH.read_text())


def test_manifest_declares_portal_page_scopes_namespace_and_no_redirect_allowlist():
    """Covers criterion: AC16
    Covers scenario: AC16, the manifest declares the plugin's own portal page, three application scopes and the namespace, and carries no redirect allowlist
    """
    manifest = _manifest()

    # One entry per instance the plugin is installed on rather than exactly
    # one. The home app matches an entry against the frame's own absolute URL
    # by prefix, so a single entry sandboxes the portal frame on one host and
    # leaves it unsandboxed everywhere else. What the criterion guarantees is
    # that every entry is this plugin's own portal page and nothing else.
    url_permissions = manifest["url_permissions"]
    assert url_permissions
    for entry in url_permissions:
        assert sorted(entry["permissions"]) == sorted(["ALLOW_SAME_ORIGIN", "SCRIPTS"])
        assert entry["url"].endswith(
            "/plugin-io/api/recurring_membership_payments/portal/"
        )

    assert manifest["custom_data"] == {
        "namespace": "membership__data",
        "access": "read_write",
    }

    scopes = {application["scope"] for application in manifest["components"]["applications"]}
    assert scopes == {"portal_menu_item", "patient_specific", "provider_menu_item"}

    variables_by_name = {variable["name"]: variable for variable in manifest["variables"]}
    assert "REDIRECT_ALLOWLIST_INTERNAL" not in variables_by_name
