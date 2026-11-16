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

`search_options` lists the global vocabulary. A building/floor combination may
offer only a subset of facility types. A `search_filter_unavailable` failure
provides known `requested_facility_types` and `available_facility_types` for
the current native scope. It does not establish room availability. Agents must
not silently broaden the user's filters to recover from this failure.

For one session per date, inspect `date_options` as well as the candidate page.
Each found option contains its retained `candidate_index`, `occurrence_index`
and legs, including dates whose candidates lie on later pages. A
`candidate_not_retained` status means the global retention cap omitted that
date's candidates; rerun only that date with the same constraints to retrieve
them. It does not mean no rooms matched. Per-date summaries do not remove
computation limits or make a partial common-date search complete.

`mode="any"` permits different rooms and times across dates. `allow_split`
separately permits a room change within a session and requires the user's
permission. Its travel gap adds time beyond the requested study duration.
Alternatives results expose source/probe `rooms_checked` and `complete` fields;
their counts describe the checked native scope before preference exclusions.

When checking clashes for a flexible plan, pass the full outer window to
`check_booking_clashes`. With complete account coverage, `clear_intervals`
subtracts all confirmed/pending overlaps, and `longest_clear_minutes` shows
whether a continuous session could fit. Both remain null when coverage is
uncertain. Clear account time does not establish room availability or final
booking eligibility.

# Arguments, pagination and account coverage

Unknown top-level MCP arguments are rejected before an operation starts.
Call `list_bookings({})` to start a fresh read, then page the retained result
with `wait_operation` or `operation_result` using `offset` and `limit` (1–50).
An invalid poll does not start or repeat the underlying operation.

Pagination distinguishes `returned_count` (rows in this page), `retained_total`
(rows kept by Sagasu), and `source_total` (the reported matching source total).
The older `returned_total` remains an alias of `retained_total` for existing
clients. Inspect source completeness and truncation rather than assuming the
displayed page covers every record.

My Bookings reads reject inherited account filters before claiming coverage.
The direct reader retries a one-shot inherited filter once; persistent filters
fall back to the browser in auto mode. The browser clears native filters and
verifies the reset before reading. An explicitly empty, unfiltered listing
remains valid. `listing_scope` identifies the native default My Bookings view
and, where verified, its Upcoming Bookings / General Bookings selections.
Complete coverage of that view does not establish historical account coverage.

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
The creator appears in `created_by` and its compatibility alias `booked_by`;
additional details include `use_type` and `booking_usage` where exposed.
`field_status.created_at` and `field_status.modified_at` explicitly identify
timestamp availability. Creator, Booked For, co-bookers and explicit workflow
confirmers remain separate roles. A last modifier or Confirmed status is never
evidence of who confirmed. Missing history remains unavailable, and free-text
workflow comments remain excluded.

For facility images, `receipt_available` and `photo_available` describe different
evidence. `room_details(include_image=true)` can provide a native details-page
screenshot through `operation_receipt` even when FBS exposes no photo. A page
screenshot is not a verified photograph of the exact room; scrolling panels may
be cropped, and an FBS photo may depict a facility type.
