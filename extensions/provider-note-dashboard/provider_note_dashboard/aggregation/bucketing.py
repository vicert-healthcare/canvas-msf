"""The bucketing contract.

This is the small contract the aggregation engine depends on. It places notes
into the calendar buckets of one view. Concrete strategies for day, week, and
month live in strategies.py and are wired to their view keys in registry.py. The
engine never names a concrete strategy, it is handed one and calls only the
methods described here.

In this dynamic language the contract is a documented base class, a set of
required methods plus the meaning of each, rather than a compiled interface. A
concrete strategy is any object that sets view_key and default_periods and
implements the three methods below with matching key formats.
"""


class BucketingStrategy:
    """Places notes into the calendar buckets of one view."""

    # The view key this strategy answers to, for example "day". The registry
    # maps this to the strategy instance.
    view_key = ""

    # The number of periods a view spans by default when the request does not
    # ask for a specific count.
    default_periods = 1

    def window(self, anchor, periods):
        """Return the half open datetime window the view spans.

        anchor is an arrow moment in the target zone, periods is how many of
        this view's periods to span ending at the anchor's period. Returns a
        pair of arrow moments, the inclusive start and the exclusive end, that
        bound the note query.
        """
        raise NotImplementedError

    def buckets(self, anchor, periods):
        """Return the ordered buckets spanning the window.

        A list of dicts, each with a key and a human label, in chronological
        order, so that periods with no notes still appear as empty buckets. The
        key must be produced the same way key_for produces it for a note.
        """
        raise NotImplementedError

    def key_for(self, moment):
        """Return the bucket key a localized moment falls in.

        moment is an arrow moment already converted to the target zone. The
        returned key must match one of the keys buckets produced for the same
        window.
        """
        raise NotImplementedError
