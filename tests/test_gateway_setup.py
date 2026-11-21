"""The deploy side of the shared address (sagasu/gateway.py): sagasu-ctl, install.sh,
the Caddy template, the gateway's unit and command line. No root and no system changes:
sagasu-ctl's paths are moved into a temporary folder and its commands only recorded."""

import asyncio
import configparser
import contextlib
import datetime
import hashlib
import importlib.util
import io
import json
import logging
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

from dotenv import dotenv_values

from sagasu import gateway
from sagasu.console import ContextFilter, EventFormatter, Redact
from sagasu.oauth import PUBLIC_PATHS, OAuthStore
from sagasu.oauth import digest as token_digest
from tests.test_remote import DIGEST
from tests.test_signin_desk import call

KIT = Path(__file__).resolve().parents[1] / "deploy" / "server"
INSTALL = (KIT / "install.sh").read_text(encoding="utf-8")
CADDYFILE = (KIT / "Caddyfile.template").read_text(encoding="utf-8")
CADDYSITE = (KIT / "Caddysite.template").read_text(encoding="utf-8")
UNIT = (KIT / "sagasu-gateway.service").read_text(encoding="utf-8")
BASE = "fbs.example.test"
KEY_TEXT = re.compile(r"sgs_[A-Za-z0-9_-]{40,}")
COPIES = (None, "alice", "charlie")
# gateway_sync as sync_after calls it: no restarts there, warnings shown.
NO_RESTART = {"restart_changed": False, "quiet": False}
# install.sh reads SAGASU_HTTP_OAUTH with GNU sed's \| (the server has it).
GNU_SED = (
    subprocess.run(
        ["sed", "-n", r"s/^X=\(on\|off\)$/\1/p"],
        input="X=off\n",
        capture_output=True,
        text=True,
    ).stdout.strip()
    == "off"
)
# Elsewhere (macOS), the same expressions in sed -E form: \( \) \| become ( ) |.
# install.sh's sed expressions have no other character that -E would change.
SED_SHIM = r"""sed() {
    local args=() arg
    for arg in "$@"; do
        arg=${arg//\\(/(}; arg=${arg//\\)/)}; arg=${arg//\\|/|}
        args+=("$arg")
    done
    command sed -E "${args[@]}"
}"""
PROBE = (
    "curl -s -o /dev/null -w %{http_code} -X POST --unix-socket /run/sagasu-gateway/http.sock"
    f" -H Host: {BASE} http://localhost/mcp"
)


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def load_ctl():
    loader = SourceFileLoader("sagasu_ctl_under_test", str(KIT / "sagasu-ctl"))
    module = importlib.util.module_from_spec(
        importlib.util.spec_from_loader(loader.name, loader)
    )
    loader.exec_module(module)
    return module


def output(function, *args, **kwargs):
    """(result or the SystemExit raised, what was printed)."""
    stream = io.StringIO()
    with contextlib.redirect_stdout(stream):
        try:
            result = function(*args, **kwargs)
        except SystemExit as stop:
            result = stop
    return result, stream.getvalue()


def shown(when):
    return datetime.datetime.fromtimestamp(when).strftime("%d %b %H:%M")


class CtlCase(unittest.TestCase):
    """sagasu-ctl in a temporary root, like the live server: the owner's copy
    (keys laptop, phone) and teammates' copies alice and charlie (key main)."""

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="sagasu-ctl-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        root = str(self.root)
        self.ctl = ctl = load_ctl()
        self.default_routes = ctl.GATEWAY_ROUTES
        (self.root / "etc/sagasu/instances").mkdir(parents=True)
        ctl.OWNER_ENV = f"{root}/etc/sagasu/sagasu.env"
        ctl.INSTANCES = f"{root}/etc/sagasu/instances"
        ctl.GATEWAY_ROUTES = f"{root}/etc/sagasu/gateway-routes.json"
        ctl.GATEWAY_SETTINGS = f"{root}/etc/sagasu/gateway.env"
        ctl.GATEWAY_STORE = f"{root}/var/lib/sagasu-gateway/oauth.sqlite3"
        ctl.SITES = f"{root}/etc/caddy/sites"
        ctl.SSH_KEYS = f"{root}/etc/ssh/sagasu_keys"
        ctl.KIT = str(KIT)

        def moved(path):
            return path if path.startswith(root) else root + path

        class Copy(ctl.Copy):
            def __init__(self, name=None):
                super().__init__(name)
                self.env, self.state = moved(self.env), moved(self.state)
                self.sign_in_key = moved(self.sign_in_key)
                self.oauth_store = f"{self.state}/oauth.sqlite3"
                self.sign_in_state = [moved(path) for path in self.sign_in_state]

        ctl.Copy = Copy
        # Whoever runs the tests is not a teammate's login.
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("SUDO_USER", None)
        self.accounts = {"sagasu", "sagasu-gateway"}
        self.calls, self.chowns, self.on_run = [], {}, None
        self.gateway_active = True
        ctl.pwd = types.SimpleNamespace(getpwnam=self.getpwnam)
        ctl.subprocess = types.SimpleNamespace(run=self.fake_run)
        ctl.shutil = types.SimpleNamespace(
            chown=lambda *a, **k: None, rmtree=shutil.rmtree
        )
        self.keys = {}  # (copy, label) -> the person's key
        self.add_copy(None, ["laptop", "phone"])
        self.add_copy("alice", ["main"])
        self.add_copy("charlie", ["main"])

    def getpwnam(self, name):
        if name not in self.accounts:
            raise KeyError(name)
        return types.SimpleNamespace(pw_name=name, pw_uid=None)

    def fake_run(self, command, **options):
        command = list(command)
        returncode = 0
        if command[0] == "chown":
            self.chowns[command[2].removesuffix(".new")] = command[1]
        else:
            self.calls.append(command)
        if command[0] == "useradd":
            self.accounts.add(command[-1])
        elif command[0] == "userdel":
            self.accounts.discard(command[-1])
        elif (
            command[:3] == ["systemctl", "is-active", "--quiet"]
            and not self.gateway_active
        ):
            returncode = 3
        if self.on_run:
            self.on_run(command)
        return types.SimpleNamespace(
            returncode=returncode, stdout="active\n", stderr=""
        )

    def add_copy(self, name, labels, extra=(), oauth="on"):
        """A copy's settings and account; labels are names, or (label, key)
        pairs. oauth=None leaves the SAGASU_HTTP_OAUTH line out."""
        copy_name, entries = name or "sagasu", []
        for item in labels:
            label, key = (
                item
                if isinstance(item, tuple)
                else (item, f"sgs_{copy_name}_{item}_{'k' * 40}")
            )
            self.keys[(copy_name, label)] = key
            entries.append(f"{label}:{sha(key)}")
        host = f"{name}.{BASE}" if name else BASE
        socket_path = (
            f"/run/sagasu-{name}/http.sock" if name else "/run/sagasu/http.sock"
        )
        lines = [
            f"# Sagasu copy {copy_name}",
            "SAGASU_BROWSER_CHANNEL=chromium",
            f"SAGASU_HTTP_SOCKET={socket_path}",
            f"SAGASU_HTTP_ALLOWED_HOSTS={host}",
            "SAGASU_HTTP_KEYS=" + ",".join(entries),
            *([f"SAGASU_HTTP_OAUTH={oauth}"] if oauth else []),
            *extra,
        ]
        Path(self.ctl.Copy(name).env).write_text(
            "\n".join(lines) + "\n", encoding="utf-8"
        )
        self.accounts.add(f"sagasu-{name}" if name else "sagasu")
        instance = Path(self.ctl.INSTANCES, name or "")
        if name and not instance.exists():
            index = len(self.ctl.instance_names()) + 1
            instance.write_text(
                f"INDEX={index}\nDISPLAY_NUM={99 + index}\nVNC_PORT={5910 + index}\n"
                f"WS_PORT={6090 + index}\nHOST={host}\n"
            )

    def break_settings(self, name):
        """A hand edit that sagasu-ctl refuses to read."""
        path = Path(self.ctl.Copy(name).env)
        path.write_text(
            re.sub(
                r"^SAGASU_HTTP_KEYS=.*$",
                "SAGASU_HTTP_KEYS=main:not-a-digest",
                path.read_text(),
                flags=re.M,
            )
        )

    def env_keys(self, name):
        """[(label, digest)] in a copy's SAGASU_HTTP_KEYS, in order."""
        text = Path(self.ctl.Copy(name).env).read_text(encoding="utf-8")
        found = re.findall(r"^SAGASU_HTTP_KEYS=(.*)$", text, re.M)
        self.assertEqual(len(found), 1)
        return [tuple(item.split(":", 1)) for item in found[0].split(",") if item]

    def labels(self, name):
        return [label for label, _ in self.env_keys(name)]

    def routes(self):
        return json.loads(Path(self.ctl.GATEWAY_ROUTES).read_text(encoding="utf-8"))

    def snapshot(self):
        return {
            path: path.read_bytes()
            for path in sorted(self.root.rglob("*"))
            if path.is_file()
        }

    def sync(self, **options):
        return output(self.ctl.gateway_sync, **options)

    def restarts(self):
        return [
            command[2]
            for command in self.calls
            if command[:2] == ["systemctl", "restart"]
        ]

    def spy_sync(self):
        """[(options, copies restarted before it ran)] for each gateway_sync call."""
        real, seen = self.ctl.gateway_sync, []

        def spy(**options):
            seen.append((options, self.restarts()))
            return real(**options)

        self.ctl.gateway_sync = spy
        return seen

    def gateway_settings(self, oauth="on"):
        Path(self.ctl.GATEWAY_SETTINGS).write_text(
            f"# The gateway: one address for every copy (sagasu/gateway.py).\n"
            f"SAGASU_GATEWAY_HOST={BASE}\nSAGASU_GATEWAY_OAUTH={oauth}\n"
        )


