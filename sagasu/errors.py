"""Allowlisted recovery instructions; browser exception text never leaves the host."""

from .personal_options import SCRAPE_PARAM_CHOICES

REASONS = {
    "amendment_unavailable": (
        "The native FBS amendment could not be prepared. No CONFIRM action was attempted.",
        "Refresh amendment_options for the exact reference, review current permissions/fields and prepare a fresh amendment_review. An unreadable added-interval search is not evidence of no availability.",
        "amendment_options",
    ),
    "browser_screenshot_unavailable": (
        "No browser screenshot was captured. The page is unavailable, changed during capture, or contains authentication controls.",
        "If no FBS browser page exists, use session_status to load it without signing in, then retry browser_screenshot while idle. Authentication/MFA pages are excluded. The screenshot tool does not navigate or sign in.",
        None,
    ),
    "fbs_application_error": (
        "FBS returned an application error. The requested data could not be read; this is not an empty result.",
        "Check the same page in FBS and retry later. Local configuration or another login may not resolve an FBS application failure.",
        None,
    ),
    "cobooker_lookup_unavailable": (
        "FBS did not expose verified co-booker search results. No co-booker was selected and no booking was submitted.",
        "Retry search_cobookers without room/search inputs for automatic temporary lookup context, or supply a fresh free room/search together. Do not infer that a person is absent from an unreadable directory result.",
        "search_cobookers",
    ),
    "search_filter_unavailable": (
        "FBS could not match the requested facility types to its current search options. Availability was not determined.",
        "Inspect details.available_facility_types for the current building/floor scope; search_options lists the global vocabulary, not combinations supported everywhere. Revise only the requested constraints the user permits, then retry. Do not treat this failure as no available rooms or silently broaden the scope.",
        "search_options",
    ),
    "booking_details_unavailable": (
        "FBS did not expose verified booking details for this reference.",
        "Refresh list_bookings or list_tasks and inspect listing coverage. Use an exact returned reference_number and a permitted VIEW action before retrying booking_details.",
        "list_bookings",
    ),
    "configuration_required": (
        "Sagasu cannot run FBS operations until its local configuration is corrected.",
        "Read configuration_status for the failing checks and exact local fixes. Correct them on the host, then reconnect Sagasu before retrying. Never send credentials in chat.",
        "configuration_status",
    ),
    "authentication_context_invalid": (
        "Microsoft rejected an incomplete or expired sign-in request context.",
        "Start a fresh Sagasu login from FBS. Do not reuse or edit the Microsoft resume URL. If this repeats, inspect the local sign-in page.",
        "login",
    ),
    "authentication_failed": (
        "Microsoft sign-in could not complete; session expiry alone does not explain this failure.",
        "Start a fresh Sagasu login. If it fails again, inspect the local Microsoft error and run sagasu doctor to check reachability.",
        "login",
    ),
    "authentication_interrupted": (
        "The browser closed before Microsoft sign-in completed.",
        "Run login again and keep its browser open until Sagasu verifies authentication.",
        "login",
    ),
    "api_read_unavailable": (
        "Sagasu could not verify the direct FBS read response.",
        "Run the read-only backend probe after login to check compatibility. Use SAGASU_READ_BACKEND=auto for browser fallback or browser to force browser reads.",
        None,
    ),
    "missing_email": (
        "Headless sign-in needs SMU_LOGIN_EMAIL.",
        "Set SMU_LOGIN_EMAIL privately in the host environment, then restart the idle shared host and reconnect Sagasu to load the change before retrying login.",
        "configuration_status",
    ),
    "missing_password": (
        "Headless password sign-in needs SMU_LOGIN_PASSWORD.",
        "Configure SMU_LOGIN_PASSWORD privately on the host, then restart the idle shared host and reconnect Sagasu to load the change before retrying login. A visible browser can also be used for sign-in. Never send passwords in chat.",
        "configuration_status",
    ),
    "session_expired": (
        "The saved FBS session needs renewal.",
        "Run login and complete Microsoft sign-in and MFA.",
        "login",
    ),
    "sign_in_required_on_host": (
        "SMU sign-in is needed on the Sagasu server. A remote connection cannot sign in or approve MFA.",
        "Tell the user that the server's SMU sign-in has lapsed and that they must sign in on the server themselves. Do not retry until they confirm; further remote FBS requests are refused for a few minutes after this failure.",
        None,
    ),
    "session_renewal_pending": (
        "The saved FBS session had expired and did not renew itself in time. Nothing was read or changed.",
        "Retry the same request once. If it fails again, tell the user that SMU sign-in may be needed on the server; a remote connection cannot sign in.",
        None,
    ),
    "sign_in_rejected": (
        "Microsoft did not accept sign-in.",
        "Check the account privately on the host before trying again.",
        "login",
    ),
    "interactive_verification_required": (
        "Microsoft requires an interactive verification step.",
        "Complete sign-in with a visible browser on the host, then resume headless use.",
        "login",
    ),
    "authentication_timeout": (
        "Microsoft sign-in did not finish before the deadline.",
        "Retry login when ready to approve MFA.",
        "login",
    ),
    "outside_booking_horizon": (
        "The requested room/time is outside its FBS booking horizon.",
        "Inspect the checked-room counts. For future dates, retry closer to the date or review other facilities; no opening date has been inferred.",
        None,
    ),
    "room_unavailable": (
        "The requested room is no longer available for the full interval.",
        "Refresh availability and prepare a new review for an available room.",
        "search_rooms",
    ),
    "layout_changed": (
        "FBS returned a page Sagasu could not verify.",
        "Check compatibility using the host's read-only smoke check; do not infer availability from this failure.",
        None,
    ),
}


def public_failure(error):
    reason = getattr(error, "reason", None)
    if reason not in REASONS:
        return {}
    message, action, tool = REASONS[reason]
    details = getattr(error, "details", {})
    # Only small counts/flags and known catalogue labels cross the boundary.
    safe = {
        key: value
        for key, value in details.items()
        if key in {"checked_rooms", "available_total", "all_checked_rooms_affected"}
        and type(value) in {int, bool}
        and (type(value) is bool or 0 <= value <= 10000)
    }
    if reason == "search_filter_unavailable":
        for key in ("requested_facility_types", "available_facility_types"):
            labels = details.get(key)
            if isinstance(labels, (list, tuple)):
                safe[key] = [
                    label
                    for label in SCRAPE_PARAM_CHOICES["facility_type"]
                    if label in labels
                ]
    return {
        "reason": reason,
        "message": message,
        "next_action": action,
        "next_tool": tool,
        "details": safe,
    }
