"""Hosted MCP policy and presentation over the shared tool implementation."""

from dataclasses import dataclass
from typing import Callable

from ..mcp_adapter import LocalAdapter
from ..origin import CALL_LABEL, CALL_ORIGIN
from ..observability import event

# Never offered to a remote API key. Sign-in, MFA prompts and host control stay
# with the person at the host; a key cannot stand in for a human confirmation;
# approving other people's requests and multi-action batches are too much reach
# for a bearer secret. One exception: on a copy where the person saved their
# own SMU sign-in (RemoteAccess.saved_sign_in), login is offered, and the number
# to approve comes back in operation results (see AgentService.sign_in_view).
REMOTE_HIDDEN_TOOLS = frozenset(
    {
        "login",
        "authentication_status",
        "host_status",
        "recover_host",
        "browser_screenshot",
        "probe_read_backends",
        "smoke_check",
        "confirm_review",
        "batch_review",
    }
)
# Offered remotely only when the host owner sets SAGASU_HTTP_WRITES=on: anything
# that changes FBS, saved data or the action history, opens an unsent FBS form,
# or searches the SMU people directory.
REMOTE_WRITE_TOOLS = frozenset(
    {
        "submit_review",
        # One exact Task List item the person asked for, e.g. accepting a
        # co-booking (owner's decision, 21 Nov 2026). Batches stay hidden.
        "task_approval_review",
        "reconcile_operation",
        "booking_review",
        "planned_booking_review",
        "repeat_booking_review",
        "cancellation_review",
        "amendment_options",
        "amendment_review",
        "validate_booking",
        "get_review",
        "discard_review",
        "booking_options",
        "search_cobookers",
        "save_cobooker",
        "delete_cobooker",
        "set_cobooker_aliases",
        "set_search_preferences",
        "reset_search_preferences",
        "set_room_preferences",
        "save_room_plan",
        "delete_saved_plan",
        "save_booking_template",
        "delete_booking_template",
    }
)


