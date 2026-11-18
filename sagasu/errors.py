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
    # Sign-in with the person's saved SMU email and password (saved_sign_in.py).
    "sign_in_setup_needed": (
        "Sagasu needs the user's SMU sign-in before it can use FBS. Nothing was read or changed.",
        'Give the user the sign_in_url and say: "Open this link to save your SMU email and password for Sagasu. It works once, for 15 minutes. Approve the sign-in on your phone there, then tell me here." Do not open the link yourself, and never ask for or accept a password in chat. When they say they are done, run their request again.',
        None,
    ),
    "saved_password_rejected": (
        "Microsoft did not accept the saved SMU password; it may have been changed. Sagasu deleted it and did not try again. Nothing was read or changed.",
        "Tell the user their saved SMU password no longer works and give them the sign_in_url to save the new one. Do not open the link yourself, and never ask for the password in chat. When they say they are done, run their request again.",
        None,
    ),
    "sign_in_not_approved": (
        "The sign-in request on the user's phone was denied or not answered in time. Nothing was read or changed.",
        "Tell the user the Microsoft Authenticator request was not approved. Ask if they want a new one; if yes, call login and give them the new number, and after it succeeds run their request again. If login is not in your tool list, give them the sign_in_url instead (do not open it yourself). Sagasu will not send another request to their phone until they ask.",
        "login",
    ),
    "sign_in_unfinished": (
        "The SMU sign-in stopped before the password was used (for example, Microsoft showed an error page). Nothing was read or changed.",
        "Tell the user. If they want to try again, call login and give them the number it returns; after it succeeds run their request again. If login is not in your tool list, give them the sign_in_url instead (do not open it yourself).",
        "login",
    ),
    "sign_in_needs_page": (
        "Microsoft asked for something Sagasu cannot do from chat, or did not clearly accept the saved password. To protect the account, Sagasu will not try again from chat. Nothing was read or changed.",
        "Give the user the sign_in_url and say they can check their saved sign-in there. Do not open the link yourself. When they say they are done, run their request again.",
        None,
    ),
    "sign_in_blocked": (
        "Microsoft has locked or blocked sign-in to this SMU account for now. Sagasu will not try again. Nothing was read or changed.",
        "Tell the user Microsoft has blocked sign-in to their SMU account; they should wait, or contact SMU IT if it continues. Do not call login. Once Microsoft has cleared it, they can sign in again at the sign_in_url (do not open it yourself); after a day, login also works again when they ask.",
        None,
    ),
    "sign_in_attempts_exceeded": (
        "Sagasu has already made several sign-in attempts for this user recently. Nothing was read or changed.",
        "Tell the user to try again in details.retry_after_minutes minutes. Do not call login before then.",
        None,
    ),
    "sign_in_needed": (
        "The SMU session has ended. Nothing was sent to the user's phone, and nothing was read or changed.",
        "If the user wants to continue, call login and give them the number it returns. If login is not in your tool list, give them the sign_in_url instead (do not open it yourself).",
        "login",
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


# Where a person signs in to SMU from a browser, when this host serves its web
# sign-in page (set by the remote listener). Agents pass it on to the person.
SIGN_IN_URL = None
# Makes a one-time link to that page, which opens it without the access key.
SIGN_IN_LINK = None
# Failures whose cure is the sign-in page.
LINKED_REASONS = frozenset(
    {
        "sign_in_required_on_host",
        "session_renewal_pending",
        "sign_in_setup_needed",
        "saved_password_rejected",
        "sign_in_needs_page",
        # The page is also the way out when chat cannot (or should not) retry.
        "sign_in_blocked",
        "sign_in_not_approved",
        "sign_in_unfinished",
        "sign_in_needed",
    }
)


def set_sign_in_url(url, link=None):
    global SIGN_IN_URL, SIGN_IN_LINK
    SIGN_IN_URL, SIGN_IN_LINK = url, link if url is not None else None


def sign_in_url():
    """A one-time link when the page can make one, else its plain address."""
    if SIGN_IN_LINK is not None:
        try:
            return SIGN_IN_LINK()
        except Exception:
            pass
    return SIGN_IN_URL


def sign_in_instruction():
    """What an agent tells its person when only that person can sign in."""
    if SIGN_IN_URL is None:
        return "tell the user that they must sign in on the server themselves"
    if SIGN_IN_LINK is not None:
        return (
            "give the user the sign_in_url link to open in a browser (do not open it "
            "yourself). There they save their own SMU email and password once and "
            "approve one sign-in in Microsoft Authenticator; after that Sagasu signs "
            "in for them from chat. Never ask for or accept a password in chat"
        )
    return (
        f"give the user this link to open in a browser: {SIGN_IN_URL} . There they "
        "enter their access key and sign in to SMU with their own account, "
        "approving it in Microsoft Authenticator"
    )


def public_failure(error):
    reason = getattr(error, "reason", None)
    if reason not in REASONS:
        return {}
    message, action, tool = REASONS[reason]
    if reason == "sign_in_required_on_host":
        action = (
            "SMU sign-in has lapsed; "
            + sign_in_instruction()
            + ". Do not retry until they say they have signed in."
        )
    elif reason == "session_renewal_pending":
        action = (
            "Retry the same request once. If it fails again, SMU sign-in is needed: "
            + sign_in_instruction()
            + "."
        )
    details = getattr(error, "details", {})
    # Only small counts/flags and known catalogue labels cross the boundary.
    safe = {
        key: value
        for key, value in details.items()
        if key
        in {
            "checked_rooms",
            "available_total",
            "all_checked_rooms_affected",
            "retry_after_minutes",
        }
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
    failure = {
        "reason": reason,
        "message": message,
        "next_action": action,
        "next_tool": tool,
        "details": safe,
    }
    if reason in LINKED_REASONS and SIGN_IN_URL is not None:
        failure["sign_in_url"] = sign_in_url()
    return failure
