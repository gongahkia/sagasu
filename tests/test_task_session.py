"""Targeted application-session recovery never clears authentication or loops."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sagasu.fbs_account import _open_task_list
from sagasu.fbs_client import AuthenticationRequired, FBSApplicationError, LayoutChanged


class TaskSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cookie = {
            "name": "ASP.NET_SessionId",
            "domain": "fbs.intranet.smu.edu.sg",
            "path": "/",
            "value": "PRIVATE_SESSION",
        }
        self.client = SimpleNamespace(
            context=SimpleNamespace(
                cookies=AsyncMock(return_value=[self.cookie]),
                clear_cookies=AsyncMock(),
                add_cookies=AsyncMock(),
            ),
            ensure_authenticated=AsyncMock(),
            _save_session=AsyncMock(),
            progress=AsyncMock(),
            interactive=True,
        )
        self.error = FBSApplicationError("PRIVATE ERROR")

    async def test_healthy_task_read_never_changes_cookies(self):
        frame = object()
        with patch(
            "sagasu.fbs_account.open_account_tab", AsyncMock(return_value=frame)
        ):
            self.assertEqual(await _open_task_list(self.client), (frame, False))
        self.client.context.cookies.assert_not_awaited()
        self.client.context.clear_cookies.assert_not_awaited()

    async def test_recovery_clears_only_the_exact_application_cookie_and_keeps_login_noninteractive(
        self,
    ):
        frame = object()

        async def verify():
            self.assertFalse(self.client.interactive)

        self.client.ensure_authenticated.side_effect = verify
        with patch(
            "sagasu.fbs_account.open_account_tab",
            AsyncMock(side_effect=[self.error, frame]),
        ) as navigation:
            self.assertEqual(await _open_task_list(self.client), (frame, True))
        self.assertEqual(navigation.await_count, 2)
        self.client.context.clear_cookies.assert_awaited_once_with(
            name="ASP.NET_SessionId", domain="fbs.intranet.smu.edu.sg", path="/"
        )
        self.client.context.add_cookies.assert_not_awaited()
        self.client._save_session.assert_awaited_once()
        self.assertTrue(self.client.interactive)

    async def test_unknown_cookie_scope_is_not_reset(self):
        for cookies in [
            [],
            [self.cookie, self.cookie],
            [{**self.cookie, "domain": ".smu.edu.sg"}],
            [{**self.cookie, "path": "/other"}],
        ]:
            self.client.context.cookies.return_value = cookies
            with patch(
                "sagasu.fbs_account.open_account_tab", AsyncMock(side_effect=self.error)
            ) as navigation:
                with self.assertRaises(FBSApplicationError):
                    await _open_task_list(self.client)
            self.assertEqual(navigation.await_count, 1)
        self.client.context.clear_cookies.assert_not_awaited()

    async def test_repeated_application_error_rolls_back_and_is_not_an_empty_task_list(
        self,
    ):
        with patch(
            "sagasu.fbs_account.open_account_tab", AsyncMock(side_effect=self.error)
        ) as navigation:
            with self.assertRaises(FBSApplicationError):
                await _open_task_list(self.client)
        self.assertEqual(navigation.await_count, 2)
        self.assertEqual(self.client.context.clear_cookies.await_count, 2)
        self.client.context.add_cookies.assert_awaited_once_with([self.cookie])
        self.client._save_session.assert_awaited_once()
        self.assertTrue(self.client.interactive)

    async def test_recovery_authentication_failure_restores_the_old_session_without_starting_login(
        self,
    ):
        async def denied():
            self.assertFalse(self.client.interactive)
            raise AuthenticationRequired("PRIVATE AUTH ERROR")

        self.client.ensure_authenticated.side_effect = denied
        with patch(
            "sagasu.fbs_account.open_account_tab", AsyncMock(side_effect=self.error)
        ) as navigation:
            with self.assertRaises(FBSApplicationError) as caught:
                await _open_task_list(self.client)
        self.assertIs(caught.exception, self.error)
        self.assertEqual(navigation.await_count, 1)
        self.client.context.add_cookies.assert_awaited_once_with([self.cookie])
        self.assertTrue(self.client.interactive)

    async def test_unrelated_layout_failure_never_resets_session(self):
        with patch(
            "sagasu.fbs_account.open_account_tab",
            AsyncMock(side_effect=LayoutChanged("PRIVATE")),
        ):
            with self.assertRaises(LayoutChanged):
                await _open_task_list(self.client)
        self.client.context.clear_cookies.assert_not_awaited()

    async def test_cancellation_restores_cookie_and_preserves_cancelled_outcome(self):
        self.client.ensure_authenticated.side_effect = asyncio.CancelledError()
        with patch(
            "sagasu.fbs_account.open_account_tab", AsyncMock(side_effect=self.error)
        ):
            with self.assertRaises(asyncio.CancelledError):
                await _open_task_list(self.client)
        self.client.context.add_cookies.assert_awaited_once_with([self.cookie])
        self.client._save_session.assert_awaited_once()
        self.assertTrue(self.client.interactive)
