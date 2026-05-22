"""Prompts editor — overlay-edit the system prompts for the three content
channels (email / linkedin_msg / call_script).

Edits persist to data/prompts_config.json via the loader's atomic write,
and take effect on the next content generation — no Streamlit restart
needed. The ICP block is owned by the Settings page; this editor does
not touch it.

Visual conventions mirror app/pages/6_settings.py: title, last-saved
caption, collapsed expanders, primary save button.
"""
from __future__ import annotations

import re
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
    save_overlay,
)

inject_styles()

CHANNELS = [
    ("email", "Email", DEFAULT_EMAIL_PROMPT_BODY),
    ("linkedin_msg", "LinkedIn DM", DEFAULT_LINKEDIN_MSG_PROMPT_BODY),
    ("call_script", "Call Script", DEFAULT_CALL_SCRIPT_PROMPT_BODY),
]


def parse_prompt_sections(prompt: str) -> list[tuple[str, str]]:
    """Split a prompt into (header, body) pairs by H1 markdown headers.

    Preserves order. Body is everything until the next H1. Content before
    the first `# ` header is returned under the synthetic header
    "Preamble" so it round-trips through `recombine_sections` cleanly.
    """
    if not prompt or not prompt.strip():
        return [("Full prompt", prompt or "")]
    parts = re.split(r"^# ", prompt, flags=re.MULTILINE)
    preamble = parts[0].strip()
    sections: list[tuple[str, str]] = []
    if preamble:
        sections.append(("Preamble", preamble))
    for part in parts[1:]:
        lines = part.split("\n", 1)
        header = lines[0].strip()
        body = lines[1].strip() if len(lines) > 1 else ""
        sections.append((header, body))
    return sections


def recombine_sections(sections: list[tuple[str, str]]) -> str:
    """Reverse of `parse_prompt_sections`."""
    parts: list[str] = []
    for header, body in sections:
        if header == "Preamble" or header == "Full prompt":
            parts.append(body)
        else:
            parts.append(f"# {header}\n{body}")
    return "\n\n".join(parts).strip() + "\n"


def _last_saved_caption() -> str:
    ts = get_last_saved_timestamp()
    if ts is None:
        return ":gray[No overlay saved yet — showing defaults.]"
    return f":gray[Last saved: {ts}]"


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

        save_clicked = st.button(
            f"Save {label} prompt",
            type="primary",
            key=f"prompt_save_{channel}",
        )

        if save_clicked:
            try:
                save_overlay(channel, edited)
            except (OSError, ValueError) as exc:
                st.error(f"Failed to save {label} prompt: {exc}")
            else:
                st.toast(f"{label} prompt saved.", icon="✅")
                st.rerun()


def _render_email_channel_sectioned(channel: str, label: str, default_body: str) -> None:
    """Email-only variant: split the body into per-H1 sub-expanders.

    Each section gets its own textarea so edits stay scoped to one block
    of the prompt. On save the sections are recombined in order back into
    a single string and written via the same `save_overlay` path the
    other channels use.
    """
    with st.expander(label, expanded=False):
        current = get_effective_prompt(channel, default_body)
        sections = parse_prompt_sections(current)

        st.caption(
            f"Edit any section independently. Click 'Save {label} prompt' to commit."
        )

        edited_sections: list[tuple[str, str]] = []
        for i, (header, body) in enumerate(sections):
            is_goal = "GOAL" in header.upper()
            with st.expander(header, expanded=is_goal):
                edited_body = st.text_area(
                    label=header,
                    value=body,
                    height=200,
                    key=f"prompt_text_{channel}_section_{i}",
                    label_visibility="collapsed",
                )
                edited_sections.append((header, edited_body))

        save_clicked = st.button(
            f"Save {label} prompt",
            type="primary",
            key=f"prompt_save_{channel}",
        )

        if save_clicked:
            new_prompt = recombine_sections(edited_sections)
            if len(new_prompt.strip()) < 100:
                st.error(
                    "Combined prompt is under 100 characters. Add more "
                    "content before saving."
                )
            else:
                try:
                    save_overlay(channel, new_prompt)
                except (OSError, ValueError) as exc:
                    st.error(f"Failed to save {label} prompt: {exc}")
                else:
                    st.toast(f"{label} prompt saved.", icon="✅")
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
    if channel == "email":
        _render_email_channel_sectioned(channel, label, default_body)
    else:
        _render_channel(channel, label, default_body)
