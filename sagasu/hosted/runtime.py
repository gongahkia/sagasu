"""Bootstrap the optional hosted adapter without changing local startup."""


def saved_sign_in(settings, listener):
    """The person's saved sign-in store, or None when this copy cannot keep one.

    Off unless the key file is set and readable at start-up: a copy without a
    key behaves exactly as before (the person signs in on the page; agents
    cannot sign in).
    """
    if not getattr(listener, "sign_in_key_file", None):
        return None
    from ..observability import event
    from .saved_sign_in import SavedSignIn

    store = SavedSignIn(
        settings.preferences.parent,
        listener.sign_in_key_file,
        account=getattr(listener, "smu_account", None),
    )
    if not store.available:
        event("saved_sign_in.unavailable", level=30)
        return None
    return store


async def start_listener(settings, service, configuration_provider):
    from . import require_dependencies

    require_dependencies()
    listener = settings.http
    from ..mcp_server import create_server
    from .mcp_adapter import RemoteAccess
    from .remote import LABEL_HEADER, RemoteHTTP

    from .authentication import HostedAuthentication

    service.authentication = HostedAuthentication(
        saved_sign_in(settings, listener), clock=service.clock
    )
    remote_http = RemoteHTTP(
        listener,
        create_server(
            service,
            configuration_provider=configuration_provider,
            allow_agent_submission=listener.writes,
            adapter=RemoteAccess(
                writes=listener.writes,
                # The gate in front of /mcp names the key it
                # accepted, whether sent directly or as a token.
                caller=lambda headers: headers.get(LABEL_HEADER),
                saved_sign_in=service.saved_sign_in is not None,
            ),
        ),
        oauth_store=settings.preferences.with_name("oauth.sqlite3"),
        service=service,
    )
    try:
        await remote_http.start()
    except BaseException:
        # ownership reaches the host only after startup returns.
        await remote_http.stop()
        raise
    return remote_http