class GatewaySyncTests(CtlCase):
    def test_without_the_gateway_account_nothing_is_written(self):
        self.accounts.discard("sagasu-gateway")
        before = self.snapshot()
        self.assertEqual(self.sync()[0], [])
        self.assertFalse(os.path.exists(self.ctl.GATEWAY_ROUTES))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual((self.calls, self.chowns), ([], {}))

    def test_every_copy_gets_one_gateway_key_whose_text_is_only_in_the_table(self):
        people = {name: self.env_keys(name) for name in COPIES}
        changed, printed = self.sync(restart_changed=False)
        self.assertEqual(printed, "")
        self.assertEqual(
            sorted(copy.name or "" for copy in changed), ["", "alice", "charlie"]
        )
        self.assertEqual(self.restarts(), [])
        table = self.routes()
        self.assertEqual(
            (table["version"], set(table["copies"])),
            (1, {"sagasu", "alice", "charlie"}),
        )
        settings = "".join(Path(self.ctl.Copy(name).env).read_text() for name in COPIES)
        gateway_keys = set()
        for name in COPIES:
            entries = self.env_keys(name)
            gateway_digests = [
                digest for label, digest in entries if label == "gateway"
            ]
            copy = table["copies"][name or "sagasu"]
            key = copy["key"]
            self.assertRegex(key, KEY_TEXT)
            self.assertIs(copy["oauth"], True)
            self.assertEqual(gateway_digests, [sha(key)])
            self.assertNotIn(key, settings)
            # The people's keys are kept exactly as they were.
            self.assertEqual(
                [entry for entry in entries if entry[0] != "gateway"], people[name]
            )
            gateway_keys.add(key)
        self.assertEqual(len(gateway_keys), 3)
        # Settings keep their owners; the table is root:sagasu-gateway 0640.
        self.assertEqual(
            self.chowns,
            {
                self.ctl.OWNER_ENV: "root:sagasu",
                self.ctl.Copy("alice").env: "root:sagasu-alice",
                self.ctl.Copy("charlie").env: "root:sagasu-charlie",
                self.ctl.GATEWAY_ROUTES: "root:sagasu-gateway",
            },
        )
        self.assertEqual(os.stat(self.ctl.GATEWAY_ROUTES).st_mode & 0o777, 0o640)
        text = Path(self.ctl.GATEWAY_ROUTES).read_text()
        for key in self.keys.values():
            self.assertNotIn(key, text)  # no person's key, only digests

    def test_an_existing_gateway_key_is_kept_without_a_restart(self):
        self.sync(restart_changed=False)
        first, before = self.routes(), self.snapshot()
        self.calls.clear()
        self.chowns.clear()
        changed, _ = self.sync()
        self.assertEqual((changed, self.restarts()), ([], []))
        self.assertEqual(self.routes(), first)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(
            list(self.chowns), [self.ctl.GATEWAY_ROUTES]
        )  # settings untouched

    def test_a_lost_table_keeps_the_gateway_key_until_rotate_and_never_adds_a_second(
        self,
    ):
        self.sync(restart_changed=False)
        old = self.routes()["copies"]
        os.remove(self.ctl.GATEWAY_ROUTES)
        settings = {name: Path(self.ctl.Copy(name).env).read_bytes() for name in COPIES}
        # A "gateway" key the table does not know is never replaced on its own.
        changed, printed = self.sync(restart_changed=False)
        self.assertEqual(changed, [])
        table = self.routes()
        for name in COPIES:
            self.assertIn(
                f"{name or 'sagasu'}: has a key labelled 'gateway' that is not the gateway's",
                printed,
            )
            self.assertEqual(Path(self.ctl.Copy(name).env).read_bytes(), settings[name])
            copy = table["copies"][name or "sagasu"]
            self.assertEqual((copy["key"], copy["oauth"]), (None, False))
        # The people's keys are still routed.
        expected = {
            sha(key): {"copy": copy, "label": label}
            for (copy, label), key in self.keys.items()
        }
        self.assertEqual(table["keys"], expected)
        # --rotate replaces it: still one gateway key per copy.
        self.assertEqual(len(self.sync(restart_changed=False, rotate=True)[0]), 3)
        new = self.routes()["copies"]
        for name in COPIES:
            digests = [
                digest for label, digest in self.env_keys(name) if label == "gateway"
            ]
            self.assertEqual(digests, [sha(new[name or "sagasu"]["key"])])
            self.assertNotEqual(
                new[name or "sagasu"]["key"], old[name or "sagasu"]["key"]
            )
            self.assertIs(new[name or "sagasu"]["oauth"], True)

    def test_a_gateway_label_the_table_does_not_know_is_kept_until_rotate(self):
        person = "sgs_kim_person_" + "q" * 40  # from before the label was reserved
        self.add_copy("kim", ["main", ("gateway", person)])
        self.keys.pop(("kim", "gateway"))
        before = Path(self.ctl.Copy("kim").env).read_bytes()
        changed, printed = self.sync(restart_changed=False)
        self.assertNotIn("kim", [copy.name for copy in changed])
        self.assertIn(
            "kim: has a key labelled 'gateway' that is not the gateway's", printed
        )
        self.assertIn("gateway-sync --rotate", printed)
        self.assertEqual(Path(self.ctl.Copy("kim").env).read_bytes(), before)
        table = self.routes()
        kim = table["copies"]["kim"]
        self.assertEqual(
            (kim["socket"], kim["host"], kim["key"], kim["oauth"]),
            ("/run/sagasu-kim/http.sock", f"kim.{BASE}", None, False),
        )
        self.assertEqual(
            table["keys"][sha(self.keys[("kim", "main")])],
            {"copy": "kim", "label": "main"},
        )
        self.assertNotIn(
            sha(person), table["keys"]
        )  # a "gateway" label is never routed
        # Run again: still kept.
        self.assertEqual(self.sync(restart_changed=False)[0], [])
        self.assertEqual(Path(self.ctl.Copy("kim").env).read_bytes(), before)
        # --rotate puts the gateway's own key in its place.
        changed, _ = self.sync(restart_changed=False, rotate=True)
        self.assertIn("kim", [copy.name for copy in changed])
        kim = self.routes()["copies"]["kim"]
        self.assertEqual(self.labels("kim"), ["main", "gateway"])
        self.assertEqual(dict(self.env_keys("kim"))["gateway"], sha(kim["key"]))
        self.assertIs(kim["oauth"], True)

    def test_the_table_names_each_copy_and_every_other_key(self):
        self.add_copy("hana", [("setup", "sgs_placeholder_" + "p" * 40), "main"])
        setup = self.keys.pop(("hana", "setup"))
        self.sync(restart_changed=False)
        table = self.routes()
        for name in ("sagasu", "alice", "charlie", "hana"):
            self.assertEqual(
                set(table["copies"][name]), {"socket", "host", "key", "oauth"}
            )
            self.assertIs(table["copies"][name]["oauth"], True)
        owner, alice = table["copies"]["sagasu"], table["copies"]["alice"]
        self.assertEqual(
            (owner["socket"], owner["host"]), ("/run/sagasu/http.sock", BASE)
        )
        self.assertEqual(
            (alice["socket"], alice["host"]),
            ("/run/sagasu-alice/http.sock", f"alice.{BASE}"),
        )
        expected = {
            sha(key): {"copy": copy, "label": label}
            for (copy, label), key in self.keys.items()
        }
        self.assertEqual(table["keys"], expected)
        self.assertNotIn(sha(setup), table["keys"])
        for copy in table["copies"].values():
            self.assertNotIn(sha(copy["key"]), table["keys"])
        # The gateway reads the table as sagasu-ctl writes it.
        routes = gateway.Routes(self.ctl.GATEWAY_ROUTES)
        self.assertEqual(routes.copies, table["copies"])
        labels = {k: f"{o['copy']}/{o['label']}" for k, o in expected.items()}
        self.assertEqual(routes.keys, labels)
        self.assertEqual(routes.oauth_keys, labels)  # every copy allows OAuth
        self.assertEqual(
            routes.digests, {label: digest for digest, label in labels.items()}
        )

    def test_apps_sign_in_here_for_a_copy_only_with_its_oauth_on(self):
        self.add_copy("kim", ["main"], oauth="off")
        self.add_copy("lee", ["main"], oauth=None)  # never set: off
        self.add_copy(
            "max", ["main"], extra=["SAGASU_HTTP_OAUTH=off"]
        )  # the last line counts
        self.sync(restart_changed=False)
        copies = self.routes()["copies"]
        self.assertEqual(
            {name: copy["oauth"] for name, copy in copies.items()},
            {
                "sagasu": True,
                "alice": True,
                "charlie": True,
                "kim": False,
                "lee": False,
                "max": False,
            },
        )
        routes = gateway.Routes(self.ctl.GATEWAY_ROUTES)
        off = {sha(self.keys[(name, "main")]) for name in ("kim", "lee", "max")}
        # Their keys are still routed; apps cannot sign in with them here.
        self.assertLessEqual(off, set(routes.keys))
        self.assertEqual(set(routes.oauth_keys), set(routes.keys) - off)
        self.assertEqual(
            {routes.keys[digest] for digest in off},
            {"kim/main", "lee/main", "max/main"},
        )

    def test_a_key_on_two_copies_is_routed_to_neither(self):
        shared = "sgs_shared_" + "s" * 40
        self.add_copy("kim", ["main", ("shared", shared)])
        self.add_copy("lee", ["main", ("also", shared)])
        _, printed = self.sync(restart_changed=False)
        self.assertIn("A key is configured on several copies (kim, lee)", printed)
        keys = self.routes()["keys"]
        self.assertNotIn(sha(shared), keys)
        self.assertEqual(
            keys[sha(self.keys[("kim", "main")])], {"copy": "kim", "label": "main"}
        )
        self.assertEqual(
            keys[sha(self.keys[("lee", "main")])], {"copy": "lee", "label": "main"}
        )
        self.assertNotIn(sha(shared), gateway.Routes(self.ctl.GATEWAY_ROUTES).keys)

    def test_a_copy_with_sixteen_keys_stays_routed_without_app_sign_in(self):
        self.add_copy("hana", [f"k{number}" for number in range(16)])
        self.add_copy("ivan", [f"k{number}" for number in range(15)])
        hana = Path(self.ctl.Copy("hana").env).read_bytes()
        changed, printed = self.sync(restart_changed=False)
        self.assertIn("hana: 16 keys already", printed)
        self.assertNotIn("hana", [copy.name for copy in changed])
        self.assertEqual(Path(self.ctl.Copy("hana").env).read_bytes(), hana)
        table = self.routes()
        self.assertEqual(
            table["copies"]["hana"],
            {
                "socket": "/run/sagasu-hana/http.sock",
                "host": f"hana.{BASE}",
                "key": None,
                "oauth": False,
            },
        )
        people = {sha(self.keys[("hana", f"k{number}")]) for number in range(16)}
        self.assertEqual(
            {
                digest
                for digest, owner in table["keys"].items()
                if owner["copy"] == "hana"
            },
            people,
        )
        routes = gateway.Routes(self.ctl.GATEWAY_ROUTES)
        self.assertLessEqual(people, set(routes.keys))
        self.assertFalse(people & set(routes.oauth_keys))
        # Fifteen keys leave room for the gateway's, and it stays at sixteen.
        self.assertEqual(len(self.env_keys("ivan")), 16)
        self.assertRegex(table["copies"]["ivan"]["key"], KEY_TEXT)
        self.assertIs(table["copies"]["ivan"]["oauth"], True)
        changed, _ = self.sync()
        self.assertEqual((changed, self.restarts()), ([], []))
        self.assertEqual(self.routes()["copies"]["ivan"], table["copies"]["ivan"])
        self.assertEqual(self.routes()["copies"]["hana"]["key"], None)

    def test_a_copy_that_cannot_be_read_is_left_off_with_a_warning(self):
        self.break_settings("alice")
        self.accounts.discard("sagasu-charlie")  # its account is missing
        alice = Path(self.ctl.Copy("alice").env).read_bytes()
        changed, printed = self.sync(restart_changed=False)
        self.assertNotIsInstance(changed, SystemExit)
        self.assertEqual([copy.name for copy in changed], [None])
        self.assertIn("alice: settings unreadable", printed)
        self.assertIn("charlie: settings unreadable", printed)
        table = self.routes()
        self.assertEqual(set(table["copies"]), {"sagasu"})
        self.assertEqual(
            {owner["copy"] for owner in table["keys"].values()}, {"sagasu"}
        )
        self.assertEqual(Path(self.ctl.Copy("alice").env).read_bytes(), alice)
        self.assertEqual(self.labels("charlie"), ["main"])  # never given a key

    def test_strict_refuses_to_write_a_table_without_the_owner(self):
        self.sync(restart_changed=False)
        before = Path(self.ctl.GATEWAY_ROUTES).read_bytes()
        self.break_settings(None)
        with self.assertRaises(self.ctl.GatewaySyncError):
            output(self.ctl.gateway_sync, restart_changed=False, strict=True)
        self.assertEqual(Path(self.ctl.GATEWAY_ROUTES).read_bytes(), before)
        # As install.sh runs it: the command fails, saying why.
        with mock.patch.object(self.ctl.os, "geteuid", return_value=0):
            refused, printed = output(
                self.ctl.main, ["gateway-sync", "--no-restart", "--strict"]
            )
        self.assertIsInstance(refused, SystemExit)
        self.assertIn("owner's copy", str(refused))
        self.assertIn("sagasu: settings unreadable", printed)
        self.assertEqual(Path(self.ctl.GATEWAY_ROUTES).read_bytes(), before)
        # Without strict, the other copies are still written.
        self.sync(restart_changed=False)
        self.assertEqual(set(self.routes()["copies"]), {"alice", "charlie"})
        # And on a server with no table yet, none is written.
        os.remove(self.ctl.GATEWAY_ROUTES)
        with self.assertRaises(self.ctl.GatewaySyncError):
            output(self.ctl.gateway_sync, restart_changed=False, strict=True)
        self.assertFalse(os.path.exists(self.ctl.GATEWAY_ROUTES))

    def test_the_gateway_keeps_its_last_table_when_no_copy_can_be_read(self):
        self.sync(restart_changed=False)
        routes = gateway.Routes(self.ctl.GATEWAY_ROUTES)
        good = (dict(routes.copies), dict(routes.keys), dict(routes.oauth_keys))
        self.assertEqual(len(good[2]), 4)
        for name in COPIES:
            self.break_settings(name)
        self.sync(restart_changed=False)
        self.assertEqual(self.routes()["copies"], {})
        # Zero copies reads as unreadable: never as "every key removed".
        routes.reload(force=True)
        self.assertEqual((routes.copies, routes.keys, routes.oauth_keys), good)
        with self.assertRaises(SystemExit):
            gateway.build(
                {
                    "SAGASU_GATEWAY_HOST": BASE,
                    "SAGASU_GATEWAY_ROUTES": self.ctl.GATEWAY_ROUTES,
                    "SAGASU_GATEWAY_OAUTH": "off",
                }
            )

    def test_only_changed_copies_restart_unless_told_not_to(self):
        with (
            mock.patch.object(self.ctl.os, "geteuid", return_value=0),
            mock.patch.dict(os.environ, {"SUDO_USER": "owner"}),
        ):
            _, printed = output(self.ctl.main, ["gateway-sync", "--no-restart"])
            self.assertIn("gave 3 copy(ies) a new gateway key", printed)
            self.assertEqual(self.restarts(), [])
            self.add_copy("zara", ["main"])
            output(self.ctl.main, ["gateway-sync"])
            self.assertEqual(self.restarts(), ["sagasu@zara"])
            os.environ["SUDO_USER"] = "fbs-alice"  # a teammate's own login
            self.add_copy("yuki", ["main"])
            refused, _ = output(self.ctl.main, ["gateway-sync"])
        self.assertIn("Only the server's owner", str(refused))
        self.assertNotIn("yuki", self.routes()["copies"])

    def test_rotate_gives_every_copy_a_new_gateway_key_and_restarts_it(self):
        self.sync(restart_changed=False)
        old = self.routes()["copies"]
        with mock.patch.object(self.ctl.os, "geteuid", return_value=0):
            _, printed = output(self.ctl.main, ["gateway-sync", "--rotate"])
            self.assertIn("gave 3 copy(ies) a new gateway key", printed)
            self.assertEqual(
                self.restarts(), ["sagasu", "sagasu@alice", "sagasu@charlie"]
            )
            new = self.routes()["copies"]
            for name in COPIES:
                self.assertNotEqual(
                    new[name or "sagasu"]["key"], old[name or "sagasu"]["key"]
                )
                self.assertEqual(
                    [
                        digest
                        for label, digest in self.env_keys(name)
                        if label == "gateway"
                    ],
                    [sha(new[name or "sagasu"]["key"])],
                )
            # An option it does not know changes nothing.
            before = self.snapshot()
            refused, _ = output(self.ctl.main, ["gateway-sync", "--force"])
        self.assertIsInstance(refused, SystemExit)
        self.assertEqual(self.snapshot(), before)

    # Regression (fixed): gateway_sync skipped the label "setup" on every copy,
    # but "setup" is a reserved placeholder only on teammates' copies: the
    # owner may hold (and key-add, and status lists) a key labelled "setup".
    def test_an_owner_key_labelled_setup_keeps_working_at_the_shared_address(self):
        self.add_copy(None, ["laptop", "setup"])
        self.sync(restart_changed=False)
        found = self.routes()["keys"].get(sha(self.keys[("sagasu", "setup")]))
        self.assertEqual(found, {"copy": "sagasu", "label": "setup"})


