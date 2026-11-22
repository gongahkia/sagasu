"""OAuth sign-in for the remote listener, for agent apps that cannot send a key header.

Hosted path only (see SETUP.md, "Hosting for a group"): used by the remote
listener and the gateway. The local stdio path never uses it.

Some apps (ChatGPT, for one) only connect to a remote MCP server through
OAuth. This is a small authorization server for them. The person types an API
key once, on this server's own page; the app then holds short-lived tokens
tied to that key. Removing the key from the host's configuration ends them.

Nothing here signs in to SMU or FBS. A token is only another way to present an
API key, with the same access and the same label in the log.

Security choices:
- Codes go back only to redirect URIs on configured hosts (by default the
  ChatGPT and Claude callbacks, and loopback for apps on the same computer).
- PKCE (S256) is required by the SDK's handlers.
- Only SHA-256 digests of tokens are stored. Refresh tokens rotate, and
  presenting a rotated one ends the whole grant.
- The sign-in page limits wrong keys per client address and overall.
"""

import contextvars
import hashlib
import html
import json
import logging
import os
import secrets
import sqlite3
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from mcp.server.auth.handlers.metadata import (
    MetadataHandler,
    ProtectedResourceMetadataHandler,
)
from mcp.server.auth.middleware.client_auth import (
    AuthenticationError,
    ClientAuthenticator,
)
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    AuthorizeError,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.server.auth.routes import (
    build_metadata,
    cors_middleware,
    create_auth_routes,
)
from mcp.server.auth.settings import ClientRegistrationOptions, RevocationOptions
from mcp.shared.auth import (
    OAuthClientInformationFull,
    OAuthToken,
    ProtectedResourceMetadata,
)

from ..observability import event

SCOPE = "sagasu"
SIGN_IN_PATH = "/oauth/sign-in"
RESOURCE_METADATA_PATH = "/.well-known/oauth-protected-resource"
AUTHORIZATION_METADATA_PATH = "/.well-known/oauth-authorization-server"
# Paths served without an API key. Everything else needs a key or a token.
PUBLIC_PATHS = frozenset(
    {
        RESOURCE_METADATA_PATH,
        RESOURCE_METADATA_PATH + "/mcp",
        AUTHORIZATION_METADATA_PATH,
        "/authorize",
        "/token",
        "/register",
        "/revoke",
        SIGN_IN_PATH,
    }
)

ACCESS_SECONDS = 3600
REFRESH_SECONDS = 30 * 86400
# After this the person types the key again, however often the app refreshed.
GRANT_SECONDS = 90 * 86400
CODE_SECONDS = 300
PENDING_SECONDS = 600
# Sign-ins started but not finished: per client address, and in all.
PENDING_PER_ADDRESS = 20
MAX_PENDING = 1000
REGISTRATIONS_PER_ADDRESS = 20
MAX_CODES = 200
MAX_CLIENTS = 200
# Keys are 256-bit random values, so this limit only slows a nuisance down.
# There is deliberately no overall limit: it would let anyone lock the owner out.
WRONG_KEYS_PER_ADDRESS = 10
WRONG_KEY_WINDOW = 600
LOOPBACK = frozenset({"localhost", "127.0.0.1", "[::1]", "::1"})
# Text-direction overrides could reorder the sign-in sentence around a host name.
BIDI_CONTROLS = frozenset(
    chr(code) for code in (*range(0x202A, 0x202F), *range(0x2066, 0x206A))
)

# The address a public OAuth request came from, set by the gate in remote.py
# from the proxy's X-Forwarded-For. The SDK's handlers do not pass the request on.
CLIENT_ADDRESS = contextvars.ContextVar("sagasu_oauth_address", default="unknown")


def display_name(name):
    """An app's self-chosen name, safe to show on the page and in a terminal."""
    kept = "".join(
        char for char in name or "" if char.isprintable() and char not in BIDI_CONTROLS
    ).strip()
    return kept[:80] or None


SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    client_id TEXT PRIMARY KEY,
    info TEXT NOT NULL,
    created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS grants (
    grant_id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    key_digest TEXT NOT NULL,
    scopes TEXT NOT NULL,
    created REAL NOT NULL,
    client_name TEXT,
    redirect_host TEXT
);
CREATE TABLE IF NOT EXISTS tokens (
    digest TEXT PRIMARY KEY,
    grant_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    expires REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS tokens_by_grant ON tokens (grant_id);
"""


def digest(value):
    return hashlib.sha256(value.encode("utf-8", "surrogatepass")).hexdigest()


def redirect_allowed(uri, hosts):
    """True for a redirect URI on an allowed host: https, or http on loopback."""
    try:
        parsed = urlsplit(str(uri))
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        return False
    if (
        not host
        or host not in hosts
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        return False
    if host in LOOPBACK:
        return parsed.scheme in {"http", "https"}
    return parsed.scheme == "https" and port in {None, 443}


class Limiter:
    """Counts events per key within a sliding window."""

    def __init__(self, limit, window, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self.events = {}

    def used_up(self, key):
        now = self.clock()
        recent = self.events.get(key)
        while recent and now - recent[0] >= self.window:
            recent.popleft()
        return bool(recent) and len(recent) >= self.limit

    def record(self, key):
        if len(self.events) > 5000:
            self.events.clear()
        self.events.setdefault(key, deque()).append(self.clock())


class Client(OAuthClientInformationFull):
    """A registered app. Scopes grant nothing beyond the key, so unknown ones are dropped."""

    def validate_scope(self, requested_scope):
        if requested_scope is None:
            return None
        return [scope for scope in requested_scope.split() if scope == SCOPE]


class Code(AuthorizationCode):
    key_digest: str
    client_name: str | None = None
    redirect_host: str | None = None


class Refresh(RefreshToken):
    grant_id: str


class Access(AccessToken):
    grant_id: str


class OAuthStore:
    """Registered apps and token digests, in a private SQLite file."""

    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(
            str(path), isolation_level=None, check_same_thread=False
        )
        os.chmod(path, 0o600)
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    def client(self, client_id):
        row = self.db.execute(
            "SELECT info FROM clients WHERE client_id = ?", (client_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def add_client(self, client_id, info, now):
        (count,) = self.db.execute("SELECT COUNT(*) FROM clients").fetchone()
        if count >= MAX_CLIENTS:
            # Make room by forgetting apps that never finished signing in.
            self.db.execute(
                """DELETE FROM clients WHERE client_id IN (
                    SELECT client_id FROM clients
                    WHERE created < ? AND client_id NOT IN (SELECT client_id FROM grants)
                    ORDER BY created LIMIT 50)""",
                (now - 86400,),
            )
            (count,) = self.db.execute("SELECT COUNT(*) FROM clients").fetchone()
            if count >= MAX_CLIENTS:
                return False
        self.db.execute(
            "INSERT INTO clients (client_id, info, created) VALUES (?, ?, ?)",
            (client_id, info, now),
        )
        return True

    def add_grant(self, client_id, key_digest, scopes, now, client_name, host):
        grant_id = secrets.token_urlsafe(16)
        self.db.execute(
            "INSERT INTO grants VALUES (?, ?, ?, ?, ?, ?, ?)",
            (grant_id, client_id, key_digest, " ".join(scopes), now, client_name, host),
        )
        return grant_id

    def add_token(self, token_digest, grant_id, kind, expires):
        self.db.execute(
            "INSERT INTO tokens VALUES (?, ?, ?, ?)",
            (token_digest, grant_id, kind, expires),
        )

    def token(self, token_digest):
        row = self.db.execute(
            """SELECT t.kind, t.expires, g.grant_id, g.client_id, g.key_digest,
                      g.scopes, g.created
               FROM tokens t JOIN grants g ON g.grant_id = t.grant_id
               WHERE t.digest = ?""",
            (token_digest,),
        ).fetchone()
        if row is None:
            return None
        keys = ("kind", "expires", "grant_id", "client_id", "key_digest")
        found = dict(zip(keys, row[:5]))
        found["scopes"] = row[5].split() if row[5] else []
        found["grant_created"] = row[6]
        return found

    def rotate(self, token_digest):
        """Mark a refresh token used. False if it was not a live refresh token."""
        changed = self.db.execute(
            "UPDATE tokens SET kind = 'rotated' WHERE digest = ? AND kind = 'refresh'",
            (token_digest,),
        ).rowcount
        return changed == 1

    def end_grant(self, grant_id):
        self.db.execute("DELETE FROM tokens WHERE grant_id = ?", (grant_id,))
        self.db.execute("DELETE FROM grants WHERE grant_id = ?", (grant_id,))

    def prune(self, now):
        self.db.execute("DELETE FROM tokens WHERE expires < ?", (now,))
        # A grant with no tokens left has ended. A new one gets its tokens
        # straight after it is written, so give it a minute's grace.
        self.db.execute(
            """DELETE FROM grants WHERE created < ?
               OR (created < ? AND grant_id NOT IN (SELECT grant_id FROM tokens))""",
            (now - GRANT_SECONDS, now - 60),
        )
        self.db.execute(
            "DELETE FROM tokens WHERE grant_id NOT IN (SELECT grant_id FROM grants)"
        )

    def keep_only_keys(self, digests):
        """End every grant made with a key that is no longer configured, so a
        key that is restored later does not bring its old sign-ins back."""
        digests = sorted(digests)
        if digests:
            marks = ",".join("?" * len(digests))
            self.db.execute(
                f"DELETE FROM grants WHERE key_digest NOT IN ({marks})", digests
            )
        else:
            # No key at all: every grant ends. ("NOT IN (NULL)" would match none.)
            self.db.execute("DELETE FROM grants")
        self.db.execute(
            "DELETE FROM tokens WHERE grant_id NOT IN (SELECT grant_id FROM grants)"
        )

    def clear(self):
        self.db.execute("DELETE FROM tokens")
        self.db.execute("DELETE FROM grants")
        self.db.execute("DELETE FROM clients")

    def grants(self):
        return self.db.execute(
            """SELECT g.key_digest, g.client_name, g.redirect_host, g.created
               FROM grants g ORDER BY g.created"""
        ).fetchall()


class SagasuOAuth:
    """The OAuth authorization server for one remote listener."""

    def __init__(self, store_path, keys, public_host, redirect_hosts, clock=time.time):
        self.store = OAuthStore(store_path)
        self.keys = keys  # SHA-256 digest of each API key -> label
        self.redirect_hosts = frozenset(redirect_hosts)
        self.issuer = f"https://{public_host}"
        self.resource = f"{self.issuer}/mcp"
        self.resource_metadata_url = f"{self.issuer}{RESOURCE_METADATA_PATH}/mcp"
        self.clock = clock
        self.pending = {}
        self.codes = {}
        self.wrong_keys = Limiter(WRONG_KEYS_PER_ADDRESS, WRONG_KEY_WINDOW)
        self.registrations = Limiter(REGISTRATIONS_PER_ADDRESS, 3600)
        # The SMU sign-in page (signin_desk.py). With it, an app is sent back
        # only once this copy is signed in to SMU.
        self.desk = None
        self.store.keep_only_keys(keys)
        self.store.prune(self.clock())

    def close(self):
        self.store.close()

    # -- what the gate in front of /mcp asks -------------------------------------

    def verify(self, token):
        """Return (label, None) for a live access token, else (None, reason)."""
        found = self.store.token(digest(token))
        if found is None or found["kind"] != "access":
            return None, "unknown_token"
        if found["expires"] <= self.clock():
            return None, "expired_token"
        label = self.keys.get(found["key_digest"])
        if label is None:
            return None, "key_removed"
        return label, None

    # -- the SDK's OAuthAuthorizationServerProvider --------------------------------

    async def get_client(self, client_id):
        info = self.store.client(client_id)
        return Client.model_validate(info) if info else None

    async def register_client(self, client_info):
        refused = [
            str(uri)
            for uri in client_info.redirect_uris or []
            if not redirect_allowed(uri, self.redirect_hosts)
        ]
        if refused or not client_info.redirect_uris:
            raise RegistrationError(
                "invalid_redirect_uri",
                "This server only signs in apps whose redirect is on: "
                + ", ".join(sorted(self.redirect_hosts)),
            )
        address = CLIENT_ADDRESS.get()
        if self.registrations.used_up(address):
            raise RegistrationError(
                "invalid_client_metadata", "Too many registrations. Try again later."
            )
        self.registrations.record(address)
        client_info.client_name = display_name(client_info.client_name)
        if not self.store.add_client(
            client_info.client_id, client_info.model_dump_json(), self.clock()
        ):
            raise RegistrationError(
                "invalid_client_metadata", "Too many apps are registered."
            )
        event(
            "remote.oauth_client_registered",
            redirect_host=urlsplit(str(client_info.redirect_uris[0])).hostname,
        )

    async def authorize(self, client, params: AuthorizationParams):
        if not redirect_allowed(params.redirect_uri, self.redirect_hosts):
            raise AuthorizeError("access_denied", "This app's redirect is not allowed.")
        if params.resource is not None and params.resource.rstrip("/") not in {
            self.resource,
            self.issuer,
        }:
            raise AuthorizeError("invalid_target", "Unknown resource.")
        now = self.clock()
        self.pending = {
            key: value for key, value in self.pending.items() if value["expires"] > now
        }
        address = CLIENT_ADDRESS.get()
        if (
            sum(value["address"] == address for value in self.pending.values())
            >= PENDING_PER_ADDRESS
        ):
            raise AuthorizeError(
                "temporarily_unavailable", "Too many sign-ins are in progress."
            )
        while len(self.pending) >= MAX_PENDING:
            # Dicts keep insertion order: drop the oldest unfinished sign-in.
            self.pending.pop(next(iter(self.pending)))
        request_id = secrets.token_urlsafe(24)
        self.pending[request_id] = {
            "address": address,
            "client_id": client.client_id,
            "client_name": client.client_name,
            "params": params,
            "expires": now + PENDING_SECONDS,
            "wrong": 0,
        }
        return f"{self.issuer}{SIGN_IN_PATH}?request={request_id}"

    async def load_authorization_code(self, client, authorization_code):
        code = self.codes.get(authorization_code)
        if code is None or code.client_id != client.client_id:
            return None
        return code

    async def exchange_authorization_code(self, client, authorization_code):
        code = self.codes.pop(authorization_code.code, None)
        if code is None:
            raise TokenError("invalid_grant", "The sign-in code was already used.")
        if code.key_digest not in self.keys:
            raise TokenError("invalid_grant", "That key no longer works.")
        now = self.clock()
        self.store.prune(now)
        grant_id = self.store.add_grant(
            client.client_id,
            code.key_digest,
            code.scopes,
            now,
            code.client_name,
            code.redirect_host,
        )
        event(
            "remote.oauth_signed_in",
            key=self.keys[code.key_digest],
            redirect_host=code.redirect_host,
        )
        return self._issue(grant_id, code.scopes, now, now)

    async def load_refresh_token(self, client, refresh_token):
        found = self.store.token(digest(refresh_token))
        if found is None:
            return None
        if found["kind"] == "rotated":
            # A used refresh token came back: someone else may hold a copy.
            self.store.end_grant(found["grant_id"])
            event(
                "remote.oauth_refresh_reused",
                level=logging.WARNING,
                key=self.keys.get(found["key_digest"], "removed"),
            )
            return None
        if (
            found["kind"] != "refresh"
            or found["client_id"] != client.client_id
            or found["expires"] <= self.clock()
            or found["key_digest"] not in self.keys
        ):
            return None
        return Refresh(
            token=refresh_token,
            client_id=found["client_id"],
            scopes=found["scopes"],
            expires_at=int(found["expires"]),
            resource=self.resource,
            subject=self.keys[found["key_digest"]],
            grant_id=found["grant_id"],
        )

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        found = self.store.token(digest(refresh_token.token))
        if found is None or not self.store.rotate(digest(refresh_token.token)):
            raise TokenError("invalid_grant", "The refresh token was already used.")
        self.store.prune(self.clock())
        return self._issue(
            found["grant_id"], scopes, self.clock(), found["grant_created"]
        )

    async def load_access_token(self, token):
        found = self.store.token(digest(token))
        label, _ = self.verify(token)
        if found is None or label is None:
            return None
        return Access(
            token=token,
            client_id=found["client_id"],
            scopes=found["scopes"],
            expires_at=int(found["expires"]),
            resource=self.resource,
            subject=label,
            grant_id=found["grant_id"],
        )

    async def revoke_token(self, token):
        grant_id = getattr(token, "grant_id", None)
        if grant_id:
            self.store.end_grant(grant_id)

    def _issue(self, grant_id, scopes, now, grant_created):
        access = "sgt_" + secrets.token_urlsafe(32)
        refresh = "sgr_" + secrets.token_urlsafe(32)
        self.store.add_token(digest(access), grant_id, "access", now + ACCESS_SECONDS)
        self.store.add_token(
            digest(refresh),
            grant_id,
            "refresh",
            min(now + REFRESH_SECONDS, grant_created + GRANT_SECONDS),
        )
        return OAuthToken(
            access_token=access,
            expires_in=ACCESS_SECONDS,
            scope=" ".join(scopes) or None,
            refresh_token=refresh,
        )

    # -- the sign-in page ------------------------------------------------------------

    def _address(self, request):
        # Caddy replaces X-Forwarded-For with the address it saw; nothing else
        # can reach the loopback listener from outside.
        forwarded = request.headers.get("x-forwarded-for", "")
        return forwarded.split(",")[0].strip() or (
            request.client.host if request.client else "unknown"
        )

    async def sign_in(self, request: Request):
        now = self.clock()
        if request.method == "POST":
            # Browsers send Origin "null" under some privacy settings. A post
            # from another site is refused; the key itself is the real secret.
            origin = request.headers.get("origin")
            if (
                origin
                and origin != "null"
                and (urlsplit(origin).hostname != urlsplit(self.issuer).hostname)
            ):
                return self._page(
                    "Not allowed",
                    ERROR_BODY.format(
                        message="This form can only be sent from its own page."
                    ),
                    403,
                )
            form = await request.form()
            request_id = str(form.get("request") or "")
        else:
            form = None
            request_id = request.query_params.get("request", "")
        pending = self.pending.get(request_id)
        if pending is None or pending["expires"] <= now:
            self.pending.pop(request_id, None)
            return self._page(
                "Sign-in expired",
                ERROR_BODY.format(
                    message="This sign-in link has expired or was already used. "
                    "Start again from your app."
                ),
                400,
            )
        params = pending["params"]
        redirect_host = urlsplit(str(params.redirect_uri)).hostname or ""
        if form is None:
            return self._form(request_id, pending, redirect_host)
        continuation = {
            "client_id": pending["client_id"],
            "client_name": pending["client_name"],
            "params": params,
            "redirect_host": redirect_host,
        }
        if form.get("action") == "cancel":
            self.pending.pop(request_id, None)
            return RedirectResponse(
                self.refuse(continuation), status_code=303, headers=NO_STORE
            )
        address = self._address(request)
        if self.wrong_keys.used_up(address):
            return self._form(
                request_id,
                pending,
                redirect_host,
                "Too many wrong keys. Wait ten minutes, then try again.",
                429,
            )
        from .remote import check_key

        label, _ = check_key(self.keys, {"x-api-key": str(form.get("key") or "")})
        if label is None:
            self.wrong_keys.record(address)
            pending["wrong"] += 1
            event("remote.oauth_wrong_key", level=logging.WARNING)
            if pending["wrong"] >= 5:
                self.pending.pop(request_id, None)
                return self._page(
                    "Sign-in stopped",
                    ERROR_BODY.format(
                        message="Too many wrong keys for this sign-in. "
                        "Start again from your app."
                    ),
                    400,
                )
            return self._form(
                request_id,
                pending,
                redirect_host,
                "That key was not accepted. Check it and try again.",
                400,
            )
        self.pending.pop(request_id, None)
        if self.desk is not None:
            # Next: sign in to SMU on this server; the app is sent back after.
            return self.desk.begin(request, label, continuation)
        return RedirectResponse(
            self.finish(continuation, label), status_code=303, headers=NO_STORE
        )

    def finish(self, continuation, label):
        """The app's redirect with a fresh one-time code for this key."""
        params = continuation["params"]
        now = self.clock()
        key_digest = next(known for known, name in self.keys.items() if name == label)
        self.codes = {
            key: value for key, value in self.codes.items() if value.expires_at > now
        }
        if len(self.codes) >= MAX_CODES:
            self.codes.clear()
        code = secrets.token_urlsafe(32)
        self.codes[code] = Code(
            code=code,
            # Without a requested scope the grant still carries the advertised
            # one, so an app that names it when refreshing is not refused.
            scopes=params.scopes or [SCOPE],
            expires_at=now + CODE_SECONDS,
            client_id=continuation["client_id"],
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=self.resource,
            subject=label,
            key_digest=key_digest,
            client_name=continuation["client_name"],
            redirect_host=continuation["redirect_host"],
        )
        return construct_redirect_uri(
            str(params.redirect_uri), code=code, state=params.state
        )

    @staticmethod
    def refuse(continuation):
        params = continuation["params"]
        return construct_redirect_uri(
            str(params.redirect_uri),
            error="access_denied",
            error_description="The sign-in was cancelled.",
            state=params.state,
        )

    def _form(self, request_id, pending, redirect_host, error=None, status=200):
        name = pending["client_name"] or "An app"
        body = FORM_BODY.format(
            next_step=(
                " Next, you sign in to your own SMU account here, if you are not"
                " signed in already."
                if self.desk is not None
                else " If your SMU sign-in is not saved yet, your app will give you"
                " a link to save it."
            ),
            app=html.escape(name),
            host=html.escape(redirect_host),
            request=html.escape(request_id, quote=True),
            error=(
                f'<p class="error" role="alert">{html.escape(error)}</p>'
                if error
                else ""
            ),
        )
        return self._page("Sign in to Sagasu", body, status)

    @staticmethod
    def _page(title, body, status):
        page = PAGE.format(title=html.escape(title), body=body)
        return HTMLResponse(page, status_code=status, headers=PAGE_HEADERS)

    async def revoke(self, request: Request):
        """RFC 7009 revocation. The SDK's own handler wrongly requires a
        client_secret field even from apps registered without one."""
        try:
            client = await ClientAuthenticator(self).authenticate_request(request)
        except AuthenticationError:
            return Response(status_code=401, headers=NO_STORE)
        form = await request.form()
        token = form.get("token")
        if not isinstance(token, str) or not token:
            return Response(status_code=400, headers=NO_STORE)
        found = await self.load_access_token(token) or await self.load_refresh_token(
            client, token
        )
        if found is not None and found.client_id == client.client_id:
            await self.revoke_token(found)
        # RFC 7009 §2.2: unknown tokens are not an error.
        return Response(status_code=200, headers=NO_STORE)

    # -- routes ------------------------------------------------------------------------

    def routes(self):
        issuer = AnyHttpUrl(self.issuer)
        # Any requested scope is accepted at registration and later ignored:
        # access comes from the key, so refusing an app's scope list would only
        # stop it connecting.
        registration = ClientRegistrationOptions(
            enabled=True, valid_scopes=None, default_scopes=[SCOPE]
        )
        revocation = RevocationOptions(enabled=True)
        routes = [
            route
            for route in create_auth_routes(
                provider=self,
                issuer_url=issuer,
                client_registration_options=registration,
                revocation_options=revocation,
            )
            if route.path not in {AUTHORIZATION_METADATA_PATH, "/revoke"}
        ]
        metadata = build_metadata(issuer, None, registration, revocation)
        metadata.scopes_supported = [SCOPE]
        # Public apps (no client secret) are welcome; PKCE protects their codes.
        metadata.token_endpoint_auth_methods_supported = [
            "none",
            "client_secret_post",
            "client_secret_basic",
        ]
        resource_metadata = ProtectedResourceMetadata(
            resource=AnyHttpUrl(self.resource),
            authorization_servers=[issuer],
            scopes_supported=[SCOPE],
            resource_name="Sagasu FBS",
        )
        about_resource = cors_middleware(
            ProtectedResourceMetadataHandler(resource_metadata).handle,
            ["GET", "OPTIONS"],
        )
        return [
            *routes,
            Route(
                AUTHORIZATION_METADATA_PATH,
                endpoint=cors_middleware(
                    MetadataHandler(metadata).handle, ["GET", "OPTIONS"]
                ),
                methods=["GET", "OPTIONS"],
            ),
            Route(
                RESOURCE_METADATA_PATH + "/mcp",
                endpoint=about_resource,
                methods=["GET", "OPTIONS"],
            ),
            Route(
                RESOURCE_METADATA_PATH,
                endpoint=about_resource,
                methods=["GET", "OPTIONS"],
            ),
            Route(SIGN_IN_PATH, endpoint=self.sign_in, methods=["GET", "POST"]),
            Route(
                "/revoke",
                endpoint=cors_middleware(self.revoke, ["POST", "OPTIONS"]),
                methods=["POST", "OPTIONS"],
            ),
        ]


NO_STORE = {"Cache-Control": "no-store"}
PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; "
        "frame-ancestors 'none'"
    ),
    "X-Frame-Options": "DENY",
    # "no-referrer" would make the page's own form post carry Origin "null".
    "Referrer-Policy": "same-origin",
    "X-Content-Type-Options": "nosniff",
}

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>{title}</title>
<style>
:root {{ color-scheme: light dark; --bg: #f6f6f4; --card: #fff; --text: #1d1d1b;
  --muted: #5f5f5a; --line: #d9d9d4; --accent: #1f5f8b; --error: #a12a1a; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg: #161615; --card: #22221f;
  --text: #ecece8; --muted: #a9a9a2; --line: #3a3a36; --accent: #7db3d9; --error: #f08a78; }} }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--text);
  font: 17px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
main {{ max-width: 420px; margin: 0 auto; padding: 32px 16px; }}
.card {{ background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 24px; }}
h1 {{ font-size: 22px; margin: 0 0 12px; }}
p {{ margin: 0 0 14px; }}
.muted {{ color: var(--muted); font-size: 15px; }}
label {{ display: block; font-weight: 600; margin: 18px 0 6px; }}
input {{ width: 100%; font: inherit; padding: 12px; border: 1px solid var(--line);
  border-radius: 10px; background: var(--bg); color: var(--text); }}
button {{ width: 100%; font: inherit; font-weight: 600; padding: 13px; margin-top: 14px;
  border-radius: 10px; border: 1px solid var(--accent); background: var(--accent); color: #fff; }}
button.secondary {{ background: transparent; color: var(--text); border-color: var(--line); }}
.error {{ color: var(--error); font-weight: 600; }}
</style>
</head>
<body><main><div class="card">{body}</div></main></body>
</html>"""

FORM_BODY = """<h1>Sign in to Sagasu</h1>
<p><strong>{app}</strong> wants to use this Sagasu FBS server. It will be sent back to <strong>{host}</strong>.</p>
<p class="muted">Only continue if you started this from that app yourself. The key is checked here and never given to the app.{next_step}</p>
{error}
<form method="post" autocomplete="off">
<input type="hidden" name="request" value="{request}">
<label for="key">Access key</label>
<input id="key" name="key" type="password" required autocomplete="off" autocapitalize="off" autocorrect="off" spellcheck="false" autofocus>
<button type="submit" name="action" value="allow">Continue</button>
<button type="submit" name="action" value="cancel" class="secondary" formnovalidate>Cancel</button>
</form>"""

ERROR_BODY = """<h1>Sagasu</h1>
<p>{message}</p>"""
