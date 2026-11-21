"""Adapter inputs control access and browser authentication without leaking between jobs."""

import asyncio
import unittest

from sagasu.access import AccessPolicy, Caller
from sagasu.auth_context import BROWSER_AUTHENTICATION
from sagasu.fbs_client import FBSClient
from sagasu.mcp_service import AgentError, AgentService
from tests.test_remote import remote_origin


class BrowserPolicyBackend:
    async def run(self, kind, payload, progress, notify):
        client = FBSClient()
        await asyncio.sleep(0)
        return {
            "passive_renewal": client.silent_renewal,
            "interactive": client.interactive,
        }


class AdapterPolicyTests(unittest.IsolatedAsyncioTestCase):
    def service(self, caller):
        service = AgentService(
            backend=BrowserPolicyBackend(), access=AccessPolicy(lambda: caller)
        )
        self.addAsyncCleanup(service.close)
        return service

    async def test_explicit_local_and_hosted_policies_isolate_concurrent_browser_jobs(
        self,
    ):
        local = self.service(Caller())
        hosted = self.service(Caller(remote=True, label="test"))
        with remote_origin():
            local_job = local.start("session")["operation_id"]
        hosted_job = hosted.start("session")["operation_id"]
        local_result, hosted_result = await asyncio.gather(
            local.wait(local_job), hosted.wait(hosted_job)
        )
        self.assertEqual(
            local_result["result"], {"passive_renewal": False, "interactive": True}
        )
        self.assertEqual(
            hosted_result["result"], {"passive_renewal": True, "interactive": False}
        )
        self.assertFalse(local.get(local_job).remote)
        self.assertTrue(hosted.get(hosted_job).remote)
        self.assertIsNone(BROWSER_AUTHENTICATION.get())

    async def test_injected_hosted_policy_cannot_observe_a_local_job(self):
        service = self.service(Caller())
        operation = service.start("session")["operation_id"]
        await service.wait(operation)
        service.access = AccessPolicy(lambda: Caller(remote=True, label="test"))
        self.assertEqual(service.visible_jobs(), [])
        with self.assertRaisesRegex(AgentError, "Unknown or expired"):
            service.get(operation)
