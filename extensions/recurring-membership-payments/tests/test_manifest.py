"""Tests reading apex_recurring_membership_payments/CANVAS_MANIFEST.json directly.

02-spec/SPEC.md criterion 16, the one manifest level acceptance criterion,
read from the file rather than from any handler, since nothing in this
plugin computes the manifest at runtime.
"""

import json
from pathlib import Path

_MANIFEST_PATH = (
    Path(__file__).resolve().parent.parent
    / "apex_recurring_membership_payments"
    / "CANVAS_MANIFEST.json"
)


def _manifest() -> dict:
    return json.loads(_MANIFEST_PATH.read_text())


def test_manifest_declares_portal_page_scopes_namespace_and_allowlist():
    """Covers criterion: AC16
    Covers scenario: AC16, the manifest declares the plugin's own portal page, three application scopes, the namespace and the redirect allowlist
    """
    manifest = _manifest()

    url_permissions = manifest["url_permissions"]
    assert len(url_permissions) == 1
    entry = url_permissions[0]
    assert sorted(entry["permissions"]) == sorted(["ALLOW_SAME_ORIGIN", "SCRIPTS"])
    assert entry["url"].endswith(
        "/plugin-io/api/apex_recurring_membership_payments/portal/"
    )

    assert manifest["custom_data"] == {
        "namespace": "apex__membership",
        "access": "read_write",
    }

    scopes = {application["scope"] for application in manifest["components"]["applications"]}
    assert scopes == {"portal_menu_item", "patient_specific", "provider_menu_item"}

    variables_by_name = {variable["name"]: variable for variable in manifest["variables"]}
    assert variables_by_name["REDIRECT_ALLOWLIST_INTERNAL"]["default"] == "/patient"
