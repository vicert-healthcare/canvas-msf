"""The access policy.

The second seam of the plugin. A single pure authorization step decides who may
see across providers and who is scoped to their own output. It is a pure
function of two inputs, the calling staff key and the raw allowlist value, and
it returns a scope object that answers one question, may this caller see a given
provider. It names neither the manifest variable nor the request, the handler
reads those from the one composition root and hands them in. The routes depend
only on the returned scope, so this module is the only place that reasons about
who is a lead.

The caller key arrives from the canvas-logged-in-user-id header as a thirty two
character hex string, the staff key, while the aggregation identifies a provider
by the provider UUID string, which may carry dash separators. So all comparison
runs through _normalize, which strips dashes and lowercases, and a dash only or
case only difference can never break the match. This rests on the staff key and
the provider UUID being the same underlying identifier, the Canvas convention,
which is the one thing to confirm on a live instance.
"""


def _normalize(identifier):
    """Reduce an identifier to a comparison form, no dashes and lower case."""
    return (identifier or "").replace("-", "").strip().lower()


class Scope:
    """The providers a caller is allowed to see.

    A cross provider scope allows every provider. A self scope allows only the
    provider whose normalized id matches the normalized caller key. The
    normalization lives here so no route has to repeat it.
    """

    def __init__(self, cross_provider, caller_key):
        self._cross_provider = cross_provider
        self._caller_key = _normalize(caller_key)

    @property
    def cross_provider(self):
        """Whether this scope sees across all providers."""
        return self._cross_provider

    def allows(self, provider_id):
        """Whether this scope may see the given provider."""
        if self._cross_provider:
            return True
        return _normalize(provider_id) == self._caller_key


def resolve_scope(caller_key, allowlist_raw):
    """Resolve the scope a caller is allowed from the allowlist.

    caller_key is the staff key from the session header, allowlist_raw is the
    comma separated allowlist value from configuration. An empty allowlist opens
    the tool, everyone sees everyone. A caller in the allowlist sees across
    providers. Anyone else is scoped to their own output.
    """
    leads = [
        _normalize(entry)
        for entry in (allowlist_raw or "").split(",")
        if entry.strip()
    ]

    if not leads:
        return Scope(True, caller_key)

    if _normalize(caller_key) in leads:
        return Scope(True, caller_key)

    return Scope(False, caller_key)