class SyncAfterTests(CtlCase):
    def test_other_copies_given_a_gateway_key_restart_but_not_this_one(self):
        seen = self.spy_sync()
        result, _ = output(self.ctl.sync_after, self.ctl.Copy("alice"))
        self.assertIsNone(result)
        self.assertEqual(seen, [(NO_RESTART, [])])
        self.assertEqual(self.restarts(), ["sagasu", "sagasu@charlie"])
        self.assertEqual(self.labels("alice"), ["main", "gateway"])
        self.assertIn("alice", self.routes()["copies"])

    def test_without_a_copy_every_changed_copy_restarts(self):
        output(self.ctl.sync_after)
        self.assertEqual(self.restarts(), ["sagasu", "sagasu@alice", "sagasu@charlie"])
        output(self.ctl.sync_after)
        self.assertEqual(len(self.restarts()), 3)  # nothing changed the second time

    def test_a_failed_refresh_never_fails_the_command(self):
        for error in (
            RuntimeError("disk full"),
            OSError("read-only"),
            self.ctl.GatewaySyncError("no owner"),
        ):
            with self.subTest(error=error):
                self.ctl.gateway_sync = mock.Mock(side_effect=error)
                result, printed = output(self.ctl.sync_after, self.ctl.Copy("alice"))
                self.assertIsNone(result)
                self.assertEqual(
                    printed,
                    "Warning: the shared address's routing table was not refreshed"
                    f" ({error}); run sudo sagasu-ctl gateway-sync.\n",
                )
                self.ctl.gateway_sync.assert_called_once_with(
                    restart_changed=False, quiet=False
                )
        self.assertEqual(self.restarts(), [])

    # Regression (fixed): sync_after catches `Exception`, which does not include SystemExit
    # (a BaseException), although its comment says "SystemExit included". A
    # sys.exit in gateway_sync (the way sagasu-ctl reports most problems)
    # would end key-add or instance-add after their real work was done.
    def test_an_exit_in_the_refresh_never_fails_the_command(self):
        self.ctl.gateway_sync = mock.Mock(
            side_effect=SystemExit("Correct it by hand first.")
        )
        result, printed = output(self.ctl.sync_after, self.ctl.Copy("alice"))
        self.assertIsNone(result)
        self.assertIn(
            "Warning: the shared address's routing table was not refreshed", printed
        )

    def test_a_teammate_sees_nothing_of_other_copies(self):
        self.break_settings("charlie")
        real, seen = self.ctl.gateway_sync, self.spy_sync()
        with mock.patch.dict(os.environ, {"SUDO_USER": "fbs-alice"}):
            _, printed = output(self.ctl.sync_after, self.ctl.Copy("alice"))
            self.assertEqual(seen, [({"restart_changed": False, "quiet": True}, [])])
            self.assertEqual(printed, "")
            self.assertIn("alice", self.routes()["copies"])
            # A failure says only that it happened.
            self.ctl.gateway_sync = mock.Mock(
                side_effect=OSError(f"{self.ctl.GATEWAY_ROUTES}: charlie")
            )
            _, printed = output(self.ctl.sync_after, self.ctl.Copy("alice"))
        self.assertEqual(
            printed, "Warning: the shared address's routing table was not refreshed.\n"
        )
        # The owner sees the warnings.
        self.ctl.gateway_sync = real
        _, printed = output(self.ctl.sync_after, self.ctl.Copy("alice"))
        self.assertIn("charlie: settings unreadable", printed)


