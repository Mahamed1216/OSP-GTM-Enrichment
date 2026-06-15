"""Single shared-password gate (fail-closed).

Authentication is mandatory by default. Set ``APP_PASSWORD`` to the access
password. Authentication can only be disabled by explicitly setting
``AUTH_REQUIRED=false`` — intended for local development.

Fail-closed rules:
  - ``AUTH_REQUIRED`` defaults to ``true``. When true and ``APP_PASSWORD``
    is missing/empty, the app is **misconfigured** and must not render
    (see ``is_auth_misconfigured`` + the blocking error in ``app/main.py``).
  - When a password is configured, login is always required.
  - Authentication is only bypassed when ``AUTH_REQUIRED=false`` AND no
    password is set (explicit local-dev opt-in).

The previous Supabase magic-link flow was removed — Supabase Postgres is
still used for storage via ``DATABASE_URL`` + SQLAlchemy, but the Python
client is no longer a dependency.
"""
from __future__ import annotations

import hmac

import streamlit as st

from app.lib.config import get_secret

_FALSEY = {"false", "0", "no", "off"}


def is_auth_required() -> bool:
    """Whether authentication is mandatory. Defaults to True (fail-closed).

    Only an explicit ``AUTH_REQUIRED`` set to a falsey value
    (false/0/no/off) disables the requirement — intended for local dev.
    """
    raw = get_secret("AUTH_REQUIRED")
    if raw is None:
        return True
    return str(raw).strip().lower() not in _FALSEY


def is_password_configured() -> bool:
    return bool(get_secret("APP_PASSWORD"))


def is_auth_misconfigured() -> bool:
    """True when auth is required but no password is configured.

    In this state the app must refuse to render (fail closed) rather than
    silently allow public access.
    """
    return is_auth_required() and not is_password_configured()


def is_auth_enabled() -> bool:
    """True when a login gate should be shown.

    A login is required whenever a password is configured. The
    misconfiguration case (required but no password) is handled separately
    by ``is_auth_misconfigured`` so it can show a blocking error instead of
    a login form. Authentication is only bypassed when no password is set
    and ``AUTH_REQUIRED`` is explicitly false.
    """
    return is_password_configured()


def is_authenticated() -> bool:
    if not is_auth_enabled():
        return True
    return bool(st.session_state.get("authenticated", False))


def check_password(password: str) -> bool:
    correct = get_secret("APP_PASSWORD")
    if not correct:
        # Fail closed: never accept a password when none is configured.
        return False
    return hmac.compare_digest(str(password), str(correct))


def login(password: str) -> bool:
    if check_password(password):
        st.session_state["authenticated"] = True
        return True
    return False


def logout() -> None:
    st.session_state.pop("authenticated", None)
