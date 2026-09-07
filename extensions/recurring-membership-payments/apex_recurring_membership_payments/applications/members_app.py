"""The staff members application, opening the members page from the left menu."""

from __future__ import annotations

from datetime import datetime, timezone

from canvas_sdk.effects import Effect
from canvas_sdk.effects.launch_modal import LaunchModalEffect
from canvas_sdk.handlers.application import Application

_CACHE_BUST = str(int(datetime.now(timezone.utc).timestamp()))
_MEMBERS_PATH = "/plugin-io/api/apex_recurring_membership_payments/members/"


class MembersPageApp(Application):
    """Opens the members page as a full page, served by MembersAPI.

    02-spec/SPEC.md Behaviour step 44, the provider_menu_item application
    placed in the staff left menu, with no badge or count on the entry.
    """

    def on_open(self) -> Effect:
        """Return the launch effect for the members page.

        The url is relative and matches no url_permissions entry, so this
        frame renders unsandboxed, which a page of the plugin's own with no
        third party content needs nothing from.
        """
        return LaunchModalEffect(
            url=f"{_MEMBERS_PATH}?v={_CACHE_BUST}",
            target=LaunchModalEffect.TargetType.PAGE,
            title="Members",
        ).apply()