class KeyCommandTests(CtlCase):
    def setUp(self):
        super().setUp()
        self.sync(restart_changed=False)
        self.calls.clear()
        self.first = self.routes()

    def command(self, name, *arguments):
        return output(self.ctl.copy_command, self.ctl.Copy(name), list(arguments))

    def watch_restarts(self, name):
        """[(printed so far, the copy's keys)] at each restart."""
        found = []
        self.on_run = lambda command: (
            found.append((sys.stdout.getvalue(), dict(self.env_keys(name))))
            if command[:2] == ["systemctl", "restart"]
            else None
        )
        return found

    def test_key_add_refuses_the_gateway_label(self):
        seen, before = self.spy_sync(), self.snapshot()
        for name in (None, "alice"):
            refused, _ = self.command(name, "key-add", "gateway")
            self.assertIsInstance(refused, SystemExit)
            self.assertIn("reserved", str(refused))
        self.assertEqual((self.snapshot(), seen, self.calls), (before, [], []))

    def test_key_add_saves_shows_restarts_then_routes_the_new_key(self):
        seen, at_restart = self.spy_sync(), self.watch_restarts("alice")
        _, printed = self.command("alice", "key-add", "tablet")
        key = KEY_TEXT.search(printed).group()
        self.assertEqual(dict(self.env_keys("alice"))["tablet"], sha(key))
        # Saved and shown before the restart; the table refreshed after it.
        self.assertEqual(len(at_restart), 1)
        self.assertIn(key, at_restart[0][0])
        self.assertEqual(at_restart[0][1]["tablet"], sha(key))
        self.assertEqual(
            (seen, self.restarts()),
            ([(NO_RESTART, ["sagasu@alice"])], ["sagasu@alice"]),
        )
        table = self.routes()
        self.assertEqual(table["keys"][sha(key)], {"copy": "alice", "label": "tablet"})
        self.assertEqual(table["copies"], self.first["copies"])  # gateway keys kept

    def test_key_remove_refuses_the_gateway_key(self):
        before = self.snapshot()
        refused, _ = self.command(None, "key-remove", "gateway")
        self.assertIn("No key has that label", str(refused))
        self.assertEqual(self.snapshot(), before)

    def test_the_only_key_rule_does_not_count_the_gateway_key(self):
        before = self.snapshot()
        refused, _ = self.command("alice", "key-remove", "main")
        self.assertIn("This is the only key", str(refused))
        self.assertEqual((self.snapshot(), self.restarts()), (before, []))
        seen, at_restart = self.spy_sync(), self.watch_restarts(None)
        _, printed = self.command(None, "key-remove", "phone")
        self.assertIn("no longer works", printed)
        self.assertEqual(self.labels(None), ["laptop", "gateway"])
        # Removed and said before the restart; the table refreshed after it.
        self.assertEqual(len(at_restart), 1)
        self.assertIn("API key 'phone' no longer works.", at_restart[0][0])
        self.assertNotIn("phone", at_restart[0][1])
        table = self.routes()
        self.assertNotIn(sha(self.keys[("sagasu", "phone")]), table["keys"])
        self.assertIn(sha(self.keys[("sagasu", "laptop")]), table["keys"])
        self.assertEqual(table["copies"], self.first["copies"])
        self.assertEqual(
            (seen, self.restarts()), ([(NO_RESTART, ["sagasu"])], ["sagasu"])
        )

    def test_oauth_off_and_on_reach_the_shared_address(self):
        seen = self.spy_sync()
        main = sha(self.keys[("alice", "main")])
        _, printed = self.command("alice", "oauth", "off")
        self.assertIn("OAuth sign-in is now off.", printed)
        self.assertIn(
            "SAGASU_HTTP_OAUTH=off",
            Path(self.ctl.Copy("alice").env).read_text().splitlines(),
        )
        alice = self.routes()["copies"]["alice"]
        self.assertEqual(alice, {**self.first["copies"]["alice"], "oauth": False})
        routes = gateway.Routes(self.ctl.GATEWAY_ROUTES)
        self.assertEqual(routes.keys[main], "alice/main")  # the key still works
        self.assertNotIn(
            main, routes.oauth_keys
        )  # apps signed in with it are signed out
        self.assertEqual(seen, [(NO_RESTART, ["sagasu@alice"])])
        self.command("alice", "oauth", "on")
        self.assertEqual(
            self.routes()["copies"]["alice"], self.first["copies"]["alice"]
        )
        self.assertEqual(self.restarts(), ["sagasu@alice", "sagasu@alice"])
        self.assertEqual([options for options, _ in seen], [NO_RESTART, NO_RESTART])

    def test_status_hides_the_gateway_key(self):
        self.add_copy("hana", [("setup", "sgs_placeholder_" + "p" * 40), "main"])
        self.sync(restart_changed=False)
        for name, listed in (
            (None, "laptop, phone"),
            ("alice", "main"),
            ("hana", "main"),
        ):
            _, printed = self.command(name, "status")
            line = next(
                line for line in printed.splitlines() if line.startswith("api keys")
            )
            self.assertEqual(line[20:].strip(), listed)
            self.assertNotIn("gateway", printed)

    # Regression (fixed): key-add saved the new key's digest and only then ran
    # gateway_sync, which exited on any other copy's hand-edited settings
    # ("Correct it by hand first"). The key was then never shown and its copy
    # was not restarted.
    def test_a_broken_teammate_copy_does_not_lose_a_new_key(self):
        self.break_settings("alice")
        _, printed = self.command(None, "key-add", "tablet")
        saved = dict(self.env_keys(None))
        self.assertIn("tablet", saved)
        found = KEY_TEXT.search(printed)
        self.assertIsNotNone(found, "the new key was saved but never shown")
        self.assertEqual(sha(found.group()), saved["tablet"])
        self.assertEqual(self.restarts(), ["sagasu"])
        self.assertIn("alice: settings unreadable", printed)
        self.assertEqual(
            self.routes()["keys"][saved["tablet"]],
            {"copy": "sagasu", "label": "tablet"},
        )

    # Regression (fixed): key-remove deleted the digest and only then ran
    # gateway_sync; when that exited on another copy's settings the copy was
    # not restarted, so the removed key kept working.
    def test_a_broken_teammate_copy_does_not_leave_a_removed_key_working(self):
        self.break_settings("alice")
        self.command(None, "key-remove", "phone")
        self.assertNotIn("phone", self.labels(None))
        self.assertEqual(self.restarts(), ["sagasu"])
        self.assertNotIn(sha(self.keys[("sagasu", "phone")]), self.routes()["keys"])

    def test_a_table_that_cannot_be_written_does_not_lose_a_new_key(self):
        self.ctl.GATEWAY_ROUTES = str(self.root / "missing" / "gateway-routes.json")
        result, printed = self.command("alice", "key-add", "tablet")
        self.assertNotIsInstance(result, SystemExit)
        key = KEY_TEXT.search(printed).group()
        self.assertEqual(dict(self.env_keys("alice"))["tablet"], sha(key))
        self.assertEqual(self.restarts(), ["sagasu@alice"])
        self.assertIn(
            "Warning: the shared address's routing table was not refreshed (", printed
        )
        self.assertIn("run sudo sagasu-ctl gateway-sync.", printed)


