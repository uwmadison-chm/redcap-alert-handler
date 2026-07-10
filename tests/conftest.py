# This file is part of rah, the REDCap Alert Handler.
# Copyright (c) Board of Regents of the University of Wisconsin System
# Distributed under the MIT license; see LICENSE in the project root.

"""Shared fixtures: a fake msal app and config files with usable cache paths.

The fake stands in for msal.ConfidentialClientApplication so nothing here
ever talks to Azure. The token cache stays real msal -- it's pure local
JSON, and faking it would just retest our own fake.
"""

import shutil
from pathlib import Path

import pytest

DATA = Path(__file__).parent / "data"


@pytest.fixture(autouse=True)
def reset_package_logger():
    """Undo any logging configuration a test left on the global rah logger.

    CLI invocations install a handler bound to CliRunner's captured stderr,
    which is closed once the invoke returns. Without this reset, that dead
    handler (and a lingering DEBUG level) leaks into whichever test logs next
    on the same xdist worker.
    """
    yield
    import logging

    from redcap_alert_handler.cli.conventions import LOGGER_NAME

    logger = logging.getLogger(LOGGER_NAME)
    logger.handlers.clear()
    logger.setLevel(logging.NOTSET)
    logger.propagate = True


class _FakeApp:
    """A stand-in for msal.ConfidentialClientApplication. No network, ever.

    Records calls so tests can assert what msal would have been asked to do.
    """

    def __init__(
        self,
        accounts=(),
        silent_result=None,
        flow=None,
        redeem_result=None,
        redeem_raises=None,
    ):
        self.accounts = list(accounts)
        self.silent_result = silent_result
        self.flow = flow or {
            "auth_uri": "https://login.microsoftonline.com/fake/authorize?client_id=x",
            "state": "abc123",
        }
        self.redeem_result = redeem_result
        self.redeem_raises = redeem_raises
        self.silent_calls = []
        self.redeem_calls = []
        self.initiate_calls = []

    def get_accounts(self):
        return list(self.accounts)

    def acquire_token_silent(self, scopes, account=None):
        self.silent_calls.append((scopes, account))
        return self.silent_result

    def initiate_auth_code_flow(self, scopes, redirect_uri=None):
        self.initiate_calls.append((scopes, redirect_uri))
        return self.flow

    def acquire_token_by_auth_code_flow(self, flow, auth_response):
        self.redeem_calls.append((flow, auth_response))
        if self.redeem_raises is not None:
            raise self.redeem_raises
        return self.redeem_result


@pytest.fixture
def fake_app():
    """A factory for fake msal apps; kwargs mirror _FakeApp's."""
    return _FakeApp


@pytest.fixture
def install_fake_msal(monkeypatch):
    """Patch msal's app class, in auth's namespace, to return the given fake."""

    def install(app):
        # Imported here, not at module scope, so a broken auth module fails
        # the tests that use it instead of taking down all collection.
        import redcap_alert_handler.auth

        monkeypatch.setattr(
            redcap_alert_handler.auth.msal,
            "ConfidentialClientApplication",
            lambda *args, **kwargs: app,
        )
        return app

    return install


@pytest.fixture
def cache_with_account(tmp_path):
    """A real token-cache file, holding one signed-in account, in tmp_path."""
    dest = tmp_path / "token-cache.json"
    shutil.copy(DATA / "token_caches" / "with_account.json", dest)
    return dest


@pytest.fixture
def empty_cache(tmp_path):
    """A real token-cache file with nothing in it."""
    dest = tmp_path / "token-cache.json"
    shutil.copy(DATA / "token_caches" / "empty.json", dest)
    return dest


@pytest.fixture
def fake_graph():
    """A fresh in-memory Graph mailbox, seeded with just the well-known folders.

    It's a plain object, not a factory: mutate page_size or add folders and
    messages on it before wiring a client to fake_graph.transport().
    """
    from fake_graph import FakeGraph

    return FakeGraph()


@pytest.fixture
def sleep_spy():
    """A stand-in for time.sleep that records what it was asked to wait."""

    class Spy:
        def __init__(self):
            self.calls = []

        def __call__(self, seconds):
            self.calls.append(seconds)

    return Spy()


@pytest.fixture
def graph_client(fake_graph, sleep_spy):
    """A GraphClient wired to fake_graph, with a recording sleep and static token."""
    from redcap_alert_handler.graph import GraphClient

    with GraphClient(
        fake_graph.mailbox,
        get_token=lambda: "test-token",
        transport=fake_graph.transport(),
        sleep=sleep_spy,
    ) as client:
        yield client


@pytest.fixture
def install_fake_graph(monkeypatch):
    """Patch doctor's GraphClient so its Graph check runs against a fake mailbox.

    Same idea as install_fake_msal: swap the name doctor constructs so nothing
    reaches the network. Pass a FakeGraph to seed state, or take the default.
    """

    def install(fake=None):
        from fake_graph import FakeGraph

        import redcap_alert_handler.cli.doctor
        from redcap_alert_handler.graph import GraphClient

        fake = fake if fake is not None else FakeGraph()

        def factory(mailbox, get_token, **kwargs):
            return GraphClient(
                mailbox,
                get_token=get_token,
                transport=fake.transport(),
                sleep=lambda seconds: None,
            )

        monkeypatch.setattr(redcap_alert_handler.cli.doctor, "GraphClient", factory)
        return fake

    return install


@pytest.fixture
def write_config(tmp_path):
    """Write a fixture config into tmp_path with token_cache_path swapped.

    Fixture configs point token_cache_path at /var/lib, which doesn't exist
    on a test box; tests that care about the cache pass a path of their own.
    """

    def write(cache_path, base="good_minimal.toml"):
        text = (DATA / "configs" / base).read_text()
        text = text.replace('"/var/lib/rah/token-cache.json"', f'"{cache_path}"')
        path = tmp_path / "config.toml"
        path.write_text(text)
        return path

    return write
