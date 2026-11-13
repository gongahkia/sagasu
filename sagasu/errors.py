"""Allowlisted recovery instructions; browser exception text never leaves the host."""

REASONS = {
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
