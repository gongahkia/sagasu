"""Real authentication state handling across agent jobs, without external requests."""

import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from sagasu.fbs_client import FBSClient, LOGIN_PASSWORD_SELECTOR
from sagasu.mcp_service import AgentService
from tests import test_auth as auth_fixtures
from tests.test_mcp import CONFIG


class AuthRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        environment = patch.dict(
            os.environ,
            {
                "SMU_LOGIN_EMAIL": "test@smu.edu.sg",
                "SMU_LOGIN_PASSWORD": "private $test#secret",
                "SAGASU_AUTH_TIMEOUT_MS": "1000",
            },
            clear=True,
        )
        environment.start()
        self.addCleanup(environment.stop)

    def service(self, client):
        class Backend:
            async def run(inner, kind, payload, progress, notify):
                client.progress = progress
                client.auth_notify = notify
                await client._complete_interactive_authentication()
                return {"authenticated": True}

        service = AgentService(backend=Backend())
        self.addAsyncCleanup(service.close)
        return service

    async def test_changed_mfa_number_replaces_prompt_and_success_clears_it(self):
        client = auth_fixtures.AuthTest().flow(
            [
                ("authenticator", "Enter number 24."),
                ("authenticator", "Enter number 31."),
            ]
        )
        service = self.service(client)
        release_first, release_second = asyncio.Event(), asyncio.Event()
        original = client._classify_auth_state

        async def classify():
            if getattr(classify, "count", 0) == 1:
                await release_first.wait()
            state = await original()
            classify.count = getattr(classify, "count", 0) + 1
            return state

        client._classify_auth_state = classify

        async def pause(_):
            if getattr(classify, "count", 0) == 2:
                await release_second.wait()

        client.page.wait_for_timeout.side_effect = pause
        operation = service.start("login")
        first = await service.wait(operation["operation_id"], timeout=1)
        self.assertEqual(first["status"], "awaiting_mfa")
        self.assertIn(
            "24", service.authentication_status(operation["operation_id"])["prompt"]
        )
        release_first.set()
        await asyncio.sleep(0)
        self.assertIn(
            "31", service.authentication_status(operation["operation_id"])["prompt"]
        )
        self.assertNotIn(
            "24", service.authentication_status(operation["operation_id"])["prompt"]
        )
        release_second.set()
        await service.get(operation["operation_id"]).task
        self.assertIsNone(
            service.authentication_status(operation["operation_id"])["prompt"]
        )

    async def test_rejected_mfa_is_terminal_and_never_leaks_configured_credentials(
        self,
    ):
        client = auth_fixtures.AuthTest().flow(
            [
                ("password", "password"),
                ("authenticator", "Enter number 24."),
                ("failed", "Microsoft sign-in was denied"),
            ]
        )
        service = self.service(client)
        with self.assertLogs(level="INFO") as logs:
            operation = service.start("login")
            await service.get(operation["operation_id"]).task
        result = service.result(operation["operation_id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "AuthenticationRequired")
        self.assertIsNone(
            service.authentication_status(operation["operation_id"])["prompt"]
        )
        exposed = json.dumps(result) + "\n".join(logs.output)
        for secret in ("Enter number 24", "private $test#secret", "test@smu.edu.sg"):
            self.assertNotIn(secret, exposed)
        client.fields[LOGIN_PASSWORD_SELECTOR].fill.assert_awaited_once()

    async def test_invalid_context_reason_reaches_mcp_without_a_false_mfa_prompt(self):
        client = auth_fixtures.AuthTest().flow(
            [
                ("invalid_context", "PRIVATE_CONTEXT"),
                ("invalid_context", "PRIVATE_CONTEXT"),
            ]
        )
        service = self.service(client)
        operation = service.start("login")
        await service.get(operation["operation_id"]).task
        result = service.result(operation["operation_id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["reason"], "authentication_context_invalid")
        self.assertEqual(result["error"]["next_tool"], "login")
        self.assertIsNone(
            service.authentication_status(operation["operation_id"])["prompt"]
        )
        self.assertNotIn("PRIVATE_CONTEXT", json.dumps(result))

    async def test_provider_error_is_not_reported_as_session_expiry(self):
        client = auth_fixtures.AuthTest().flow(
            [("provider_error", "PRIVATE_PROVIDER_ERROR")]
        )
        service = self.service(client)
        operation = service.start("login")
        await service.get(operation["operation_id"]).task
        result = service.result(operation["operation_id"])
        self.assertEqual(result["error"]["reason"], "authentication_failed")
        self.assertIsNone(
            service.authentication_status(operation["operation_id"])["prompt"]
        )
        self.assertNotIn("PRIVATE_PROVIDER_ERROR", json.dumps(result))

    async def test_cancellation_while_waiting_for_mfa_clears_prompt_and_stops_login(
        self,
    ):
        client = auth_fixtures.AuthTest().flow([("authenticator", "Enter number 24.")])
        gate = asyncio.Event()

        async def pause(_):
            await gate.wait()

        client.page.wait_for_timeout.side_effect = pause
        service = self.service(client)
        operation = service.start("login")
        await service.wait(operation["operation_id"], timeout=1)
        cancelled = await service.cancel(operation["operation_id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertIsNone(
            service.authentication_status(operation["operation_id"])["prompt"]
        )

    async def test_session_expiry_during_search_is_not_empty_availability_or_automatic_login(
        self,
    ):
        client = FBSClient()
        client.page = Mock(
            url="https://login.microsoftonline.com/tenant/login?cookie=PRIVATE_COOKIE"
        )
        client._search_available = AsyncMock(
            side_effect=PlaywrightTimeoutError("PRIVATE_COOKIE")
        )
        client._complete_interactive_authentication = AsyncMock()

        class Backend:
            async def run(inner, kind, payload, progress, notify):
                return (await client.search(payload)).to_dict()

        service = AgentService(backend=Backend())
        self.addAsyncCleanup(service.close)
        operation = service.search(CONFIG)
        await service.get(operation["operation_id"]).task
        result = service.result(operation["operation_id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], "AuthenticationRequired")
        self.assertNotIn("PRIVATE_COOKIE", json.dumps(result))
        self.assertNotIn("result", result)
        client._complete_interactive_authentication.assert_not_awaited()