class ConnectionTests(CtlCase):
    def setUp(self):
        super().setUp()
        self.sync(restart_changed=False)
        self.now = time.time()

    def grant(self, store_path, key_digest, name, host, created, live=True):
        store = OAuthStore(store_path)
        try:
            client = f"app-{created}"
            store.add_client(client, json.dumps({"client_id": client}), created)
            grant = store.add_grant(client, key_digest, ["sagasu"], created, name, host)
            store.add_token(
                token_digest(f"refresh-{created}"),
                grant,
                "refresh",
                self.now + (86400 if live else -60),
            )
        finally:
            store.close()

    def test_connections_list_both_addresses_each_copy_only_its_own(self):
        own, shared = self.ctl.Copy().oauth_store, self.ctl.GATEWAY_STORE
        laptop, phone = sha(self.keys[("sagasu", "laptop")]), sha(
            self.keys[("sagasu", "phone")]
        )
        main = sha(self.keys[("alice", "main")])
        gateway_key = dict(self.env_keys("alice"))["gateway"]
        times = [self.now - 600 + 60 * number for number in range(7)]
        self.grant(own, laptop, "ChatGPT", "chatgpt.com", times[0])
        self.grant(
            own, "0" * 64, "Old app", "chatgpt.com", times[1]
        )  # its key was removed
        self.grant(own, phone, "Expired", "claude.ai", times[2], live=False)
        self.grant(shared, phone, "Claude", "claude.ai", times[3])
        self.grant(shared, main, "Claude", "claude.ai", times[4])
        self.grant(
            shared, "1" * 64, "Someone's", "claude.ai", times[5]
        )  # no copy's key
        self.grant(shared, gateway_key, "Gateway", "claude.ai", times[6])
        self.assertEqual(
            self.ctl.connections(self.ctl.Copy()),
            [
                ("laptop", "ChatGPT", "chatgpt.com", shown(times[0])),
                ("(key removed)", "Old app", "chatgpt.com", shown(times[1])),
                ("phone", "Claude (shared address)", "claude.ai", shown(times[3])),
            ],
        )
        self.assertEqual(
            self.ctl.connections(self.ctl.Copy("alice")),
            [
                ("main", "Claude (shared address)", "claude.ai", shown(times[4])),
            ],
        )
        self.assertEqual(self.ctl.connections(self.ctl.Copy("charlie")), [])
        _, printed = output(
            self.ctl.copy_command, self.ctl.Copy("alice"), ["connections"]
        )
        self.assertIn("Claude (shared address)", printed)
        self.assertNotIn("ChatGPT", printed)
        _, printed = output(self.ctl.copy_command, self.ctl.Copy("alice"), ["status"])
        self.assertIn("oauth sign-in        on (1 app(s) signed in)", printed)


class InstanceCommandTests(CtlCase):
    def test_instance_add_gives_the_shared_address_and_routes_the_key_first(self):
        self.sync(restart_changed=False)
        self.gateway_settings("on")
        self.calls.clear()
        seen, at_start = self.spy_sync(), {}
        self.on_run = lambda command: (
            at_start.update(self.routes())
            if command[:3] == ["systemctl", "enable", "--now"]
            else None
        )
        _, printed = output(self.ctl.instance_add, "zara")
        self.assertIn(f"Address:  https://{BASE}/mcp", printed)
        self.assertNotIn(f"zara.{BASE}/mcp", printed)
        key = KEY_TEXT.search(printed).group()
        self.assertEqual(at_start["keys"][sha(key)], {"copy": "zara", "label": "main"})
        zara = at_start["copies"]["zara"]
        self.assertEqual(
            (zara["socket"], zara["host"]),
            ("/run/sagasu-zara/http.sock", f"zara.{BASE}"),
        )
        self.assertIs(zara["oauth"], True)
        self.assertEqual(self.labels("zara"), ["main", "gateway"])
        self.assertEqual((seen, self.restarts()), ([(NO_RESTART, [])], []))

    def test_instance_add_gives_the_copy_address_unless_the_gateway_serves_apps(self):
        for name, active, oauth in (
            ("xena", False, "on"),
            ("yuki", True, "off"),
            ("zara", True, None),
        ):
            with self.subTest(active=active, oauth=oauth):
                self.gateway_active = active
                if oauth:
                    self.gateway_settings(oauth)
                elif os.path.exists(self.ctl.GATEWAY_SETTINGS):
                    os.remove(self.ctl.GATEWAY_SETTINGS)
                _, printed = output(self.ctl.instance_add, name)
                self.assertIn(f"Address:  https://{name}.{BASE}/mcp", printed)
                self.assertNotIn(f"https://{BASE}/mcp", printed)
                # Its key is routed all the same.
                key = KEY_TEXT.search(printed).group()
                self.assertEqual(
                    self.routes()["keys"][sha(key)], {"copy": name, "label": "main"}
                )

    def test_instance_add_without_a_gateway_gives_the_copy_address(self):
        self.accounts.discard("sagasu-gateway")
        _, printed = output(self.ctl.instance_add, "zara")
        self.assertIn(f"Address:  https://zara.{BASE}/mcp", printed)
        self.assertFalse(os.path.exists(self.ctl.GATEWAY_ROUTES))
        self.assertEqual(self.labels("zara"), ["main"])

    def test_instance_remove_takes_the_copy_off_the_table(self):
        self.sync(restart_changed=False)
        first = self.routes()
        seen = self.spy_sync()
        self.ctl.access_remove = lambda name: None
        _, printed = output(self.ctl.instance_remove, "charlie")
        self.assertIn("deleted", printed)
        self.assertFalse(os.path.exists(self.ctl.Copy("charlie").env))
        table = self.routes()
        self.assertEqual(set(table["copies"]), {"sagasu", "alice"})
        self.assertNotIn(sha(self.keys[("charlie", "main")]), table["keys"])
        self.assertEqual(table["copies"]["alice"], first["copies"]["alice"])
        self.assertEqual((seen, self.restarts()), ([(NO_RESTART, [])], []))


