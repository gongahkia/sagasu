"""Installation boundaries, compatible entry points and optional dependency failures."""

import importlib.metadata
import subprocess
import sys
import textwrap
import unittest
from unittest.mock import patch


class InstallationModeTests(unittest.TestCase):
    def isolated(self, source):
        completed = subprocess.run(
            [sys.executable, "-I", "-c", textwrap.dedent(source)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_base_metadata_scopes_own_crypto_requirement_to_hosted_extra(self):
        requirements = importlib.metadata.requires("sagasu")
        crypto = [r for r in requirements if r.startswith("cryptography")]
        self.assertEqual(len(crypto), 1)
        self.assertIn('extra == "hosted"', crypto[0])

    def test_local_mcp_works_when_all_hosted_imports_are_blocked(self):
        self.isolated(
            """
            import asyncio
            import importlib.abc
            import sys

            class NoHosted(importlib.abc.MetaPathFinder):
                def find_spec(self, fullname, path=None, target=None):
                    if fullname.startswith("sagasu.hosted") or fullname in {
                        "sagasu.remote", "sagasu.gateway", "sagasu.oauth",
                        "sagasu.saved_sign_in", "sagasu.signin_desk", "sagasu.live_view",
                    }:
                        raise AssertionError("local imported " + fullname)

            sys.meta_path.insert(0, NoHosted())
            import sagasu.cli
            import sagasu.host
            from mcp import Client
            from sagasu.mcp_service import AgentService
            from sagasu.mcp_server import create_server

            async def check():
                service = AgentService()
                try:
                    async with Client(create_server(service)) as client:
                        tools = (await client.list_tools()).tools
                        names = {tool.name for tool in tools}
                        assert {"login", "authentication_status", "task_approval_review"} <= names
                finally:
                    await service.close()
                assert not any(n.startswith("sagasu.hosted") for n in sys.modules)

            asyncio.run(check())
        """
        )

    def test_legacy_hosted_imports_resolve_to_the_same_implementations(self):
        self.isolated(
            """
            import importlib
            for name in ("remote", "gateway", "oauth", "saved_sign_in", "signin_desk", "live_view"):
                assert importlib.import_module("sagasu." + name) is importlib.import_module("sagasu.hosted." + name)
            from sagasu.mcp_server import RemoteAccess as legacy
            from sagasu.hosted.mcp_adapter import RemoteAccess
            assert legacy is RemoteAccess
        """
        )

    def test_missing_hosted_dependency_explains_how_to_install_it(self):
        from sagasu.config import ConfigError
        from sagasu.hosted import require_dependencies
        import builtins

        original = builtins.__import__

        def missing(name, *args, **kwargs):
            if name.startswith("cryptography"):
                raise ImportError("missing optional dependency")
            return original(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=missing):
            with self.assertRaisesRegex(ConfigError, r"sagasu\[hosted\]"):
                require_dependencies()
