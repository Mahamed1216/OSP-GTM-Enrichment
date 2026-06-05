"""Reply Agent: classify inbound replies and draft responses.

Draft-only MVP. Does not send emails, create Gmail drafts, book calendar
events, push to Instantly, or modify campaigns in any way.
"""
from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from src.llm import generate_json

log = logging.getLogger(__name__)

# ---- Intent categories ----
INTENT_POSITIVE_INTEREST = "positive_interest"
INTENT_PRICING_QUESTION = "pricing_question"
INTENT_COMMERCIAL_TERMS = "commercial_terms_shared"
INTENT_REVSHARE = "revshare_or_bounty_offer"
INTENT_MEETING_REQUEST = "meeting_request"
INTENT_OBJECTION = "objection"
INTENT_NOT_INTERESTED = "not_interested"
INTENT_WRONG_PERSON = "wrong_person"
INTENT_REFERRAL = "referral"
INTENT_UNSUBSCRIBE = "unsubscribe"
INTENT_ANGRY = "angry_or_complaint"
INTENT_OOO = "out_of_office"
INTENT_HUMAN_REVIEW = "needs_human_review"

# ---- Recommended actions ----
ACTION_BOOK_MEETING = "book_meeting"
ACTION_CLARIFY = "ask_clarifying_question"
ACTION_SEND_INFO = "send_info"
ACTION_HUMAN = "route_to_human"
ACTION_NOT_INTERESTED = "mark_not_interested"
ACTION_STOP = "stop_sequence"
ACTION_NO_REPLY = "do_not_reply"

_UNSUBSCRIBE_PHRASES = [
    "unsubscribe",
    "remove me",
    "stop emailing",
    "stop contacting",
    "opt out",
    "opt-out",
    "take me off",
    "no more emails",
    "don't email",
    "do not email",
    "please remove",
]

_SYSTEM_PROMPT = """You are a reply agent for an outbound B2B sales team running cold email campaigns.

Your job:
1. Classify the intent of an inbound reply to an outbound cold email.
2. Recommend the right next action.
3. Draft a short, professional, human-sounding response.

---

CLASSIFICATION CATEGORIES (choose exactly one):
- positive_interest: prospect is genuinely interested
- pricing_question: prospect is asking about pricing or cost
- commercial_terms_shared: prospect is proposing contract or commercial terms
- revshare_or_bounty_offer: prospect is offering a revshare, bounty, or commission arrangement
- meeting_request: prospect has explicitly asked for a meeting
- objection: prospect has a concern or pushback
- not_interested: prospect is not interested
- wrong_person: email went to the wrong person
- referral: prospect is referring you to someone else
- unsubscribe: prospect wants to stop receiving emails
- angry_or_complaint: prospect is angry, threatening, or making a complaint
- out_of_office: auto-reply or out-of-office message
- needs_human_review: complex, sensitive, or unclear — requires a human

RECOMMENDED ACTIONS (choose exactly one):
- book_meeting: move to a call or meeting
- ask_clarifying_question: need more info before responding
- send_info: answer a question or send requested materials
- route_to_human: hand off to a person, do not reply automatically
- mark_not_interested: log as not interested, stop outreach
- stop_sequence: unsubscribe / opt-out — stop all contact immediately
- do_not_reply: out-of-office or no reply needed

---

HARD RULES:

1. Positive replies (positive_interest, meeting_request): default action = book_meeting.
2. Revshare, bounty, commission, or any commercial / contract terms:
   - Do NOT accept any terms in the draft
   - Do NOT negotiate by email
   - Redirect to a meeting to discuss
   - human_review_notes MUST say: "Commercial terms discussed — do not accept in writing without approval"
3. Unsubscribe requests: action = stop_sequence, draft = short polite opt-out confirmation only.
4. Angry or threatening replies: action = route_to_human, draft_body = "(Routed to human — do not send.)"
5. Legal, contract, payment, or guarantee topics: action = route_to_human.
6. Do not re-pitch the product — they already know who you are.

REPLY STYLE RULES:
- Maximum 3–4 sentences.
- Plain language. Sound like a real person.
- No fake enthusiasm ("Absolutely!", "Great question!", "That's great!").
- No long bullet lists unless the prospect explicitly asked for details.
- No pricing promises or commitments.
- Never accept revshare, bounty, discounts, or terms of any kind in writing.

CALENDAR LINK BEHAVIOR:
- If a calendar_link is provided AND action = book_meeting: include the link at the end of the draft.
- If NO calendar_link AND action = book_meeting: end the draft with something like
  "Happy to send over a calendar link if that helps." or ask to set up a time.

---

REVSHARE / BOUNTY EXAMPLE:
Inbound: "I'm happy to pay you 25% bounty on every one that buys. Our cycle is measured in weeks, not months or quarters."

Good response:
  "Can we set up a time to discuss? We don't normally do revshare but would like to learn more about the product and your ICP, and it might be something we consider.

  Here's our calendar link to book a time: [calendar_link]"

Avoid:
- "That works."
- Accepting the 25% bounty in any form.
- Negotiating specifics by email.
- Explaining your product again.
- Over-explaining.

---

Return ONLY valid JSON with these exact keys:
{
  "classification": "<category>",
  "recommended_action": "<action>",
  "draft_body": "<the reply draft for operator review>",
  "human_review_notes": "<notes for the operator to check before sending>"
}"""


