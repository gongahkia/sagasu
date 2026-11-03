"""Allowlisted recovery instructions; browser exception text never leaves the host."""

REASONS = {
    "missing_email": (
        "Headless sign-in needs SMU_LOGIN_EMAIL.",
        "Set SMU_LOGIN_EMAIL privately in the host environment, then retry login.",
        "login",
    ),
    "missing_password": (
        "Headless password sign-in needs SMU_LOGIN_PASSWORD.",
        "Configure the password privately on the host or sign in once using a visible browser. Never send it in chat.",
        "login",
    ),
    "session_expired": (
        "The saved FBS session needs renewal.",
        "Run login and complete Microsoft sign-in and MFA.",
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


def public_failure(error):
    reason = getattr(error, "reason", None)
    if reason not in REASONS:
        return {}
    message, action, tool = REASONS[reason]
    details = getattr(error, "details", {})
    # Only small counts/flags cross the failure boundary. Never echo page text.
    safe = {
        key: value
        for key, value in details.items()
        if key in {"checked_rooms", "available_total", "all_checked_rooms_affected"}
        and type(value) in {int, bool}
        and (type(value) is bool or 0 <= value <= 10000)
    }
    return {
        "reason": reason,
        "message": message,
        "next_action": action,
        "next_tool": tool,
        "details": safe,
    }
