"""Serialized reservations and reported spend; zero is explicitly unlimited."""
from __future__ import annotations

from contextlib import contextmanager
import json
import math
import sqlite3
from typing import Iterator

from usr.plugins.rrsi.helpers.contracts import UsageReceipt
from usr.plugins.rrsi.helpers.state import StateStore, utc_now


class BudgetExceeded(RuntimeError):
    pass


class UnknownPricing(RuntimeError):
    pass


class BudgetLedger:
    def __init__(self, store: StateStore, daily_limit: float = 0):
        if not math.isfinite(daily_limit) or daily_limit < 0:
            raise ValueError("Daily budget must be >=0; zero means unlimited")
        self.store, self.limit = store, float(daily_limit)
        with self.connection() as db:
            db.execute("CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, day TEXT NOT NULL, role TEXT NOT NULL, reserved REAL, actual REAL, status TEXT NOT NULL, receipt TEXT)")
            if "trial_id" not in {row[1] for row in db.execute("PRAGMA table_info(calls)")}:
                db.execute("ALTER TABLE calls ADD COLUMN trial_id TEXT")

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.store.path("usage.sqlite3"), timeout=30,
                             isolation_level=None)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except BaseException:
            if db.in_transaction:
                db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def reserve(self, call_id: str, role: str, maximum_usd: float | None, *, trial_id: str | None = None) -> None:
        if not call_id:
            raise ValueError("A unique model call ID is required")
        if maximum_usd is not None and (not math.isfinite(maximum_usd) or maximum_usd < 0):
            raise ValueError("Invalid maximum call cost")
        if self.limit and maximum_usd is None:
            raise UnknownPricing("A positive budget requires a known conservative call price")
        day = utc_now()[:10]
        with self.connection() as db:
            if db.execute("SELECT 1 FROM calls WHERE id=?", (call_id,)).fetchone():
                raise ValueError("Call ID already used; retry with a fresh call ID")
            # Uncertain/in-flight calls retain their reservation, including across restart.
            rows = db.execute("SELECT reserved,actual,status FROM calls WHERE day=?", (day,)).fetchall()
            if self.limit and any(r is None and a is None and s != "not_sent" for r,a,s in rows):
                raise UnknownPricing("Earlier unpriced spending must be reconciled before a capped run")
            spent = sum((a if a is not None else r or 0) for r,a,s in rows if s != "not_sent")
            if self.limit and spent + (maximum_usd or 0) > self.limit + 1e-12:
                raise BudgetExceeded("Daily RRSI spending cap reached")
            db.execute("INSERT INTO calls (id,day,role,reserved,actual,status,receipt,trial_id) VALUES (?,?,?,?,?,?,?,?)",
                       (call_id, day, role, maximum_usd, None, "reserved", None, trial_id))

    def finish(self, receipt: UsageReceipt) -> None:
        with self.connection() as db:
            row = db.execute("SELECT status,receipt,trial_id FROM calls WHERE id=?", (receipt.call_id,)).fetchone()
            if row is None:
                raise ValueError("Usage has no corresponding reservation")
            if row[2] != receipt.trial_id:
                raise ValueError("Usage trial does not match reservation")
            encoded = json.dumps(receipt.to_dict(), sort_keys=True)
            if row[0] == "complete":
                if row[1] != encoded:
                    raise ValueError("Conflicting usage for model call")
                return
            db.execute("UPDATE calls SET actual=?,status=?,receipt=? WHERE id=?",
                       (receipt.cost_usd, "complete", encoded, receipt.call_id))

    def not_sent(self, call_id: str) -> None:
        """Release only calls known not to have reached the provider."""
        with self.connection() as db:
            db.execute("UPDATE calls SET status='not_sent',reserved=0 WHERE id=? AND status='reserved'", (call_id,))

    def summary(self) -> dict:
        with self.connection() as db:
            rows = db.execute("SELECT day,role,reserved,actual,status,receipt FROM calls").fetchall()
        day = utc_now()[:10]
        current = [r for r in rows if r[0] == day and r[4] != "not_sent"]
        return {"day_utc": day, "daily_limit_usd": self.limit,
                "unlimited": self.limit == 0,
                "reported_cost_usd": sum(r[3] or 0 for r in current),
                "reserved_or_uncertain_usd": sum(r[2] or 0 for r in current if r[3] is None),
                "unpriced_calls": sum(r[3] is None and r[4] == "complete" for r in current),
                "pending_calls": sum(r[4] == "reserved" for r in current),
                "calls": len(current)}

    def receipts(self, trial_id: str | None = None) -> list[dict]:
        with self.connection() as db:
            rows = db.execute("SELECT receipt FROM calls WHERE receipt IS NOT NULL ORDER BY rowid").fetchall()
        records = [json.loads(row[0]) for row in rows]
        return [r for r in records if trial_id is None or r.get("trial_id") == trial_id]

    def trial_usage(self, trial_id: str) -> dict:
        if not trial_id:
            raise ValueError("A trial ID is required")
        with self.connection() as db:
            rows = db.execute("SELECT status,receipt FROM calls WHERE trial_id=? ORDER BY rowid", (trial_id,)).fetchall()
        receipts = [json.loads(r[1]) for r in rows if r[0] == "complete" and r[1]]
        pending = sum(r[0] not in {"complete", "not_sent"} for r in rows)
        return {"receipts": receipts, "complete": bool(receipts) and pending == 0,
                "pending_calls": pending,
                "tokens": sum(r["input_tokens"] + r["output_tokens"] for r in receipts)}


def reported_usage(raw: dict, *, call_id: str, role: str, model: str,
                   trial_id: str | None = None, price: dict | None = None) -> UsageReceipt:
    inp = raw.get("input_tokens", raw.get("prompt_tokens"))
    out = raw.get("output_tokens", raw.get("completion_tokens"))
    if type(inp) is not int or type(out) is not int:
        raise ValueError("Provider did not report complete input/output token usage")
    # Provider totals already include cached input and reasoning output. Do not add twice.
    cost = raw.get("cost")
    if cost is None and price is not None:
        cost = (inp * price["input_per_million"] + out * price["output_per_million"]) / 1_000_000
    return UsageReceipt(call_id, role, model, inp, out, cost, True, trial_id)
