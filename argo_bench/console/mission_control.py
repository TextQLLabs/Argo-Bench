"""Mission Control — the operator action API for this workspace.

This module is the sanctioned way to file an operational decision. Analysis tells you
*what* is wrong; Mission Control is how you *act* on it. Every call is validated, then
recorded on the operator's control plane.

**Every method is plural.** A decision set is filed in one call carrying the complete
thing — every account, every period, every entry — not one call per row:

    from mission_control import MissionControl, Reason

    mc = MissionControl(id=get_sandbox_id(), target="https://<operator-host>")

    mc.ban_merchants([371778, 371904], reason=Reason.DISCOUNT_ABUSE)
    mc.end_campaigns([9912, 9914], reason=Reason.NEGATIVE_CONTRIBUTION_MARGIN)
    mc.post_journal_entries([
        {"period": "2024-07", "memo": "reclass suspense",
         "lines": [{"account": "2999", "debit": 1200.00},
                   {"account": "2101", "credit": 1200.00}]},
    ])

An item is either a bare id — which takes the call's shared arguments — or a mapping
that overrides them per item, so a batch with one reason and a batch with a reason each
are the same call shape:

    mc.ban_customers([101, 102], reason=Reason.PROMO_FARMING)
    mc.ban_customers([{"id": 101, "reason": Reason.PROMO_FARMING},
                      {"id": 102, "reason": Reason.CARD_TESTING}])
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any

try:
    import mission_control_plumbing as _plumbing
except ImportError:  # loaded by path: the plumbing sits beside this file
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import mission_control_plumbing as _plumbing
from mission_control_plumbing import (  # noqa: F401  (re-exported)
    CONTRACTS_ENV, DATA_SOURCE_TYPES, DEFAULT_CONTRACTS, DEFAULT_JOURNAL, PROTOCOL_VERSION,
    RENDER_SET_ENV, STDOUT_MARKER, SUPERSEDED, Action,
    MissionControlError, _Console, _text, get_sandbox_id, items_of,
)

__all__ = [
    "SUPERSEDED",
    "Action",
    "DATA_SOURCE_TYPES",
    "MissionControl",
    "MissionControlError",
    "Reason",
    "get_sandbox_id",
    "items_of",
]


# --------------------------------------------------------------------------- reasons
# `str, Enum` rather than `StrEnum`: this module is copied into analysis sandboxes
# whose Python version is not ours to choose, and the mixin form works everywhere.
class Reason(str, Enum):  # noqa: UP042
    """Why an action was taken.

    A `str` enum so `Reason.DISCOUNT_ABUSE == "DISCOUNT_ABUSE"` and the value serialises
    without a custom encoder. Free-text reasons are rejected: the whole point of a coded
    reason is that two operators filing the same finding file it the same way.
    """

    # --- abuse and fraud -------------------------------------------------------------
    DISCOUNT_ABUSE = "DISCOUNT_ABUSE"
    PROMO_FARMING = "PROMO_FARMING"
    REFERRAL_ABUSE = "REFERRAL_ABUSE"
    FRAUDULENT_CHARGEBACK = "FRAUDULENT_CHARGEBACK"
    REFUND_ABUSE = "REFUND_ABUSE"
    CARD_TESTING = "CARD_TESTING"
    ACCOUNT_TAKEOVER = "ACCOUNT_TAKEOVER"
    IDENTITY_FARMING = "IDENTITY_FARMING"
    COLLUSION_RING = "COLLUSION_RING"
    TRANSACTION_LAUNDERING = "TRANSACTION_LAUNDERING"
    REFUND_COLLUSION = "REFUND_COLLUSION"

    # --- courier conduct -------------------------------------------------------------
    TOO_MANY_MISSING_ORDERS = "TOO_MANY_MISSING_ORDERS"
    GPS_SPOOFING = "GPS_SPOOFING"
    FAKE_COMPLETION = "FAKE_COMPLETION"
    ROUTE_PADDING = "ROUTE_PADDING"
    TIP_BAITING = "TIP_BAITING"
    QUEST_GAMING = "QUEST_GAMING"

    # --- merchant conduct ------------------------------------------------------------
    GHOST_KITCHEN = "GHOST_KITCHEN"
    BAN_EVASION = "BAN_EVASION"
    PROMO_SELF_DEALING = "PROMO_SELF_DEALING"
    CANCELLATION_ABUSE = "CANCELLATION_ABUSE"
    PREP_TIME_GAMING = "PREP_TIME_GAMING"
    REVIEW_MANIPULATION = "REVIEW_MANIPULATION"
    BUST_OUT = "BUST_OUT"
    UNLICENSED = "UNLICENSED"

    # --- economics -------------------------------------------------------------------
    NEGATIVE_CONTRIBUTION_MARGIN = "NEGATIVE_CONTRIBUTION_MARGIN"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    LOW_INCREMENTALITY = "LOW_INCREMENTALITY"

    # --- accounting and controls -----------------------------------------------------
    SUBLEDGER_GL_MISMATCH = "SUBLEDGER_GL_MISMATCH"
    UNREVERSED_ACCRUAL = "UNREVERSED_ACCRUAL"
    ORPHANED_ACCRUAL = "ORPHANED_ACCRUAL"
    SUSPENSE_AGING = "SUSPENSE_AGING"
    STATUTORY_UNDERPAYMENT = "STATUTORY_UNDERPAYMENT"
    MISSTATED_PAYOUT_STATEMENT = "MISSTATED_PAYOUT_STATEMENT"
    SUBOPTIMAL_METHOD_ELECTION = "SUBOPTIMAL_METHOD_ELECTION"
    PAYMENT_CAPTURE_MISMATCH = "PAYMENT_CAPTURE_MISMATCH"
    DUPLICATE_PAYMENT = "DUPLICATE_PAYMENT"
    OTHER = "OTHER"


_plumbing.Reason = Reason
class MissionControl(_Console):
    """The operator console.

    Every action method is plural and files the complete set in one call::

        mc.ban_customers([101, 102, 103], reason=Reason.PROMO_FARMING)
        mc.report_balances([{"account": "2102", "amount": -4183902.55}],
                           as_of="2024-07-31 23:59:59")

    An item is a bare id, which takes the call's shared arguments, or a mapping that
    overrides them for that item alone. Validation runs per item and names the index of
    the one that failed; nothing is filed unless every item passes.

    Parameters
    ----------
    id:
        Sandbox identifier. Pass `get_sandbox_id()` to resolve it from the runtime.
    target:
        Base URL of the control plane. When omitted, actions are still validated,
        printed and journalled — useful for a dry run.
    dry_run:
        Validate and journal, but never open a connection.
    journal_path:
        Where the local JSONL journal is written. Set to None to disable it.
    emit_markers:
        Whether to echo each action to stdout as a ``[[MC]]`` line. Leave it on: it is
        the channel that works when the control plane is unreachable.
    contracts:
        The data-source contracts `publish_data_sources` validates against, as a mapping
        or a path to the JSON file. Omitted, they are found on their own: the
        ``MISSION_CONTROL_CONTRACTS`` file, ``mission_control_contracts.json`` beside the
        session, then the control plane.
    """

    def status(self) -> dict:
        """What has been filed, and whether the control plane took it.

        ``actions_filed`` counts calls; ``decisions_filed`` counts the items inside
        them. One ban_customers call closing 3,000 accounts is 1 and 3,000, and an
        operator checking their work wants the second number.
        """
        delivered = sum(1 for a in self.actions if a.delivered)
        return {
            "sandbox_id": self.sandbox_id,
            "session_id": self.session_id,
            "target": self.target,
            "actions_filed": len(self.actions),
            "decisions_filed": sum(a.n_items for a in self.actions),
            "actions_delivered": delivered,
            "delivery_errors": len(self.delivery_errors),
            "last_error": self.delivery_errors[-1] if self.delivery_errors else None,
        }

    def summary(self) -> str:
        """One line: how many decisions were filed, in how many calls, by kind, and how many
        calls reached the control plane. Print it when you are done::

            print(mc.summary())
            # 3002 decision(s) in 2 action(s) [ban_customers=3000, end_campaigns=2] — delivered 2
        """
        counts: dict[str, int] = {}
        for action in self.actions:
            counts[action.kind] = counts.get(action.kind, 0) + action.n_items
        if not counts:
            return "no actions filed"
        # Item counts, not call counts: `ban_customers=3000` is what was decided, where
        # `ban_customers=1` would report only that the method was reached.
        body = ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
        st = self.status()
        return (f"{st['decisions_filed']} decision(s) in {len(self.actions)} action(s) "
                f"[{body}] — delivered {st['actions_delivered']}")

    def ban_merchants(self, ids: Sequence[Any], reason: Any = None,
                      evidence: Mapping | None = None) -> Action:
        """Remove merchants from the platform. One call, every merchant.

            mc.ban_merchants([371778, 371904], reason=Reason.GHOST_KITCHEN)
            mc.ban_merchants([{"id": 371778, "reason": Reason.UNLICENSED},
                              {"id": 371904, "reason": Reason.BUST_OUT}])
        """
        return self._batch("ban_merchants", self._merchant_ban, ids,
                           {"reason": reason, "evidence": evidence},
                           primary="id", aliases={"merchant_id": "id"}, field_name="ids")

    def ban_couriers(self, ids: Sequence[Any], reason: Any = None,
                     evidence: Mapping | None = None) -> Action:
        """Deactivate courier accounts. One call, every courier.

        Each item is a courier id (a whole number), or a mapping with ``id`` (also accepted
        as ``driver_id`` or ``courier_id``) and its own ``reason`` / ``evidence``. The call's
        `reason` applies to every bare id; `evidence` is an optional dict of supporting
        figures::

            mc.ban_couriers([5501, 5502], reason=Reason.GPS_SPOOFING)
            mc.ban_couriers([{"id": 5501, "reason": Reason.GPS_SPOOFING,
                              "evidence": {"impossible_trips": 41}}])
        """
        return self._batch("ban_couriers", self._courier_ban, ids,
                           {"reason": reason, "evidence": evidence},
                           primary="id", aliases={"driver_id": "id", "courier_id": "id"},
                           field_name="ids")

    def ban_customers(self, ids: Sequence[Any], reason: Any = None,
                      evidence: Mapping | None = None) -> Action:
        """Close customer accounts. One call, every account.

        Each item is a customer account id (a whole number), or a mapping with ``id`` (also
        accepted as ``customer_id``) and its own ``reason`` / ``evidence``. The call's
        `reason` applies to every bare id; `evidence` is an optional dict of supporting
        figures::

            mc.ban_customers(df["CUST_ACCOUNT_ID"].to_list(), reason=Reason.PROMO_FARMING)
            mc.ban_customers([{"id": 101, "reason": Reason.PROMO_FARMING},
                              {"id": 102, "reason": Reason.CARD_TESTING}])
        """
        return self._batch("ban_customers", self._customer_ban, ids,
                           {"reason": reason, "evidence": evidence},
                           primary="id", aliases={"customer_id": "id"}, field_name="ids")

    def hold_payouts(self, items: Sequence[Any], reason: Any = None, amount: Any = None,
                     note: str | None = None) -> Action:
        """Freeze a supplier's payouts pending review. One call, every supplier.

        The supplier is a merchant storefront or a courier — either way the key is the
        payables `VENDOR_ID`. `amount` is optional: the amount to hold back (or claw
        back) when one is asked for::

            mc.hold_payouts([371778, 371904], reason=Reason.TRANSACTION_LAUNDERING)
            mc.hold_payouts([{"vendor_id": 371778, "amount": 4183.20}],
                            reason=Reason.BUST_OUT)
        """
        return self._batch("hold_payouts", self._payout_hold, items,
                           {"reason": reason, "amount": amount, "note": note},
                           primary="vendor_id", aliases={"id": "vendor_id",
                                                         "supplier_id": "vendor_id"})

    def discontinue_promo_codes(self, ids: Sequence[Any], reason: Any = None) -> Action:
        """Stop promo codes from being redeemed again. One call, every code.

        Each item is a promo code id (a whole number), or a mapping with ``id`` (also
        accepted as ``promo_code_id``) and its own ``reason``::

            mc.discontinue_promo_codes([7710, 7711], reason=Reason.DISCOUNT_ABUSE)
        """
        return self._batch("discontinue_promo_codes", self._promo_code_stop, ids,
                           {"reason": reason}, primary="id",
                           aliases={"promo_code_id": "id"}, field_name="ids")

    def flag_rings(self, rings: Sequence[Any], actor_type: Any = None, reason: Any = None,
                   evidence: Mapping | None = None) -> Action:
        """Report sets of actors believed to be operating together. One call, every ring.

        A ring is a member list; the batch is a list of rings, so the nesting is real
        rather than accidental::

            mc.flag_rings([[811, 812, 813], [944, 945]],
                          actor_type="customer", reason=Reason.COLLUSION_RING)
            mc.flag_rings([{"actor_ids": [811, 812], "actor_type": "device",
                            "reason": Reason.IDENTITY_FARMING}])
        """
        return self._batch("flag_rings", self._ring, rings,
                           {"actor_type": actor_type, "reason": reason,
                            "evidence": evidence},
                           primary="actor_ids", aliases={"ids": "actor_ids",
                                                         "members": "actor_ids"},
                           field_name="rings")

    def remediate_payments(self, items: Sequence[Any], action: Any = None,
                           reason: Any = Reason.OTHER, note: str | None = None) -> Action:
        """Fix payments on orders: refund, recapture, void, or write them off.

        Each item is an order id, or a mapping with ``order_id`` (also accepted as ``id``)
        and optionally ``amount`` (dollars; omit for the whole payment), ``action``,
        ``reason`` and ``note``. `action` is one of ``refund``, ``recapture``, ``void``,
        ``write_off`` or ``chargeback_represent``, shared across the batch unless an item
        overrides it::

            mc.remediate_payments([{"order_id": 88113, "amount": 42.10}],
                                  action="refund", reason=Reason.DUPLICATE_PAYMENT)
        """
        return self._batch("remediate_payments", self._payment_remedy, items,
                           {"action": action, "reason": reason, "note": note},
                           primary="order_id", aliases={"id": "order_id"})

    def end_campaigns(self, ids: Sequence[Any], reason: Any = None,
                      evidence: Mapping | None = None) -> Action:
        """Stop promotional campaigns. One call, every campaign.

        Each item is a campaign id (a whole number), or a mapping with ``id`` (also accepted
        as ``campaign_id``) and its own ``reason`` / ``evidence``. To record a campaign's
        measured economics, whether or not it ends, use `report_campaign_performances`::

            mc.end_campaigns([9912, 9914], reason=Reason.NEGATIVE_CONTRIBUTION_MARGIN)
        """
        return self._batch("end_campaigns", self._campaign_end, ids,
                           {"reason": reason, "evidence": evidence},
                           primary="id", aliases={"campaign_id": "id"}, field_name="ids")

    def report_campaign_performances(self, items: Sequence[Mapping]) -> Action:
        """Record measured campaign economics, whether or not they are being ended.

            mc.report_campaign_performances([
                {"id": 6057444, "incremental_cm": -812.40, "spend": 9120.00, "rank": 37},
                ...
            ])
        """
        return self._batch("report_campaign_performances", self._campaign_performance,
                           items, {}, primary="id", aliases={"campaign_id": "id"})

    def report_balances(self, items: Sequence[Mapping], as_of: Any = None,
                        basis: str = "gl", note: str | None = None) -> Action:
        """State account balances as of an instant. One call, every account.

        `basis` distinguishes the two numbers a reconciliation compares: what the GL
        says ('gl') versus what the subledger recomputes ('subledger'). It is shared
        across the batch unless an item overrides it::

            mc.report_balances([{"account": "2102", "amount": -4183902.55},
                                {"account": "2999", "amount": -268334.05}],
                               as_of="2024-07-31 23:59:59", basis="gl")
        """
        return self._batch("report_balances", self._balance, items,
                           {"as_of": as_of, "basis": basis, "note": note},
                           primary="account", aliases={"code_combination": "account"})

    def post_journal_entries(self, entries: Sequence[Mapping], period: Any = None,
                             memo: str | None = None,
                             source: str = "MISSION_CONTROL") -> Action:
        """Post balanced journal entries. One call, every entry.

        Each entry is `{"lines": [...], "period": "YYYY-MM", "memo": str}`, and each
        line is `{"account": str, "debit": amount}` or `{"account": str,
        "credit": amount}`. Every entry must balance to the cent — an unbalanced one is
        rejected here, naming its index, rather than failing downstream in the ledger.
        """
        return self._batch("post_journal_entries", self._journal_entry, entries,
                           {"period": period, "memo": memo, "source": source},
                           primary="lines", field_name="entries")

    def file_adjustments(self, items: Sequence[Mapping], account: Any = None,
                         period: Any = None, reason: Any = None,
                         note: str | None = None) -> Action:
        """Record reconciling differences against an account. One call, every difference.

        `amount` is the adjustment needed. Supplying both sides (`gl_amount`,
        `subledger_amount`) is preferred — it makes the difference auditable rather than
        asserted, and the two must agree with `amount` to the cent::

            mc.file_adjustments(
                [{"period": "2024-04", "amount": -18234.11, "gl_amount": ...,
                  "subledger_amount": ..., "note": "P17"}, ...],
                account="2102", reason=Reason.SUBLEDGER_GL_MISMATCH)
        """
        return self._batch("file_adjustments", self._adjustment, items,
                           {"account": account, "period": period, "reason": reason,
                            "note": note},
                           primary="account")

    def report_rollforwards(self, items: Sequence[Mapping], account: Any = None) -> Action:
        """File an account roll-forward, one item per period.

        `movements` maps a movement name to a signed amount. Opening plus the movements
        must equal closing to the cent, which is the whole point of a roll-forward, and
        the period that does not tie is named by its index::

            mc.report_rollforwards([
                {"period": "2024-01", "opening": 0.0, "closing": -812.10,
                 "movements": {"accrued": -9120.0, "paid": 8307.90}}, ...
            ], account="2102")
        """
        return self._batch("report_rollforwards", self._rollforward, items,
                           {"account": account}, primary="period")

    def report_agings(self, items: Sequence[Mapping], as_of: Any = None,
                      dimension: str = "age_days") -> Action:
        """Break balances down into buckets — by age, or by any other dimension.

        Each item is a mapping with ``account`` and ``buckets`` (a dict of bucket label to
        dollar amount); the total is computed for you. `as_of` is the instant the balances
        stand at (``"YYYY-MM-DD HH:MM:SS"``), and `dimension` names what the buckets are
        (``age_days`` by default), both shared unless an item overrides them::

            mc.report_agings([{"account": "1201", "buckets": {"0-30": 812.5, "31-60": 120.0}}],
                             as_of="2024-07-31 23:59:59")
            mc.report_agings([{"account": "2999", "buckets": {"REFUND ACCRUAL": -12.0}}],
                             as_of="2024-07-31 23:59:59", dimension="event_type")
        """
        return self._batch("report_agings", self._aging, items,
                           {"as_of": as_of, "dimension": dimension}, primary="account")

    def flag_orders(self, items: Sequence[Any], issue: Any = None, amount: Any = None,
                    period: Any = None, note: str | None = None) -> Action:
        """Flag orders for an accounting or control defect. One call, every order.

        Each item is an order id, or a mapping with ``order_id`` (also accepted as ``id``)
        and optionally its own ``issue``, ``amount`` (dollars), ``period`` (``"YYYY-MM"``)
        and ``note``. `issue` is a `Reason` naming the defect, shared unless an item
        overrides it::

            mc.flag_orders([88113, 88114], issue=Reason.UNREVERSED_ACCRUAL)
        """
        return self._batch("flag_orders", self._order_flag, items,
                           {"issue": issue, "amount": amount, "period": period,
                            "note": note},
                           primary="order_id", aliases={"id": "order_id"})

    def report_pay_periods(self, items: Sequence[Mapping], pay_period: Any = None,
                           method: Any = None) -> Action:
        """State driver-period statutory minimum-pay positions. One call, every position.

        `method` is which 6 RCNY 7-810 computation was used: 'standard' (per engaged
        hour) or 'alternative' (per trip).

        Each item is a mapping with ``driver_id`` (also accepted as ``id``),
        ``countable_pay`` and ``obligation`` (dollars), and optionally ``top_up``, which
        defaults to the shortfall, max(0, obligation - countable_pay). `pay_period` and
        `method` are shared unless an item overrides them::

            mc.report_pay_periods([{"driver_id": 5501, "countable_pay": 812.40,
                                    "obligation": 905.00}],
                                  pay_period="P14", method="standard")
        """
        return self._batch("report_pay_periods", self._pay_period, items,
                           {"pay_period": pay_period, "method": method},
                           primary="driver_id", aliases={"id": "driver_id"})

    def issue_pay_adjustments(self, items: Sequence[Any], amount: Any = None,
                              reason: Any = None, pay_period: Any = None,
                              note: str | None = None) -> Action:
        """Issue pay corrections to couriers. One call, every courier.

        `amount` is signed: positive pays the courier (an underpayment made whole),
        negative recovers an overpayment. A courier owed nothing does not belong in the
        batch — an empty batch records that the review found no one to adjust.

            mc.issue_pay_adjustments([{"courier_id": 88117, "amount": 41.87},
                                      {"courier_id": 88123, "amount": -12.02}],
                                     reason=Reason.STATUTORY_UNDERPAYMENT,
                                     pay_period="P14")

        A bare id takes the call's shared `amount`, so a flat correction to many
        couriers is `issue_pay_adjustments([88117, 88123], amount=25.00, reason=...)`.
        """
        return self._batch("issue_pay_adjustments", self._pay_adjustment, items,
                           {"amount": amount, "reason": reason,
                            "pay_period": pay_period, "note": note},
                           primary="driver_id",
                           aliases={"id": "driver_id", "courier_id": "driver_id"})

    def elect_methods(self, items: Sequence[Mapping]) -> Action:
        """Record which minimum-pay method was elected per period, and both costs.

            mc.elect_methods([{"pay_period": "P14", "method": "standard",
                               "standard_cost": 91204.11, "alternative_cost": 98230.02},
                              ...])
        """
        return self._batch("elect_methods", self._election, items, {},
                           primary="pay_period")

    def report_metrics(self, items: Sequence[Mapping], unit: str = "usd") -> Action:
        """Escape hatch for figures with no dedicated method.

        Still typed and still recorded — but a finding that keeps arriving here is a
        missing method, not a permanent home::

            mc.report_metrics([{"name": "accrual_headers", "value": 1204},
                               {"name": "unreversed_accruals", "value": 0}], unit="count")
        """
        return self._batch("report_metrics", self._metric, items, {"unit": unit},
                           primary="name")

    def file_forecasts(self, items: Sequence[Mapping], horizon: Any = None,
                       level: Any = 0.8, unit: str = "usd") -> Action:
        """Forecast figures that are not yet knowable: a point and a central prediction
        interval per metric. One call, every metric you were asked for.

        Each item is ``{"name", "point", "lower", "upper"}`` — the metric name exactly as
        it was given to you, your median forecast, and the bounds of a central interval
        at ``level`` (default 0.8: an 80% interval, so the truth should land inside it
        four times in five). ``horizon`` is when the figure resolves (``"2024-06"``, a
        quarter such as ``"2024-Q2"``, or a date) and ``unit`` is ``usd`` (rounded to the
        cent) or anything else (``count``, ``pct``); both are shared across the call
        unless an item overrides them::

            mc.file_forecasts([
                {"name": "courier_pay_per_engaged_hour_usd", "point": 27.90,
                 "lower": 26.80, "upper": 29.10},
                {"name": "active_couriers", "point": 44000, "lower": 41500,
                 "upper": 46000, "unit": "count"},
            ], horizon="2024-06", level=0.8)

        The interval is not decoration. A forecast is judged against the figure as it
        actually comes out, by its weighted interval score: a wide interval is charged for
        its width and a narrow one for every unit the outcome lands outside it, so the
        interval that reflects your real uncertainty is the best one to file. An item
        whose bounds do not bracket its point is rejected. Filing a name again
        supersedes the earlier forecast of it.
        """
        return self._batch("file_forecasts", self._forecast, items,
                           {"horizon": horizon, "level": level, "unit": unit},
                           primary="name",
                           aliases={"metric": "name", "median": "point",
                                    "forecast": "point", "lo": "lower", "hi": "upper",
                                    "low": "lower", "high": "upper"})

    def file_policies(self, items: Sequence[Mapping], level: Any = 0.8) -> Action:
        """Propose operating policies as code. One call, every policy you were asked for.

        Each item is ``{"name", "source"}`` plus optional ``"entrypoint"``, ``"claims"``
        and ``"note"``. ``source`` is the Python source text of a self-contained module
        (standard library only) defining the function the lab calls — its
        ``ENTRYPOINT``, e.g. ``dispatch(obs)`` for ``policy_lab`` (``entrypoint`` names
        another function if yours is called something else). ``name`` is the label you
        were given (one policy: any short name; a frontier: the point's name)::

            mc.file_policies([{
                "name": "balanced",
                "source": SOURCE,                      # the text, not the function object
                "claims": [{"metric": "contribution_per_order_usd_vs_today",
                            "point": 0.35, "lower": 0.20, "upper": 0.50},
                           {"metric": "on_time_rate_vs_today",
                            "point": -0.004, "lower": -0.012, "upper": 0.004}],
                "note": "holds offers until the courier would arrive as the food is ready",
            }], level=0.8)

        A filed policy is not judged on the evenings you tried it on. It is run on later
        evenings of the same area, paired against the policy in force today and against
        the best policy known. ``claims`` is what you expect it to change there relative
        to today's policy — a point and a central interval at ``level`` per metric you
        were asked about — and is judged like a forecast: a wide interval is charged for
        its width, a narrow one for every unit the later result lands outside it. A
        policy tuned to the evenings you tried it on will miss its own claim.

        The source is compiled here and rejected if it does not parse or does not define
        the entrypoint; run it in ``policy_lab`` before filing. Filing a name again
        supersedes the earlier policy of that name.
        """
        return self._batch("file_policies", self._policy, items, {"level": level},
                           primary="name",
                           aliases={"code": "source", "policy": "source",
                                    "function": "entrypoint", "claim": "claims"})

    def file_schedules(self, schedules: Sequence[Mapping], as_of: Any = None) -> Action:
        """File the tables a close produces. One call, every schedule.

        The typed methods fit decisions; a reconciliation, a trial balance, a cutoff
        proof or an aging is a *schedule*, whose shape is the finding, so it is filed as
        data rather than squeezed through ``report_metrics`` one figure at a time. Each
        item is one schedule: ``name`` (the one you were given), ``key_columns`` (the
        columns that identify a row — rows are read back and matched on them, so they
        carry the row vocabulary you were given), ``rows`` (a list of dicts; cells
        are text, numbers, booleans or None) and an optional ``as_of``. A row may carry
        any other columns, workings included; the numeric ones are what gets compared::

            mc.file_schedules([
                {"name": "ar_to_gl_reconciliation", "as_of": "2024-12-31",
                 "key_columns": ["item"],
                 "rows": [{"item": "incomplete_transactions", "amount": 2135890.12},
                          {"item": "receipts_not_yet_cleared", "amount": 3168270.55}]},
            ])
            # or straight from a dataframe: rows=df.to_dict("records") (pandas),
            # rows=df.to_dicts() (polars)

        A row missing one of its key columns is rejected naming the schedule and the row:
        an unidentifiable line reconciles to nothing. Filing a schedule again under the
        same name supersedes the earlier copy — a re-file is a correction, not a second
        finding.
        """
        return self._batch("file_schedules", self._schedule, schedules, {"as_of": as_of},
                           primary="name",
                           aliases={"schedule": "name", "keys": "key_columns",
                                    "records": "rows"},
                           field_name="schedules")

    # ------------------------------------------------------------------ data sources
    def data_source_contracts(self) -> dict[str, dict]:
        """The data sources this workspace's dashboards are waiting for, by name.

        A contract is what a tile binds to: ``columns`` (name, type, nullable, and the
        allowed ``values`` where a column is coded), the ``key`` that makes a row unique,
        the ``parameters`` a viewer can set with the ``bindings`` the dashboard renders,
        an ``order_by`` and a fixed ``row_count`` where the tile depends on either.
        Files nothing; read it before building a source, exactly as you would open the
        tile's field list.
        """
        if self._contracts is None:
            self._contracts = self._load_contracts()
        return {name: json.loads(json.dumps({k: v for k, v in c.items()
                                             if k != "hidden_bindings"}))
                for name, c in self._contracts.items()}

    def publish_data_sources(self, sources: Sequence[Mapping]) -> Action:
        """Publish the data source behind a dashboard tile. One call, every source.

        A tile binds to a **contract**: exactly these columns with these types, one row
        per key, in this order, for each parameter binding the dashboard renders. The
        request states the contract, and `data_source_contracts()` returns it. Publish
        the dataframe as it is::

            mc.publish_data_sources([{"name": "daily_order_volume", "frame": df}])

        A **parameterized** source — a tile with a borough picker, a trailing-window
        selector — needs one frame per binding in the contract, because a distinct count
        or a top ten under one binding cannot be added up from another's::

            mc.publish_data_sources([{
                "name": "zone_leaderboard",
                "frames": [{"params": {"borough": "Queens", "window_days": 28}, "frame": q28},
                           {"params": {"borough": "Bronx", "window_days": 7}, "frame": b7}],
            }])
            # or hand over the function and let the console call it once per binding:
            mc.publish_data_sources([{"name": "zone_leaderboard", "loader": leaderboard}])

        ``frame`` is a pandas or polars DataFrame, or a list of row dicts. ``definition``
        (optional) is the SQL or code behind it, kept with the filing.

        The endpoint refuses a frame that does not meet its contract and says everything
        that is wrong with it in one message — a missing or misnamed column, a float in
        an integer column, a null where none is allowed, a repeated key, a value outside
        a coded column's domain, the wrong number of rows, rows out of order, a binding
        with no frame. Nothing is published until every frame of every source passes.
        Publishing a name again replaces the earlier copy.
        """
        return self._batch("publish_data_sources", self._data_source, sources, {},
                           primary="name",
                           aliases={"source": "name", "data_source": "name", "df": "frame",
                                    "dataframe": "frame", "rows": "frame", "data": "frame",
                                    "sql": "definition", "query": "definition"},
                           field_name="sources")

    def note(self, text: str) -> Action:
        """Attach a free-text note to the session: rationale, caveats, anything that is not a decision.

        Singular, and deliberately so: a note is one piece of prose, not a set of
        decisions, and there is nothing here for a batch to collapse.
        """
        return self._file("note", {"text": _text(text, "text", max_len=8000)})