@dataclass(frozen=True)
class RemoteAccess(LocalAdapter):
    """Policy for a server instance reached through the host's remote listener."""

    writes: bool = False
    caller: Callable | None = None  # request headers -> API key label, for the log
    # The person can save their SMU sign-in on this copy, so it signs in from chat.
    saved_sign_in: bool = False

    def label(self, context):
        try:
            headers = context.headers if context is not None else None
            return (self.caller(headers) if self.caller and headers else None) or (
                "unknown"
            )
        except Exception:
            return "unknown"

    transport = "streamable-http"
    local_authentication_instruction = ""
    login_wait_seconds = 25

    @property
    def hidden_tools(self):
        hidden = set(REMOTE_HIDDEN_TOOLS)
        if self.saved_sign_in:
            hidden.discard("login")
        if not self.writes:
            hidden |= REMOTE_WRITE_TOOLS
        return hidden

    async def call_tool(self, checked, name, arguments, context, known_fields):
        # Jobs started by this call inherit the origin through their task context.
        token = CALL_ORIGIN.set("remote")
        label = CALL_LABEL.set(self.label(context))
        outcome = "rejected"
        try:
            result = await checked(name, arguments, context)
            outcome = "error" if getattr(result, "is_error", False) else "ok"
            return result
        finally:
            CALL_LABEL.reset(label)
            CALL_ORIGIN.reset(token)
            event(
                "remote.tool_call",
                tool=name if name in (known_fields() or {}) else "unknown",
                key=self.label(context),
                outcome=outcome,
            )

    @property
    def instructions(self):
        return "REMOTE CONNECTION: you reach a private server over HTTPS with an API key; it stays signed in to one person's FBS account. " "Tools named below that are missing from your tool list are switched off for remote use. " + (
            "SMU SIGN-IN HAPPENS IN THIS CHAT. Sagasu signs in to SMU for the user with the email and password they saved on their own Sagasu sign-in page; you never see them. "
            "Never ask for, accept or repeat a password or code in chat. "
            'When a result has a sign_in field, send the user sign_in.tell_user (it carries the number to enter in Microsoft Authenticator) and end your turn. When they reply (for example "done"), call wait_operation with the same operation_id. '
            "When an error has sign_in_url, give the user the link, do not open it yourself, and when they say they have finished, run their request again. "
            "When an error's next_tool is login, ask the user first: each login may send a request to their phone. On session_renewal_pending retry once. "
            if self.saved_sign_in
            else "You cannot sign in or approve MFA from here. If an operation fails with sign_in_required_on_host, give the user the sign_in_url from the error (they sign in to SMU there themselves) and stop retrying until they say they have; on session_renewal_pending retry once. "
        ) + (
            ""
            if self.writes
            else "This connection is READ-ONLY: search rooms and read bookings, but booking, amending, cancelling, approving and changing saved data are switched off on the server. Say so plainly; do not look for another way. "
        )

    def update_status(self, status, service):
        status["authentication_prompts"] = None
        status["remote_access"] = {
            "read_only": not self.writes,
            "sign_in": (
                "required_on_host"
                if service.host_sign_in_required
                else "not_known_to_be_required"
            ),
            "sign_in_policy": "Only the owner can sign in, on the server itself. session_status verifies the saved session without credentials.",
            "unavailable_tools": sorted(self.hidden_tools),
        }
        store = service.saved_sign_in if self.saved_sign_in else None
        if store is not None:
            held = store.held()
            summary = store.summary()
            status["remote_access"].update(
                sign_in={
                    "saved": summary["saved"],
                    "paused_until_user_acts": held["reason"] if held else None,
                },
                sign_in_policy=(
                    "Sagasu signs in to SMU for the user with the sign-in they "
                    "saved on their own Sagasu page; they approve each sign-in "
                    "in Microsoft Authenticator. When a result has sign_in, give "
                    "the user sign_in.tell_user. session_status checks the "
                    "session and never signs in. Never ask for a password."
                ),
            )
        if not self.writes:
            status.update(
                confirmation="This remote connection is read-only. Booking, amendment, cancellation, approval and saved-data changes are switched off on the server.",
                submission_mode="disabled",
                agent_submission_enabled=False,
                can_submit=False,
                submission_tool=None,
            )

    def update_workflow(self, document):
        document["remote_access"] = {
            "read_only": not self.writes,
            "unavailable_tools": sorted(self.hidden_tools),
            "policy": "Tools missing from the tool list are switched off for remote use. Sequences below that need them do not apply to this connection.",
        }
        document["authentication"] = (
            [
                "SMU sign-in happens in chat: when a session lapses, Sagasu signs in with the email and password the user saved on their own Sagasu page. When a result has a sign_in field, send the user sign_in.tell_user and end your turn; when they reply, call wait_operation with the same operation_id",
                "session_status verifies the saved session without credentials or phone requests",
                "login signs in when the user asks (each call may send one request to their phone). It returns within about 25 seconds, with sign_in when there is a number to enter",
                "sign_in_setup_needed, saved_password_rejected or sign_in_needs_page: give the user the sign_in_url (a one-time page for their SMU sign-in; do not open it yourself) and run their request again when they say they are done. Never ask for or accept a password in chat",
                "sign_in_not_approved or sign_in_unfinished: tell the user; call login only if they want to try again",
                "sign_in_needed: the session ended and nothing was sent to the phone; call login if the user wants to continue",
                "sign_in_blocked or sign_in_attempts_exceeded: tell the user and do not retry",
                "session_renewal_pending: the session did not renew in time; retry the request once",
            ]
            if self.saved_sign_in
            else [
                "The person signs in to SMU themselves, in a browser, at the sign_in_url that sign-in errors carry. Remote connections cannot sign in or read MFA prompts",
                "session_status verifies the saved session without credentials",
                "sign_in_required_on_host: give the user the sign_in_url from the error and stop retrying until they confirm they have signed in",
                "session_renewal_pending: the session did not renew in time; retry the request once",
            ]
        )
        document["recovery"]["session"] = document["authentication"][1]
        document["recovery"]["awaiting_mfa"] = document["authentication"][0]
        if not self.writes:
            document["submission_mode"] = "disabled"
            document["submission_tool"] = None

    login_description = "Sign the user in to SMU with the email and password they saved on their Sagasu sign-in page, or confirm they are still signed in. Use it only when the user asks to sign in or try again. Each call may send a request to their phone. Returns within about 25 seconds: if the result has sign_in, send sign_in.tell_user to the user and end your turn; after they reply, call wait_operation with the operation_id. Never ask for or accept a password in chat. If it fails with sign_in_url, give the user that link and do not open it yourself."
