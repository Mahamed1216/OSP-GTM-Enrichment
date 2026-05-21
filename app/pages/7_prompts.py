"""Prompts editor — overlay-edit the system prompts for the three content
channels (email / linkedin_msg / call_script).

Edits persist to data/prompts_config.json via the loader's atomic write,
and take effect on the next content generation — no Streamlit restart
needed. The ICP block is owned by the Settings page; this editor does
not touch it.

Visual conventions mirror app/pages/6_settings.py: title, last-saved
caption, collapsed expanders, primary save button, persistent two-button
confirm for destructive actions, and a bordered red "Danger zone" block.
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st

from app.styles import inject_styles
from src.prompts.call_script import DEFAULT_CALL_SCRIPT_PROMPT_BODY
from src.prompts.email import DEFAULT_EMAIL_PROMPT_BODY
from src.prompts.linkedin_msg import DEFAULT_LINKEDIN_MSG_PROMPT_BODY
from src.prompts.loader import (
    get_effective_prompt,
    get_last_saved_timestamp,
    reset_all_overlays,
    reset_overlay,
    save_overlay,
)

inject_styles()

CHANNELS = [
    ("email", "Email", DEFAULT_EMAIL_PROMPT_BODY),
    ("linkedin_msg", "LinkedIn DM", DEFAULT_LINKEDIN_MSG_PROMPT_BODY),
    ("call_script", "Call Script", DEFAULT_CALL_SCRIPT_PROMPT_BODY),
]


def _last_saved_caption() -> str:
    ts = get_last_saved_timestamp()
    if ts is None:
        return ":gray[No overlay saved yet — showing defaults.]"
    return f":gray[Last saved: {ts}]"


def _render_confirm_pair(
    pending_key: str,
    confirm_label: str,
    cancel_key: str,
    confirm_key: str,
) -> bool:
    """Render persistent Confirm + Cancel pair. Returns True if Confirm clicked."""
    col_a, col_b = st.columns([1, 1])
    with col_a:
        confirmed = st.button(confirm_label, type="primary", key=confirm_key)
    with col_b:
        cancelled = st.button("Cancel", type="secondary", key=cancel_key)
    if cancelled:
        st.session_state.pop(pending_key, None)
        st.rerun()
    return bool(confirmed)


def _render_channel(channel: str, label: str, default_body: str) -> None:
    with st.expander(label, expanded=False):
        current = get_effective_prompt(channel, default_body)
        text_key = f"prompt_text_{channel}"
        edited = st.text_area(
            f"{label} system prompt",
            value=current,
            height=400,
            key=text_key,
            label_visibility="collapsed",
        )

        save_col, reset_col = st.columns([1, 1])
        with save_col:
            save_clicked = st.button(
                f"Save {label} prompt",
                type="primary",
                key=f"prompt_save_{channel}",
            )

        pending_key = f"prompt_reset_pending_{channel}"
        pending = bool(st.session_state.get(pending_key, False))

        with reset_col:
            if not pending:
                if st.button(
                    f"Reset {label} to default",
                    type="secondary",
                    key=f"prompt_reset_init_{channel}",
                ):
                    st.session_state[pending_key] = True
                    st.rerun()

        if save_clicked:
            try:
                save_overlay(channel, edited)
            except (OSError, ValueError) as exc:
                st.error(f"Failed to save {label} prompt: {exc}")
            else:
                st.toast(f"{label} prompt saved.", icon="✅")
                st.rerun()

        if pending:
            st.warning(
                f"This will discard the saved {label} overlay and revert to "
                "the built-in default. Continue?"
            )
            if _render_confirm_pair(
                pending_key=pending_key,
                confirm_label="Confirm reset",
                cancel_key=f"prompt_reset_cancel_{channel}",
                confirm_key=f"prompt_reset_confirm_{channel}",
            ):
                try:
                    reset_overlay(channel)
                except OSError as exc:
                    st.error(f"Reset failed: {exc}")
                    st.session_state.pop(pending_key, None)
                else:
                    st.session_state.pop(pending_key, None)
                    st.toast(f"{label} prompt reset to default.", icon="↩️")
                    st.rerun()


st.markdown(
    '<div style="margin-bottom: 3rem;">'
    '<h1 class="hero-headline" style="font-size: 72px;">Prompts.</h1>'
    '<p class="hero-sublabel">Edit the brain. Save, regenerate, test.</p>'
    '</div>',
    unsafe_allow_html=True,
)
st.write(_last_saved_caption())

for channel, label, default_body in CHANNELS:
    _render_channel(channel, label, default_body)

# ---------- Danger zone ----------
st.divider()
with st.container(border=True):
    st.markdown(":red[**Danger zone**]")
    st.caption(
        "Reset all three prompts to defaults. Any unsaved edits will be lost."
    )

    reset_all_key = "prompt_reset_all_pending"
    reset_all_pending = bool(st.session_state.get(reset_all_key, False))

    if not reset_all_pending:
        if st.button(
            "Reset all prompts",
            type="secondary",
            key="prompt_reset_all_init",
        ):
            st.session_state[reset_all_key] = True
            st.rerun()
    else:
        st.warning(
            "This will discard all three channel overlays. Defaults will "
            "be restored. Continue?"
        )
        if _render_confirm_pair(
            pending_key=reset_all_key,
            confirm_label="Confirm reset all",
            cancel_key="prompt_reset_all_cancel",
            confirm_key="prompt_reset_all_confirm",
        ):
            try:
                reset_all_overlays()
            except OSError as exc:
                st.error(f"Reset failed: {exc}")
                st.session_state.pop(reset_all_key, None)
            else:
                st.session_state.pop(reset_all_key, None)
                st.toast("All prompts reset to defaults.", icon="↩️")
                st.rerun()
