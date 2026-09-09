"""The portal application, opening the membership page in the patient portal."""

from __future__ import annotations

from datetime import datetime, timezone

from canvas_sdk.effects import Effect
from canvas_sdk.effects.launch_modal import LaunchModalEffect
from canvas_sdk.handlers.application import Application

_CACHE_BUST = str(int(datetime.now(timezone.utc).timestamp()))
_PORTAL_PATH = "/plugin-io/api/recurring_membership_payments/portal/"


class MembershipPortalApp(Application):
    """Opens the membership page for the logged in patient, served by PortalAPI.

    02-spec/SPEC.md Behaviour step 1, the portal_menu_item application.
    """

    def on_open(self) -> Effect:
        """Return the launch effect for the portal membership page.

        The url carries the CANVAS_PUBLIC_URL prefix when that variable is
        set, because the manifest url_permissions entry names that same
        absolute address and the home app sandboxes the frame only when the
        frame's own url matches an entry there by prefix. With the variable
        empty the relative path is used and the frame renders with no
        sandbox attribute at all, exactly as the specification allows.
        """
        public_url = self.secrets.get("CANVAS_PUBLIC_URL") or ""
        return LaunchModalEffect(
            url=f"{public_url}{_PORTAL_PATH}?v={_CACHE_BUST}",
            target=LaunchModalEffect.TargetType.PAGE,
            title="Membership",
        ).apply()
