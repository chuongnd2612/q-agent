"""Serving the app under a URL prefix — `QAGENT_MOUNT_PATH`.

Behind the suite's shared front door this app lives at `/qagent/` and the proxy
strips the prefix before anything reaches FastAPI. So the application is
deliberately *unaware* of where it is mounted: every route matches exactly as it
does standalone, and none of these tests touch routing.

**Cookie `Path` is the exception, and it is the whole point of this module.** The
browser evaluates it against the address bar, not against what the backend sees.
Scope the refresh cookie to `/auth` while the SPA lives at `/qagent/` and the
browser stores a cookie for a path it will never request — the session then
appears to end at the next reload, with nothing in any log to say why.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.deps_auth import CSRF_COOKIE, REFRESH_COOKIE


def _settings(**kwargs) -> Settings:
    return Settings(secret_key="x" * 32, **kwargs)


# ------------------------------------------------------------- normalisation
@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("", ""),
        ("/", ""),
        ("qagent", "/qagent"),
        ("/qagent", "/qagent"),
        ("/qagent/", "/qagent"),
        ("  /qagent/  ", "/qagent"),
    ],
)
def test_the_mount_path_is_normalised(configured, expected):
    """Accepts what an operator would plausibly type.

    A cookie path wrong by one slash fails in a way nobody thinks to look for, so
    this is not a place to be strict about input.
    """
    assert _settings(mount_path=configured).mount_path == expected


# ------------------------------------------------------------- cookie paths
def _cookie_paths(monkeypatch, mount: str) -> dict[str, str]:
    """The `Path` attribute of each auth cookie, as the browser would read it."""
    from fastapi import Response

    import app.config as config_module
    import app.deps_auth as deps_auth

    monkeypatch.setattr(config_module.settings, "mount_path", mount)
    response = Response()
    deps_auth.set_auth_cookies(
        response, refresh_token="r", csrf_token="c", remember=False
    )

    out: dict[str, str] = {}
    for header in response.headers.getlist("set-cookie"):
        name = header.split("=", 1)[0]
        path = next(
            (p.split("=", 1)[1] for p in header.split("; ") if p.lower().startswith("path=")),
            "",
        )
        out[name] = path
    return out


def test_at_the_root_the_paths_are_unchanged(monkeypatch):
    """The standalone deployment must not move, so this is the regression guard
    for everything else in this file."""
    paths = _cookie_paths(monkeypatch, "")
    assert paths[REFRESH_COOKIE] == "/auth"
    assert paths[CSRF_COOKIE] == "/"


def test_under_a_prefix_both_cookies_move_with_it(monkeypatch):
    paths = _cookie_paths(monkeypatch, "/qagent")
    assert paths[REFRESH_COOKIE] == "/qagent/auth"
    assert paths[CSRF_COOKIE] == "/qagent"


def test_the_refresh_cookie_stays_narrower_than_the_app(monkeypatch):
    """ADR 0007: the refresh token is only ever presented to `/auth/*`, so it is
    scoped there and kept off every other request — including all of `/api`.
    Widening it to the mount root would undo that."""
    paths = _cookie_paths(monkeypatch, "/qagent")
    assert paths[REFRESH_COOKIE].startswith(paths[CSRF_COOKIE] + "/")


def test_clearing_uses_the_same_paths(monkeypatch):
    """A `delete_cookie` whose Path differs from the one it was set with is a
    no-op the browser accepts silently, leaving the cookie in place — so a logout
    would not log the user out."""
    from fastapi import Response

    import app.config as config_module
    import app.deps_auth as deps_auth

    monkeypatch.setattr(config_module.settings, "mount_path", "/qagent")
    set_response, clear_response = Response(), Response()
    deps_auth.set_auth_cookies(
        set_response, refresh_token="r", csrf_token="c", remember=False
    )
    deps_auth.clear_auth_cookies(clear_response)

    def paths(response) -> set[tuple[str, str]]:
        out = set()
        for header in response.headers.getlist("set-cookie"):
            name = header.split("=", 1)[0]
            path = next(
                (
                    p.split("=", 1)[1]
                    for p in header.split("; ")
                    if p.lower().startswith("path=")
                ),
                "",
            )
            out.add((name, path))
        return out

    assert paths(set_response) == paths(clear_response)


# --------------------------------------------------- routing is NOT affected
def test_routes_do_not_move(client, monkeypatch):
    """The proxy strips the prefix, so the app still answers on its own paths.

    If this ever fails, someone has taught the application about its mount point
    — and then the prefix has to be right in two places instead of one.
    """
    import app.config as config_module

    monkeypatch.setattr(config_module.settings, "mount_path", "/qagent")
    assert client.get("/health").status_code == 200
    assert client.get("/qagent/health").status_code == 404