class InstallScriptTests(CtlCase):
    def test_install_parses_and_sets_the_gateway_up_while_the_copies_are_stopped(self):
        result = subprocess.run(
            ["bash", "-n", str(KIT / "install.sh")], capture_output=True
        )
        self.assertEqual(result.returncode, 0, result.stderr)

        def at(fragment, last=False):
            index = INSTALL.rfind(fragment) if last else INSTALL.find(fragment)
            self.assertNotEqual(index, -1, fragment)
            return index

        sync = at("/opt/sagasu/bin/sagasu-ctl gateway-sync --no-restart --strict\n")
        self.assertLess(
            at("useradd --system --user-group --home-dir /var/lib/sagasu-gateway"), sync
        )
        self.assertIn(
            "for name in sagasu-launch sagasu-signin sagasu-ctl sagasu-self", INSTALL
        )
        self.assertLess(at('"$KIT/$name" "/opt/sagasu/bin/$name"'), sync)
        self.assertLess(at('systemctl stop sagasu.service "${COPIES[@]}"'), sync)
        self.assertLess(
            at("sagasu@.service sagasu-xvfb@.service sagasu-gateway.service"), sync
        )
        self.assertLess(at("GATEWAY_SETTINGS=/etc/sagasu/gateway.env\n"), sync)
        self.assertIn(f"'SAGASU_GATEWAY_ROUTES={self.default_routes}'", INSTALL)
        # A gateway switched off before the run is seen before its unit is installed.
        self.assertIn("! systemctl is-enabled --quiet sagasu-gateway.service", INSTALL)
        self.assertLess(
            at("LEAVE_GATEWAY_STOPPED=1"),
            at('install -o root -g root -m 0644 "$KIT/$unit"'),
        )
        # The Caddyfile in place is kept before the new one goes in.
        keep = at("cp -p /etc/caddy/Caddyfile /etc/caddy/Caddyfile.previous")
        self.assertLess(
            at("caddy validate --config /etc/caddy/Caddyfile.sagasu-new"), keep
        )
        self.assertLess(
            keep, at("mv /etc/caddy/Caddyfile.sagasu-new /etc/caddy/Caddyfile")
        )
        # After daemon-reload and before any copy starts: the sign-ins move,
        # the gateway starts and must answer.
        reload, copy_store = at("systemctl daemon-reload"), at(
            "install -o sagasu-gateway -g sagasu-gateway -m 0600"
        )
        move = at(
            "mv /var/lib/sagasu/oauth.sqlite3 /var/lib/sagasu/oauth.sqlite3.moved-to-gateway"
        )
        enable, start = at("systemctl enable sagasu-gateway.service"), at(
            "systemctl restart sagasu-gateway.service"
        )
        probe = at("if ! gateway_answers; then")
        for earlier, later in (
            (sync, reload),
            (reload, copy_store),
            (copy_store, move),
            (move, enable),
            (enable, start),
            (start, probe),
            (probe, at('systemctl start "$unit" || true', last=True)),
            (probe, at("systemctl start sagasu.service", last=True)),
            (
                probe,
                at("systemctl enable sagasu-xvfb.service sagasu.service caddy.service"),
            ),
        ):
            self.assertLess(earlier, later)
        self.assertLess(at("if [[ $LEAVE_GATEWAY_STOPPED -eq 0 ]]; then"), enable)
        # curl, for that test request, is installed.
        packages = re.search(
            r"apt-get install --yes((?:[^\n]*\\\n)*[^\n]*)", INSTALL
        ).group(1)
        self.assertIn("curl", packages.replace("\\", " ").split())

    def run_install(self, first, last, through=False, **variables):
        """Run the part of install.sh from `first` up to `last` (included if
        through), its paths moved into the temporary root and its commands
        recorded; (exit code, log lines, stderr)."""
        start = INSTALL.index(first)
        end = INSTALL.index(last, start) + (len(last) if through else 0)
        root = str(self.root)
        body = re.sub(
            r"/var/lib/|/etc/sagasu/|/etc/caddy/",
            lambda found: root + found.group(0),
            INSTALL[start:end],
        )
        settings = {
            "SETTINGS": self.ctl.OWNER_ENV,
            "LOG": f"{root}/log",
            "KIT": str(KIT),
            "HOST": "",
            "GATEWAY_SETTINGS": self.ctl.GATEWAY_SETTINGS,
            "LEAVE_GATEWAY_STOPPED": "0",
            "PROBE": "401",
            "CADDY_STATUS": "0",
            "CTL_STATUS": "0",
            **variables,
        }
        script = "\n".join(
            [
                "set -euo pipefail",
                *(f"{name}={shlex.quote(value)}" for name, value in settings.items()),
                "" if GNU_SED else SED_SHIM,
                re.search(
                    r"^recorded_host\(\) \{\n.*?\n\}\n", INSTALL, re.S | re.M
                ).group(0),
                'chown() { echo "chown $*" >>"$LOG"; }',
                'install() { echo "install $*" >>"$LOG"; cp "${@: -2:1}" "${@: -1}"; }',
                'ctl_stub() { echo "sagasu-ctl $*" >>"$LOG"; return "$CTL_STATUS"; }',
                'systemctl() { echo "systemctl $*" >>"$LOG"; }',
                'curl() { echo "curl $*" >>"$LOG"; printf %s "$PROBE"; }',
                'caddy() { echo "caddy $*" >>"$LOG"; return "$CADDY_STATUS"; }',
                "sleep() { :; }",
                body.replace("/opt/sagasu/bin/sagasu-ctl", "ctl_stub"),
                "",
            ]
        )
        Path(root, "log").write_text("")
        result = subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, timeout=60
        )
        return (
            result.returncode,
            Path(root, "log").read_text().splitlines(),
            result.stderr,
        )

    def install_gateway(self, **variables):
        """install.sh's gateway settings and routing table."""
        code, log, errors = self.run_install(
            "# The gateway: one address",
            "gateway-sync --no-restart --strict",
            through=True,
            **variables,
        )
        self.assertEqual(code, 0, errors)
        return log

    def start_gateway(self, **variables):
        """install.sh's start of the gateway, once the copies' code is in place."""
        return self.run_install(
            "# The gateway, before any copy starts again",
            "# Teammates' copies come back",
            **variables,
        )

    def owner_store(self):
        """The owner's OAuth sign-ins from before the gateway: an app signed in
        with the laptop key, and one with a key since removed."""
        path = self.root / "var/lib/sagasu/oauth.sqlite3"
        store, now = OAuthStore(path), time.time()
        store.add_client("app-1", json.dumps({"client_id": "app-1"}), now)
        for token, key in (
            ("tok-laptop", self.keys[("sagasu", "laptop")]),
            ("tok-gone", "sgs_gone"),
        ):
            grant = store.add_grant(
                "app-1", sha(key), ["sagasu"], now, "Claude", "claude.ai"
            )
            store.add_token(token_digest(token), grant, "access", now + 3600)
        store.close()
        return path

    def test_install_keeps_apps_signed_in_at_the_main_address(self):
        (self.root / "var/lib/sagasu-gateway").mkdir(parents=True)
        owner_store = self.owner_store()
        original = owner_store.read_bytes()
        log = self.install_gateway()
        state = f"{self.root}/var/lib/sagasu-gateway"
        settings = Path(self.ctl.GATEWAY_SETTINGS)
        self.assertEqual(
            settings.read_text().splitlines(),
            [
                "# The gateway: one address for every copy (sagasu/gateway.py).",
                f"SAGASU_GATEWAY_HOST={BASE}",
                "SAGASU_GATEWAY_SOCKET=/run/sagasu-gateway/http.sock",
                f"SAGASU_GATEWAY_ROUTES={self.ctl.GATEWAY_ROUTES}",
                f"SAGASU_GATEWAY_STATE={state}",
                "SAGASU_GATEWAY_OAUTH=on",
            ],
        )
        self.assertEqual(settings.stat().st_mode & 0o777, 0o640)
        self.assertEqual(
            log,
            [
                f"chown root:sagasu-gateway {settings}.new",
                "sagasu-ctl gateway-sync --no-restart --strict",
            ],
        )
        before = settings.read_bytes()
        self.assertEqual(
            self.install_gateway(), ["sagasu-ctl gateway-sync --no-restart --strict"]
        )  # once
        self.assertEqual(settings.read_bytes(), before)
        self.sync(restart_changed=False)  # what that sagasu-ctl call does
        # The sign-ins move to the gateway, which starts and must answer.
        code, log, errors = self.start_gateway()
        self.assertEqual(code, 0, errors)
        self.assertEqual(
            log,
            [
                f"install -o sagasu-gateway -g sagasu-gateway -m 0600 {owner_store} {state}/oauth.sqlite3",
                "systemctl enable sagasu-gateway.service",
                "systemctl restart sagasu-gateway.service",
                PROBE,
            ],
        )
        self.assertEqual(Path(state, "oauth.sqlite3").read_bytes(), original)
        self.assertFalse(owner_store.exists())
        self.assertEqual(Path(f"{owner_store}.moved-to-gateway").read_bytes(), original)
        # Only the first time: a later run leaves both stores alone.
        owner_store.write_bytes(b"the copy's own, made since")
        code, log, _ = self.start_gateway()
        self.assertEqual(code, 0)
        self.assertNotIn("install", " ".join(line.split()[0] for line in log))
        self.assertEqual(Path(state, "oauth.sqlite3").read_bytes(), original)
        self.assertEqual(owner_store.read_bytes(), b"the copy's own, made since")
        # The gateway, started from those settings, keeps the app signed in.
        app, oauth = gateway.build(dotenv_values(settings, interpolate=False))
        self.addCleanup(oauth.close)
        self.assertEqual(oauth.verify("tok-laptop"), ("sagasu/laptop", None))
        self.assertEqual(oauth.verify("tok-gone"), (None, "unknown_token"))
        self.assertEqual(
            app.routes.copy_of("sagasu/laptop")[1]["socket"], "/run/sagasu/http.sock"
        )

    def test_install_follows_the_owner_oauth_setting_and_redirect_hosts(self):
        redirects = "SAGASU_OAUTH_REDIRECT_HOSTS=claude.ai,chat.example.org"
        settings = Path(self.ctl.GATEWAY_SETTINGS)
        for oauth, extra, expected in (
            ("off", [], "off"),
            (None, [], "off"),
            ("on", ["SAGASU_HTTP_OAUTH=off"], "off"),
            ("on", [redirects], "on"),
        ):
            with self.subTest(oauth=oauth, extra=extra):
                if settings.exists():
                    settings.unlink()
                self.add_copy(None, ["laptop"], extra=extra, oauth=oauth)
                self.install_gateway()
                lines = settings.read_text().splitlines()
                self.assertEqual(lines[5], f"SAGASU_GATEWAY_OAUTH={expected}")
                self.assertEqual(lines[6:], [redirects] if redirects in extra else [])
        # The gateway, started from those settings, sends apps back only there.
        self.sync(restart_changed=False)
        _, oauth = gateway.build(dotenv_values(settings, interpolate=False))
        self.addCleanup(oauth.close)
        self.assertEqual(oauth.redirect_hosts, {"claude.ai", "chat.example.org"})

    def test_a_failed_strict_sync_stops_the_install(self):
        code, log, _ = self.run_install(
            "# The gateway: one address",
            "gateway-sync --no-restart --strict",
            through=True,
            CTL_STATUS="1",
        )
        self.assertNotEqual(code, 0)
        self.assertEqual(log[-1], "sagasu-ctl gateway-sync --no-restart --strict")

    def test_a_gateway_that_does_not_answer_is_backed_out(self):
        state = self.root / "var/lib/sagasu-gateway"
        state.mkdir(parents=True)
        owner_store = self.owner_store()
        original = owner_store.read_bytes()
        caddy = self.root / "etc/caddy"
        caddy.mkdir(parents=True)
        (caddy / "Caddyfile.previous").write_text("the Caddyfile before\n")
        self.install_gateway()
        for probe in ("000", "503"):  # nothing answers; something else answers
            with self.subTest(probe=probe):
                (caddy / "Caddyfile").write_text("the new Caddyfile\n")
                code, log, errors = self.start_gateway(PROBE=probe)
                self.assertEqual(code, 1)
                self.assertIn("The gateway did not start; backing it out.", errors)
                self.assertEqual(
                    log[:3],
                    [
                        f"install -o sagasu-gateway -g sagasu-gateway -m 0600 {owner_store} {state}/oauth.sqlite3",
                        "systemctl enable sagasu-gateway.service",
                        "systemctl restart sagasu-gateway.service",
                    ],
                )
                self.assertEqual(log[3:-1], [PROBE] * 30)
                self.assertEqual(
                    log[-1], "systemctl disable --now sagasu-gateway.service"
                )
                self.assertEqual(owner_store.read_bytes(), original)
                self.assertFalse(Path(f"{owner_store}.moved-to-gateway").exists())
                self.assertFalse((state / "oauth.sqlite3").exists())
                self.assertEqual(
                    (caddy / "Caddyfile").read_text(), "the Caddyfile before\n"
                )

    def test_backing_out_keeps_sign_ins_the_gateway_already_had(self):
        state = self.root / "var/lib/sagasu-gateway"
        state.mkdir(parents=True)
        (state / "oauth.sqlite3").write_bytes(b"the gateway's sign-ins")
        owner_store = self.owner_store()
        original = owner_store.read_bytes()
        self.install_gateway()
        code, log, _ = self.start_gateway(PROBE="000")
        self.assertEqual(code, 1)
        self.assertNotIn("install", [line.split()[0] for line in log])
        self.assertEqual(
            (state / "oauth.sqlite3").read_bytes(), b"the gateway's sign-ins"
        )
        self.assertEqual(owner_store.read_bytes(), original)
        self.assertFalse(Path(f"{owner_store}.moved-to-gateway").exists())

    def test_a_gateway_disabled_before_the_run_stays_stopped(self):
        state = self.root / "var/lib/sagasu-gateway"
        state.mkdir(parents=True)
        owner_store = self.owner_store()
        self.install_gateway()
        code, log, errors = self.start_gateway(LEAVE_GATEWAY_STOPPED="1")
        self.assertEqual(code, 0, errors)
        self.assertEqual(
            log,
            [
                f"install -o sagasu-gateway -g sagasu-gateway -m 0600 {owner_store} {state}/oauth.sqlite3",
            ],
        )

    def test_install_keeps_the_caddyfile_it_replaces(self):
        caddy = self.root / "etc/caddy"
        caddy.mkdir(parents=True)
        old = "# Publishes only Sagasu's MCP endpoint (an earlier kit).\nfbs.example.test {\n}\n"
        (caddy / "Caddyfile").write_text(old)
        code, log, errors = self.run_install(
            "# An update refreshes a proxy configuration", "# Local firewall"
        )
        self.assertEqual(code, 0, errors)
        self.assertEqual(
            log,
            [
                f"caddy validate --config {caddy}/Caddyfile.sagasu-new --adapter caddyfile"
            ],
        )
        self.assertEqual(
            (caddy / "Caddyfile").read_text(), CADDYFILE.replace("@PUBLIC_HOST@", BASE)
        )
        self.assertEqual((caddy / "Caddyfile.previous").read_text(), old)
        self.assertEqual((caddy / "Caddyfile.before-sagasu").read_text(), old)
        # One Caddy refuses is never put in place, and the copy kept is untouched.
        (caddy / "Caddyfile").write_text(old)
        (caddy / "Caddyfile.previous").write_text("kept\n")
        code, _, _ = self.run_install(
            "# An update refreshes a proxy configuration",
            "# Local firewall",
            CADDY_STATUS="1",
        )
        self.assertNotEqual(code, 0)
        self.assertEqual((caddy / "Caddyfile").read_text(), old)
        self.assertEqual((caddy / "Caddyfile.previous").read_text(), "kept\n")


