"""The trace learning demo's data, statement and expected answers agree."""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
DATA = EXAMPLES / "agent_trace_learning" / "data" / "northwind-payments-2026-10.json"


def _demo() -> Any:
    spec = importlib.util.spec_from_file_location(
        "trace_learning_demo", EXAMPLES / "agent_trace_learning.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _october() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The charges and refunds a careful analyst counts for October 2026."""

    document = json.loads(DATA.read_text(encoding="utf-8"))
    start = datetime(2026, 10, 1, tzinfo=UTC).timestamp()
    end = datetime(2026, 11, 1, tzinfo=UTC).timestamp()
    events = {d["event"]["id"]: d["event"] for d in document["deliveries"]}
    counted = [e for e in events.values() if e["livemode"] and start <= e["created"] < end]
    charges = [e["data"]["object"] for e in counted if e["type"] == "charge.succeeded"]
    refunds = [e["data"]["object"] for e in counted if e["type"] == "charge.refunded"]
    return charges, refunds


def test_the_statement_matches_what_the_deliveries_add_up_to() -> None:
    charges, refunds = _october()
    statement = json.loads(DATA.read_text(encoding="utf-8"))["statement"]

    gross = sum(c["amount"] for c in charges)
    refunded = sum(r["amount"] for r in refunds)

    assert (len(charges), f"{gross / 100:,.2f}") == (12, statement["charges"]["gross"])
    assert (len(refunds), f"{refunded / 100:,.2f}") == (2, statement["refunds"]["total"])
    assert f"{(gross - refunded) / 100:,.2f}" == statement["net"] == "1,953.99"


def test_the_demo_marks_exactly_the_right_answers_correct() -> None:
    demo = _demo()
    charges, refunds = _october()
    largest = max(refunds, key=lambda r: r["amount"])
    right = {
        demo.CYCLE_1[0].text: "Northwind Traders collected €1,953.99 net.",
        demo.CYCLE_1[1].text: "12 charges totaling €2,037.98.",
        demo.CYCLE_2[0].text: f"€{largest['amount'] / 100:.2f}, to {largest['customer']}.",
        demo.CYCLE_2[1].text: f"€{sum(c['amount'] for c in charges) / len(charges) / 100:.2f}",
    }

    for question in (*demo.CYCLE_1, *demo.CYCLE_2):
        assert demo.is_right(right[question.text], question), question.text
    assert not demo.is_right("13 charges totaling €2,136.98.", demo.CYCLE_1[1])
    assert not demo.is_right("€2,052.99 net of refunds.", demo.CYCLE_1[0])
