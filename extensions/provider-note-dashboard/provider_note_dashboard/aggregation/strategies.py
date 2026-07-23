"""The three concrete bucketing strategies.

Each strategy is a self contained policy that knows only its own calendar
period. They share nothing but the contract in bucketing.py. Adding a fourth
grouping is a new class here plus one line in registry.py, with no change to the
engine. All date math runs through arrow, which the SDK uses for note date
filtering, so it is available in the sandbox and handles zones and week
boundaries without the blocked standard library imports.
"""

import arrow

from provider_note_dashboard.aggregation.bucketing import BucketingStrategy


class DayBucketing(BucketingStrategy):
    """Buckets notes by calendar day."""

    view_key = "day"
    default_periods = 14

    def window(self, anchor, periods):
        start = anchor.floor("day").shift(days=-(periods - 1))
        end = anchor.floor("day").shift(days=+1)
        return start, end

    def buckets(self, anchor, periods):
        moment = anchor.floor("day").shift(days=-(periods - 1))
        out = []
        for _ in range(periods):
            out.append({"key": moment.format("YYYY-MM-DD"), "label": moment.format("MMM D")})
            moment = moment.shift(days=+1)
        return out

    def key_for(self, moment):
        return moment.format("YYYY-MM-DD")


class WeekBucketing(BucketingStrategy):
    """Buckets notes by ISO week, weeks starting Monday."""

    view_key = "week"
    default_periods = 8

    def window(self, anchor, periods):
        start = anchor.floor("week").shift(weeks=-(periods - 1))
        end = anchor.floor("week").shift(weeks=+1)
        return start, end

    def buckets(self, anchor, periods):
        moment = anchor.floor("week").shift(weeks=-(periods - 1))
        out = []
        for _ in range(periods):
            out.append({"key": self.key_for(moment), "label": "Week of " + moment.format("MMM D")})
            moment = moment.shift(weeks=+1)
        return out

    def key_for(self, moment):
        iso = moment.isocalendar()
        return "{0}-W{1:02d}".format(iso[0], iso[1])


class MonthBucketing(BucketingStrategy):
    """Buckets notes by calendar month."""

    view_key = "month"
    default_periods = 6

    def window(self, anchor, periods):
        start = anchor.floor("month").shift(months=-(periods - 1))
        end = anchor.floor("month").shift(months=+1)
        return start, end

    def buckets(self, anchor, periods):
        moment = anchor.floor("month").shift(months=-(periods - 1))
        out = []
        for _ in range(periods):
            out.append({"key": moment.format("YYYY-MM"), "label": moment.format("MMM YYYY")})
            moment = moment.shift(months=+1)
        return out

    def key_for(self, moment):
        return moment.format("YYYY-MM")
