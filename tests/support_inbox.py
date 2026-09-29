"""The support_triage example catalog and a fake Jev that answers by ticket.

The fake provider answers judgments from one FIFO queue, and enrichment runs
processors concurrently, so a queue cannot say which ticket gets which answer.
`script_inbox` answers from the ticket text instead.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from memseek.llm.fake import fake
from memseek.llm.registry import Completion

CATALOG = Path(__file__).resolve().parents[1] / "examples" / "support_triage_catalog"


def noul(value: float) -> dict[str, Any]:
    return {"noul": value}


def choice(picked: str, confidence: float, options: tuple[str, ...]) -> dict[str, Any]:
    rest = (1 - confidence) / (len(options) - 1)
    return {
        "choice": picked,
        "probabilities": {option: confidence if option == picked else rest for option in options},
        "confidence": confidence,
    }


def score(value: float, levels: int, confidence: float = 0.8) -> dict[str, Any]:
    return {"score": value, "probabilities": [1 / levels] * levels, "confidence": confidence}


TOPICS = ("billing", "technical", "account", "other")
TEAMS = ("billing", "technical", "sales")

# Keyed by a phrase from each ticket in examples/support_triage.py.
ANSWERS: dict[str, dict[str, dict[str, Any]]] = {
    "charged our card twice": {
        "urgency": score(2.6, 4),
        "needs_reply": noul(0.97),
        "topic": choice("billing", 0.88, TOPICS),
        "frustration": score(1.9, 3),
        "team": choice("billing", 0.91, TEAMS),
        "covered": noul(0.85),
        "needs_escalation": noul(0.92),
    },
    "export last quarter": {
        "urgency": score(0.2, 4),
        "needs_reply": noul(0.9),
        "topic": choice("technical", 0.7, TOPICS),
        "frustration": score(0.1, 3),
        "team": choice("technical", 0.8, TEAMS),
        "covered": noul(0.95),
        "needs_escalation": noul(0.05),
    },
    "returned 503": {
        "urgency": score(2.9, 4),
        "needs_reply": noul(0.99),
        "topic": choice("technical", 0.95, TOPICS),
        "frustration": score(1.2, 3),
        "team": choice("technical", 0.97, TEAMS),
        "covered": noul(0.9),
        "needs_escalation": noul(0.96),
    },
    "SSO with Okta": {
        "urgency": score(0.8, 4),
        "needs_reply": noul(0.9),
        "topic": choice("account", 0.6, TOPICS),
        "frustration": score(0.3, 3),
        "team": choice("sales", 0.74, TEAMS),
        "covered": noul(0.1),
        "needs_escalation": noul(0.05),
    },
    "looks off": {
        "urgency": score(1.0, 4),
        "needs_reply": noul(0.6),
        "topic": choice("other", 0.35, TOPICS),
        "frustration": score(0.8, 3),
        "team": choice("billing", 0.41, TEAMS),
        "covered": noul(0.5),
        "needs_escalation": noul(0.2),
    },
}


def _ticket_text(state: Any) -> str | None:
    if not isinstance(state, Mapping):
        return None
    subject = state.get("record") or state.get("item") or {}
    return subject.get("text") or subject.get("content", {}).get("text")


def answers_for(state: Any, questions: set[str]) -> dict[str, Any] | None:
    text = _ticket_text(state) or ""
    for phrase, answers in ANSWERS.items():
        if phrase in text:
            return {name: answers[name] for name in questions}
    return None


def escalation_notes(prompt: str) -> str:
    """One critical note per ticket UUID the escalate prompt shows."""

    ids = re.findall(r'"id":\s*"([0-9a-f-]{36})"', prompt)
    return json.dumps(
        {
            "records": [
                {
                    "text": "Page the on-call lead and call the customer back today.",
                    "citations": [ticket_id],
                    "content": {"kind": "escalation", "severity": "critical"},
                }
                for ticket_id in dict.fromkeys(ids)
            ]
        }
    )


def script_inbox(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer every judgment from ANSWERS and every escalate prompt with notes."""

    judge, complete = fake.judge, fake.complete

    async def scripted_judge(endpoint: Any, model: str, request: Any) -> Any:
        if (raw := answers_for(request.state, set(request.questions))) is not None:
            fake.enqueue_judgment(raw)
        return await judge(endpoint, model, request)

    async def scripted_complete(endpoint: Any, model: str, system: str, prompt: str, **kw: Any):
        if "Tickets judged to need escalation" in prompt:
            fake.enqueue(Completion(escalation_notes(prompt)))
        return await complete(endpoint, model, system, prompt, **kw)

    monkeypatch.setattr(fake, "judge", scripted_judge)
    monkeypatch.setattr(fake, "complete", scripted_complete)
