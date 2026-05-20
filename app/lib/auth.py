"""Single shared-password gate.

Set ``APP_PASSWORD`` to require a password to access the app; leave it
unset (or empty) to run in dev-mode bypass with no auth. The previous
Supabase magic-link flow was removed — Supabase Postgres is still used
for storage via ``DATABASE_URL`` + SQLAlchemy, but the Python client is
no longer a dependency.
"""
from __future__ import annotations

import streamlit as st

from app.lib.config import get_secret


def is_auth_enabled() -> bool:
    return bool(get_secret("APP_PASSWORD"))


def is_authenticated() -> bool:
    if not is_auth_enabled():
        return True
    return bool(st.session_state.get("authenticated", False))


def check_password(password: str) -> bool:
    correct = get_secret("APP_PASSWORD")
    if not correct:
        return True
    return password == correct


def login(password: str) -> bool:
    if check_password(password):
        st.session_state["authenticated"] = True
        return True
    return False


def logout() -> None:
    st.session_state.pop("authenticated", None)