def caddy_site(text, site):
    """({matcher: paths}, {handle block: [upstreams]}, {handle block: {proxy
    option: values}}, [(blocks, tokens)]) of a site."""
    stack, lines = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if line == "}":
            stack.pop()
        elif line and not line.startswith("#"):
            tokens = line.split()
            lines.append((tuple(stack), tokens))
            if tokens[-1] == "{":
                stack.append(" ".join(tokens[:-1]))
    assert not stack, "unbalanced braces"
    matchers = {
        tokens[0]: tokens[2:]
        for stack, tokens in lines
        if stack == (site,) and tokens[0].startswith("@") and tokens[1] == "path"
    }
    upstreams = {
        stack[1]: tokens[1:-1] if tokens[-1] == "{" else tokens[1:]
        for stack, tokens in lines
        if stack[:1] == (site,) and len(stack) == 2 and tokens[0] == "reverse_proxy"
    }
    options = {}
    for stack, tokens in lines:
        if (
            stack[:1] == (site,)
            and len(stack) == 3
            and stack[2].startswith("reverse_proxy")
        ):
            options.setdefault(stack[1], {})[tokens[0]] = tokens[1:]
    return matchers, upstreams, options, lines


class ProxyAndUnitTests(unittest.TestCase):
    def test_the_main_host_sends_mcp_and_oauth_to_the_gateway_and_sign_in_to_the_owner(
        self,
    ):
        matchers, upstreams, options, lines = caddy_site(CADDYFILE, "@PUBLIC_HOST@")
        self.assertEqual(matchers["@mcp"], ["/mcp"])
        self.assertEqual(set(matchers["@oauth"]), PUBLIC_PATHS)
        self.assertEqual(len(matchers["@oauth"]), len(set(matchers["@oauth"])))
        self.assertEqual(matchers["@signin"], ["/sign-in", "/sign-in/*"])
        gateway_socket, owner_socket = (
            "unix//run/sagasu-gateway/http.sock",
            "unix//run/sagasu/http.sock",
        )
        self.assertEqual(
            upstreams,
            {
                "handle @mcp": [gateway_socket, owner_socket],
                "handle @oauth": [gateway_socket],
                "handle @signin": [owner_socket],
            },
        )
        # /mcp falls back to the owner's copy only while the gateway is down.
        mcp = options["handle @mcp"]
        self.assertEqual(mcp["lb_policy"], ["first"])
        for option in ("lb_try_duration", "fail_duration"):
            self.assertEqual(len(mcp[option]), 1, option)
            self.assertRegex(mcp[option][0], r"^[1-9][0-9]*(ms|s|m)$")
        for block in ("handle @oauth", "handle @signin"):
            self.assertFalse(
                {"lb_policy", "lb_try_duration", "fail_duration"}
                & set(options.get(block, {}))
            )
        catch_all = [
            tokens for stack, tokens in lines if stack == ("@PUBLIC_HOST@", "handle")
        ]
        self.assertEqual(catch_all, [["respond", '"Not', 'found"', "404"]])
        handles = [
            tokens
            for stack, tokens in lines
            if stack == ("@PUBLIC_HOST@",) and tokens[0] == "handle"
        ]
        self.assertEqual(handles[-1], ["handle", "{"])
        self.assertIn(((), ["import", "/etc/caddy/sites/*.caddy"]), lines)
        # The gateway's own socket is the one install.sh configures.
        self.assertIn("'SAGASU_GATEWAY_SOCKET=/run/sagasu-gateway/http.sock'", INSTALL)

    def test_a_teammate_host_still_goes_straight_to_their_copy(self):
        matchers, upstreams, _, _ = caddy_site(CADDYSITE, "@HOST@")
        self.assertEqual(
            set(matchers["@mcp"]), {"/mcp", "/sign-in", "/sign-in/*"} | PUBLIC_PATHS
        )
        self.assertEqual(upstreams, {"handle @mcp": ["unix/@SOCKET@"]})

    @unittest.skipUnless(shutil.which("caddy"), "no caddy binary here")
    def test_caddy_accepts_the_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            os.mkdir(f"{folder}/sites")
            Path(folder, "sites/alice.caddy").write_text(
                CADDYSITE.replace("@HOST@", f"alice.{BASE}").replace(
                    "@SOCKET@", "/run/sagasu-alice/http.sock"
                )
            )
            Path(folder, "Caddyfile").write_text(
                CADDYFILE.replace("@PUBLIC_HOST@", BASE).replace(
                    "/etc/caddy/sites/", f"{folder}/sites/"
                )
            )
            result = subprocess.run(
                [
                    "caddy",
                    "adapt",
                    "--config",
                    f"{folder}/Caddyfile",
                    "--adapter",
                    "caddyfile",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("sagasu-gateway", "sagasu", "sagasu-alice"):
            self.assertIn(f"/run/{name}/http.sock", result.stdout)

    def test_the_gateway_unit(self):
        parser = configparser.ConfigParser(interpolation=None, strict=True)
        parser.optionxform = str
        parser.read_string(UNIT)
        service = parser["Service"]
        self.assertEqual(
            (service["User"], service["SupplementaryGroups"]),
            ("sagasu-gateway", "caddy"),
        )
        command = [
            "/opt/sagasu/venv/bin/python",
            "-I",
            "-m",
            "sagasu.gateway",
            "--config",
        ]
        self.assertEqual(
            shlex.split(service["ExecStart"]), command + ["/etc/sagasu/gateway.env"]
        )
        folder = shlex.split(service["ExecStartPre"].removeprefix("+"))
        self.assertEqual(folder[-1], "/run/sagasu-gateway")
        self.assertEqual(folder[folder.index("-g") + 1], "caddy")
        self.assertEqual(folder[folder.index("-m") + 1], "2750")
        # The socket folder with "-": ExecStartPre makes it, after the sandbox
        # is set up for that step (live finding: 226/NAMESPACE without it).
        self.assertEqual(
            set(service["ReadWritePaths"].split()),
            {"/var/lib/sagasu-gateway", "-/run/sagasu-gateway"},
        )
        install = (KIT / "install.sh").read_text()
        # A failed start reaches the readiness check (and its backout), not set -e.
        self.assertIn("systemctl restart sagasu-gateway.service || true", install)
        self.assertEqual(service["WorkingDirectory"], "/var/lib/sagasu-gateway")
        self.assertEqual(parser["Install"]["WantedBy"], "multi-user.target")
        # It holds a key to every copy: socket files only, no capabilities,
        # nothing of other processes.
        self.assertEqual(service["CapabilityBoundingSet"], "")
        self.assertEqual(service["SystemCallFilter"], "@system-service")
        self.assertEqual(service["ProtectProc"], "invisible")
        self.assertEqual(service["RestrictAddressFamilies"], "AF_UNIX")
        self.assertEqual(service["ProtectSystem"], "strict")
        self.assertEqual(service["UMask"], "0077")
        for setting in (
            "PrivateNetwork",
            "RestrictNamespaces",
            "NoNewPrivileges",
            "ProtectHome",
            "PrivateTmp",
            "PrivateDevices",
            "ProtectKernelTunables",
            "ProtectKernelModules",
            "ProtectKernelLogs",
            "ProtectControlGroups",
            "RestrictSUIDSGID",
            "LockPersonality",
            "MemoryDenyWriteExecute",
        ):
            self.assertEqual(service.get(setting), "yes", setting)

    def test_the_module_runs_as_the_unit_starts_it(self):
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run(
                [sys.executable, "-I", "-m", "sagasu.gateway", "--help"],
                cwd=folder,
                capture_output=True,
                text=True,
                timeout=120,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--config", result.stdout)


class GatewayCommandTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="sagasu-gateway-"))
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.routes, self.state = (
            self.folder / "gateway-routes.json",
            self.folder / "state",
        )
        self.gateway_key = "sgs_" + "g" * 43
        owner = {
            "socket": "/run/sagasu/http.sock",
            "host": BASE,
            "key": self.gateway_key,
            "oauth": True,
        }
        self.routes.write_text(
            json.dumps(
                {
                    "version": 1,
                    "copies": {"sagasu": owner},
                    "keys": {DIGEST: {"copy": "sagasu", "label": "laptop"}},
                }
            )
        )
        for name in ("uvicorn", "uvicorn.error", "mcp"):  # main() sets these
            self.addCleanup(
                logging.getLogger(name).setLevel, logging.getLogger(name).level
            )
        root, handlers, level = (
            logging.getLogger(),
            list(logging.getLogger().handlers),
            logging.getLogger().level,
        )

        def restore():  # main() gives the root logger its handler
            for handler in list(root.handlers):
                if handler not in handlers:
                    root.removeHandler(handler)
            root.setLevel(level)

        self.addCleanup(restore)

    def settings(self, **changes):
        return {
            "SAGASU_GATEWAY_HOST": BASE,
            "SAGASU_GATEWAY_ROUTES": str(self.routes),
            "SAGASU_GATEWAY_STATE": str(self.state),
            **changes,
        }

    def request(self, app, method, path, host=BASE):
        return asyncio.run(call(app, method, path, headers={"host": host}))

    def test_build_reads_its_settings(self):
        app, oauth = gateway.build(
            self.settings(
                SAGASU_GATEWAY_HOST=" FBS.Example.TEST ",
                SAGASU_OAUTH_REDIRECT_HOSTS="claude.ai, Chat.Example.org,",
            )
        )
        self.addCleanup(oauth.close)
        self.assertIsInstance(app, gateway.Gateway)
        self.assertEqual((app.host, app.routes.path), (BASE, self.routes))
        self.assertEqual(app.routes.keys, {DIGEST: "sagasu/laptop"})
        self.assertEqual(app.routes.oauth_keys, {DIGEST: "sagasu/laptop"})
        self.assertEqual(app.routes.digests, {"sagasu/laptop": DIGEST})
        self.assertIs(oauth.keys, app.routes.oauth_keys)
        self.assertIs(app.oauth, oauth)
        self.assertEqual(oauth.issuer, f"https://{BASE}")
        self.assertEqual(oauth.redirect_hosts, {"claude.ai", "chat.example.org"})
        self.assertTrue((self.state / "oauth.sqlite3").exists())
        self.assertEqual(self.request(app, "POST", "/mcp", host="other.test")[0], 421)
        self.assertEqual(
            self.request(app, "GET", "/sign-in")[0], 404
        )  # the owner's copy has it
        status, headers, _ = self.request(app, "POST", "/mcp")
        self.assertEqual(status, 401)
        self.assertIn(
            f'resource_metadata="https://{BASE}/.well-known/oauth-protected-resource/mcp"',
            headers["www-authenticate"],
        )
        status, _, body = self.request(
            app, "GET", "/.well-known/oauth-protected-resource/mcp"
        )
        self.assertEqual(
            (status, json.loads(body)["resource"]), (200, f"https://{BASE}/mcp")
        )

    def test_apps_sign_in_only_for_copies_with_oauth_and_a_gateway_key(self):
        table = json.loads(self.routes.read_text())
        for copy in ({"key": None, "oauth": True}, {"oauth": False}):
            with self.subTest(copy=copy):
                table["copies"]["sagasu"].update(copy)
                self.routes.write_text(json.dumps(table))
                app, oauth = gateway.build(self.settings())
                self.addCleanup(oauth.close)
                self.assertEqual(
                    app.routes.keys, {DIGEST: "sagasu/laptop"}
                )  # still routed
                self.assertEqual(oauth.keys, {})
                self.assertIs(oauth.keys, app.routes.oauth_keys)
                self.assertIs(app.routes.copies["sagasu"]["oauth"], False)

    def signed_in(self, oauth, token, key_digest):
        now = time.time()
        oauth.store.add_client(
            f"app-{token}", json.dumps({"client_id": f"app-{token}"}), now
        )
        grant = oauth.store.add_grant(
            f"app-{token}", key_digest, ["sagasu"], now, "Claude", "claude.ai"
        )
        oauth.store.add_token(token_digest(token), grant, "access", now + 3600)

    def switch_oauth(self, app, name, on):
        table = json.loads(self.routes.read_text())
        table["copies"][name]["oauth"] = on
        self.routes.write_text(json.dumps(table))
        app.routes.reload(force=True)

    def test_switching_a_copy_s_oauth_off_signs_its_apps_out_here(self):
        alice = "a" * 64
        table = json.loads(self.routes.read_text())
        table["copies"]["alice"] = {
            "socket": "/run/sagasu-alice/http.sock",
            "host": f"alice.{BASE}",
            "key": "sgs_" + "h" * 43,
            "oauth": True,
        }
        table["keys"][alice] = {"copy": "alice", "label": "main"}
        self.routes.write_text(json.dumps(table))
        app, oauth = gateway.build(self.settings())
        self.addCleanup(oauth.close)
        self.signed_in(oauth, "tok-laptop", DIGEST)
        self.signed_in(oauth, "tok-alice", alice)
        self.assertEqual(oauth.verify("tok-laptop"), ("sagasu/laptop", None))
        self.switch_oauth(app, "sagasu", False)
        self.assertEqual(
            app.routes.keys, {DIGEST: "sagasu/laptop", alice: "alice/main"}
        )  # still routed
        self.assertEqual(oauth.keys, {alice: "alice/main"})
        self.assertIs(oauth.keys, app.routes.oauth_keys)
        self.assertEqual([row[0] for row in oauth.store.grants()], [alice])
        self.assertEqual(oauth.verify("tok-laptop"), (None, "unknown_token"))
        # Switched back on, the app has to sign in again.
        self.switch_oauth(app, "sagasu", True)
        self.assertEqual(oauth.verify("tok-laptop"), (None, "unknown_token"))
        self.assertEqual(oauth.verify("tok-alice"), ("alice/main", None))

    # Regression (fixed): OAuthStore.keep_only_keys({}) runs "key_digest NOT IN (NULL)", which
    # is never true in SQL, so it ends no grant. When the last copy that allows
    # OAuth switches it off (oauth_keys becomes empty), the gateway keeps every
    # sign-in; they only fail as key_removed, and switching OAuth back on
    # brings each one back to life.
    def test_switching_the_last_copy_s_oauth_off_signs_its_apps_out_here(self):
        app, oauth = gateway.build(self.settings())
        self.addCleanup(oauth.close)
        self.signed_in(oauth, "tok-laptop", DIGEST)
        self.switch_oauth(app, "sagasu", False)
        self.assertEqual((oauth.keys, app.routes.keys), ({}, {DIGEST: "sagasu/laptop"}))
        self.switch_oauth(app, "sagasu", True)
        self.assertEqual(oauth.verify("tok-laptop"), (None, "unknown_token"))
        self.assertEqual(oauth.store.grants(), [])

    def test_build_without_oauth(self):
        app, oauth = gateway.build(self.settings(SAGASU_GATEWAY_OAUTH=" OFF "))
        self.assertIsNone(oauth)
        self.assertIsNone(app.oauth_app)
        self.assertFalse(self.state.exists())
        self.assertEqual(self.request(app, "GET", "/authorize")[0], 404)

    def test_build_refuses_a_bad_host_or_routing_table(self):
        for host in ("", "bad host", "https://fbs.example.test"):
            with self.assertRaises(SystemExit):
                gateway.build(self.settings(SAGASU_GATEWAY_HOST=host))
        for text in (
            None,
            "{",
            json.dumps({"version": 2}),
            json.dumps({"version": 1, "copies": {}}),
        ):
            if text is None:
                self.routes.unlink()
            else:
                self.routes.write_text(text)
            with self.assertRaises(SystemExit):
                gateway.build(self.settings())
        self.assertFalse(self.state.exists())
        # Without a setting, the table sagasu-ctl writes.
        with mock.patch.object(gateway, "Routes") as routes:
            routes.return_value.copies = {}
            with self.assertRaises(SystemExit):
                gateway.build({"SAGASU_GATEWAY_HOST": BASE})
        routes.assert_called_once_with(load_ctl().GATEWAY_ROUTES)

    def test_main_needs_and_reads_its_config_file(self):
        config, socket_path = self.folder / "gateway.env", self.folder / "gateway.sock"
        config.write_text(
            "# The gateway: one address for every copy (sagasu/gateway.py).\n"
            f"SAGASU_GATEWAY_HOST={BASE}\nSAGASU_GATEWAY_SOCKET={socket_path}\n"
            f"SAGASU_GATEWAY_ROUTES={self.routes}\nSAGASU_GATEWAY_STATE={self.state}\n"
            "SAGASU_GATEWAY_OAUTH=on\n"
        )
        listener, root = object(), logging.getLogger()
        before, log = list(root.handlers), io.StringIO()
        with (
            mock.patch("sagasu.remote.bind_unix", return_value=listener) as bind,
            mock.patch("sagasu.remote.throttle_third_party_logs"),
            mock.patch("uvicorn.Server") as server,
            contextlib.redirect_stderr(log),
        ):
            gateway.main(["--config", str(config)])
        bind.assert_called_once_with(str(socket_path))
        server.return_value.run.assert_called_once_with(sockets=[listener])
        settings = server.call_args.args[0]
        self.assertEqual((settings.lifespan, settings.proxy_headers), ("on", False))
        self.assertEqual(
            (settings.app.host, settings.app.routes.path), (BASE, self.routes)
        )
        self.assertTrue((self.state / "oauth.sqlite3").exists())
        with self.assertRaises(
            sqlite3.ProgrammingError
        ):  # closed when the server stops
            settings.app.oauth.store.db.execute("SELECT 1")
        # It logs as the copies do: with each event's fields, through the redaction.
        added = [handler for handler in root.handlers if handler not in before]
        self.assertEqual(len(added), 1)
        self.assertIsInstance(added[0].formatter, EventFormatter)
        self.assertLessEqual(
            {ContextFilter, Redact}, {type(item) for item in added[0].filters}
        )
        self.assertEqual(root.level, logging.INFO)
        self.assertRegex(
            log.getvalue(), r"gateway\.routes_loaded copies=1 keys=1 oauth_keys=1"
        )
        self.assertIn("gateway.listening", log.getvalue())
        self.assertNotIn(self.gateway_key, log.getvalue())
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as stop,
        ):
            gateway.main([])
        self.assertEqual(stop.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