class ReplyAgentResult(BaseModel):
    classification: str = Field(description="Intent category of the inbound reply")
    recommended_action: str = Field(description="Recommended next action")
    draft_body: str = Field(description="Draft reply text for operator review")
    human_review_notes: str = Field(description="Notes for the operator before sending")


def _is_unsubscribe(text: str) -> bool:
    lower = text.lower()
    return any(phrase in lower for phrase in _UNSUBSCRIBE_PHRASES)


async def classify_and_draft_reply(
    *,
    inbound_reply: str,
    original_outbound_email: str = "",
    lead_context: str = "",
    calendar_link: str = "",
    workspace_id: int | None = None,
) -> ReplyAgentResult:
    """Classify an inbound reply and produce a draft response.

    Draft-only. Does not send anything, create Gmail drafts, or book events.
    """
    # Fast-path: unsubscribe detection before calling LLM
    if _is_unsubscribe(inbound_reply):
        return ReplyAgentResult(
            classification=INTENT_UNSUBSCRIBE,
            recommended_action=ACTION_STOP,
            draft_body="Got it — I've removed you from our list. You won't hear from us again.",
            human_review_notes="Opt-out request detected. Stop all outreach for this contact immediately.",
        )

    # Build user message
    parts = [f"Inbound reply:\n{inbound_reply.strip()}"]
    if original_outbound_email.strip():
        parts.append(f"\nOriginal outbound email:\n{original_outbound_email.strip()}")
    if lead_context.strip():
        parts.append(f"\nLead / company context:\n{lead_context.strip()}")
    if calendar_link.strip():
        parts.append(f"\nCalendar link: {calendar_link.strip()}")
    else:
        parts.append(
            "\nCalendar link: (none provided — if action is book_meeting, "
            "ask to set up a time and offer to send a calendar link)"
        )

    user_msg = "\n".join(parts)

    from src.config import settings
    result = await generate_json(
        model=settings.content_model,
        system=_SYSTEM_PROMPT,
        user=user_msg,
        schema=ReplyAgentResult,
        max_tokens=1000,
    )

    # Post-validation: enforce hard rules regardless of LLM output
    if result.classification == INTENT_ANGRY:
        return ReplyAgentResult(
            classification=INTENT_ANGRY,
            recommended_action=ACTION_HUMAN,
            draft_body="(Routed to human — do not send.)",
            human_review_notes=(
                result.human_review_notes
                or "Angry or threatening reply. Do not respond automatically."
            ),
        )

    if result.classification == INTENT_UNSUBSCRIBE:
        return ReplyAgentResult(
            classification=INTENT_UNSUBSCRIBE,
            recommended_action=ACTION_STOP,
            draft_body="Got it — I've removed you from our list. You won't hear from us again.",
            human_review_notes="Opt-out request. Stop all outreach immediately.",
        )

    if result.classification in (INTENT_REVSHARE, INTENT_COMMERCIAL_TERMS):
        notes = result.human_review_notes or ""
        required_warning = "Commercial terms discussed — do not accept in writing without approval."
        if "do not accept" not in notes.lower() and "commercial terms" not in notes.lower():
            notes = required_warning + (" " + notes if notes else "")
        return ReplyAgentResult(
            classification=result.classification,
            recommended_action=result.recommended_action,
            draft_body=result.draft_body,
            human_review_notes=notes,
        )

    return result


def save_reply_draft(
    result: ReplyAgentResult,
    *,
    inbound_reply: str,
    original_outbound_email: str = "",
    lead_context: str = "",
    workspace_id: int | None = None,
    lead_id: int | None = None,
) -> int | None:
    """Persist a reply draft to the DB. Returns the row id or None on error."""
    try:
        from src.db import session_scope
        from src.models import ReplyDraft
        with session_scope() as session:
            draft = ReplyDraft(
                workspace_id=workspace_id,
                lead_id=lead_id,
                inbound_reply=inbound_reply,
                original_outbound_email=original_outbound_email or None,
                lead_context=lead_context or None,
                classification=result.classification,
                recommended_action=result.recommended_action,
                draft_body=result.draft_body,
                human_review_notes=result.human_review_notes,
            )
            session.add(draft)
            session.flush()
            return draft.id
    except Exception as exc:
        log.warning("reply_draft_save_failed", extra={"error": str(exc)})
        return None
