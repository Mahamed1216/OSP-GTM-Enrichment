"""Verify the Instantly /api/v2/leads payload uses the *correct*
custom_variables keys so the campaign template's
{{personalized_subject}} / {{personalized_body}} placeholders actually
resolve. Regression test for the bug where the old key names
(`custom_subject`/`custom_body`) silently fell through to Instantly's
hardcoded template content.
"""
from src.delivery.instantly import _build_payload
from src.models import GeneratedContent, Lead


def test_payload_uses_personalized_variable_names():
    lead = Lead(
        first_name="Alex",
        last_name="Stone",
        email="alex@example.com",
        company="Acme Inc",
    )
    content = GeneratedContent(
        kind="email",
        subject="Saw your Series B announcement",
        body="Congrats on the round. Quick thought on how teams like Acme...",
        signals_cited=["series_b", "head_of_sales"],
        prompt_version="v1",
        model="claude-sonnet-4-6",
    )

    payload = _build_payload(lead, content)

    cv = payload["custom_variables"]
    assert cv["personalized_subject"] == "Saw your Series B announcement"
    assert cv["personalized_body"] == content.body
    assert "custom_subject" not in cv, "Old key name leaked — campaign template won't substitute"
    assert "custom_body" not in cv, "Old key name leaked — campaign template won't substitute"

    # top-level shape — Instantly v2 uses `campaign` not `campaign_id`
    assert "campaign" in payload
    assert payload["email"] == "alex@example.com"
    assert payload["first_name"] == "Alex"
    assert payload["last_name"] == "Stone"
    assert payload["company_name"] == "Acme Inc"


def test_payload_handles_missing_subject_and_signals():
    """Subject is optional in the model — make sure that doesn't crash."""
    lead = Lead(first_name="A", last_name="B", email="ab@example.com")
    content = GeneratedContent(
        kind="email", subject=None, body="hi", signals_cited=None,
        prompt_version="v1", model="claude-sonnet-4-6",
    )
    payload = _build_payload(lead, content)
    assert payload["custom_variables"]["personalized_subject"] == ""
    assert payload["custom_variables"]["signals_cited"] == ""
