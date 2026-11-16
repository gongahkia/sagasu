# Search windows and effective scope

`duration_hrs` describes the outer search window, in half-hour increments from
0.5 to 24 hours. The start and end must remain on one date. For `plan_rooms`,
`duration_minutes` separately describes the continuous slot needed within that
window. For example, `start_time="10:00"`, `duration_hrs=6`, and
`duration_minutes=120` searches for two-hour slots between 10:00 and 16:00.
The planner reads each date once; it does not require the agent to split that
window into overlapping searches. These input bounds do not establish native
booking-duration limits, quota rules or final FBS eligibility.

Use `config` for an explicit search configuration or `preferences` for a
temporary patch over saved/default search settings. Neither saves preferences.
Omitted filters retain saved/default values; `null` restores the built-in
default for that filter. An explicit empty list removes the restriction. For
example, `floors=[]` requests no floor restriction, whereas `floors=null`
restores the default floors. Exact facility-type labels remain distinct;
selecting one label does not imply that similarly named variants were checked.

Planning results include `effective_scope`: dates, window, filters, excluded
rooms, and each filter's source (`explicit_config`, `one_off_preferences`,
`saved_preferences`, or `built_in_default`). Its notice makes inherited floor
and facility-type restrictions explicit. Agents should disclose those
restrictions in the answer. `complete` and `ranking_complete` apply only to the
effective scope and never establish coverage of an entire building or campus.
An explicit unrestricted filter also does not override partial FBS coverage:
inspect each probe, booking-horizon evidence and computation limits.

# Upcoming bookings and complete details

`list_bookings` adds SGT `starts_at`, `ends_at` and `temporal_status` to each
readable row. Its `timeline` is evaluated against the request-time clock over
the full retained listing before MCP pagination. `next_upcoming_reference`
selects the earliest non-cancelled booking starting strictly after `as_of`;
`ongoing_references` separately identifies bookings that have started but not
ended. A booking at its exact start is ongoing, and at its exact end is past.
Confirmed and pending records retain their actual FBS status; a pending record
does not become confirmed because it is selected as next.

Only use a definitive next/none when `timeline.complete=true`. Partial coverage,
duplicate references, unreadable active dates or unknown statuses produce
`next_upcoming_status="unknown"`, not a false absence. Cancelled rows cannot
precede the next eligible booking. Time classification of a cached operation
updates when requested, but its FBS status remains a snapshot of the original
read; fetch a new listing when fresh FBS evidence is required.

Use the selected exact reference with `booking_details`. For a complete-detail
request, report all exposed core information and approved additional metadata,
including creation time, last-modified time and cancellation permission.
`field_status.created_at` and `field_status.modified_at` explicitly identify
timestamp availability. Creator, Booked For, co-bookers and explicit workflow
confirmers remain separate roles. A last modifier or Confirmed status is never
evidence of who confirmed. Missing history remains unavailable, and free-text
workflow comments remain excluded.
