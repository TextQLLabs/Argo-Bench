"""Reference solution for fc-12-true-ups-may.

The question (the April view, horizon May 2024): after the April true-up bleed, the dispatcher's
plan gives the connected hours for the payout periods ending in May. Forecast the sum of the
``ADJUSTMENTS`` lines on the ``COURIER_WEEKLY`` invoices for those periods.

The one idea: the dispatcher sets the hours, and the hours set what is owed (19.56 x connected
hours). It does not set the pay, which is earned per delivery and follows demand. April bled
because couriers were online and idle, not because pay was low. Cut the hours to the plan and
May owes less than it earns, so the aggregate floor stops binding; what is left is the
individual floor minus the invoices' small per-hour deduction. Scaling April's true-up per hour
up to the plan's hours, as most models did, forecasts millions.

Step 1 is one row per payout week of 2024 so far, from the minimum-pay engine's ledger
(``XX_PAY_PERIOD_INCENTIVES``) joined to the week's orders (Monday and Tuesday apart), shift
hours and the invoices' ``ADJUSTMENTS``; the last row is the week still open. Step 2 checks
the mechanism (a week's top-up = max(individual floor, 19.56 x engine hours - countable pay),
exact on the enforced weeks), reads May's pay level from April's normal-demand weeks (the open
week's Monday and Tuesday say demand has recovered), backtests how well a four-week level is
known, and forecasts May by Monte Carlo over the pay level.

The plan's hours are read from the prompt (the two warehouses' prompts state their own).

Score: 0.94 on the paper's warehouse and 0.87 on the released warehouse. On both the truth
sits a little above the individual floor, because the week still open on 30 April ran short,
which no filing from April could see.
"""

import re

QUESTION = "fc-12-true-ups-may"

_WEEKLY_LEDGER = """\
WITH weeks AS (
  SELECT PAYOUT_PERIOD_ID, {date_start} AS week_start, {date_end} AS week_end
  FROM XX_PAYOUT_PERIODS
  WHERE PARTY_TYPE_CODE = 'DRIVER' AND PERIOD_START_DATE <= '2024-04-30'
),
orders AS (
  SELECT w.PAYOUT_PERIOD_ID,
         COUNT(*) AS orders,
         SUM(CASE WHEN {mon_tue} THEN 1 ELSE 0 END) AS orders_mon_tue
  FROM weeks w
  JOIN OE_ORDER_HEADERS_ALL o ON {order_day} BETWEEN w.week_start AND w.week_end
  GROUP BY 1
),
shifts AS (
  SELECT w.PAYOUT_PERIOD_ID,
         SUM({shift_seconds}) / 3600 AS shift_hours
  FROM weeks w
  JOIN XX_DRIVER_SHIFTS s ON {shift_day} BETWEEN w.week_start AND w.week_end
  GROUP BY 1
),
invoices AS (
  SELECT CAST(i.ATTRIBUTE1 AS {int}) AS PAYOUT_PERIOD_ID, SUM(l.AMOUNT) AS adjustments
  FROM AP_INVOICES_ALL i
  JOIN AP_INVOICE_LINES_ALL l USING (INVOICE_ID)
  WHERE i.PAY_GROUP_LOOKUP_CODE = 'COURIER_WEEKLY' AND l.DESCRIPTION = 'ADJUSTMENTS'
  GROUP BY 1
)
SELECT
  w.week_start,
  e.ENFORCEMENT_STATUS_CODE              AS status,
  o.orders,
  o.orders_mon_tue,
  s.shift_hours,
  e.CONNECTED_HOURS                      AS engine_hours,
  e.COUNTABLE_PAY_AMOUNT                 AS countable_pay,
  IFNULL(e.TOPUP_INDIVIDUAL_AMOUNT, 0)   AS topup_individual,
  IFNULL(e.TOPUP_AGGREGATE_AMOUNT, 0)    AS topup_aggregate,
  a.adjustments
FROM weeks w
LEFT JOIN XX_PAY_PERIOD_INCENTIVES e USING (PAYOUT_PERIOD_ID)
LEFT JOIN orders o USING (PAYOUT_PERIOD_ID)
LEFT JOIN shifts s USING (PAYOUT_PERIOD_ID)
LEFT JOIN invoices a USING (PAYOUT_PERIOD_ID)
ORDER BY w.week_start"""

_DIALECT = {
    "bigquery": dict(
        date_start="DATE(PERIOD_START_DATE)", date_end="DATE(PERIOD_END_DATE)",
        mon_tue="EXTRACT(DAYOFWEEK FROM o.ORDERED_DATE) IN (2, 3)",   # Sunday is 1
        order_day="DATE(o.ORDERED_DATE)", shift_day="DATE(s.START_DATE)",
        shift_seconds="DATETIME_DIFF(s.END_DATE, s.START_DATE, SECOND)", int="INT64"),
    "duckdb": dict(
        date_start="CAST(PERIOD_START_DATE AS DATE)", date_end="CAST(PERIOD_END_DATE AS DATE)",
        mon_tue="EXTRACT(DOW FROM o.ORDERED_DATE) IN (1, 2)",          # Sunday is 0
        order_day="CAST(o.ORDERED_DATE AS DATE)", shift_day="CAST(s.START_DATE AS DATE)",
        shift_seconds="DATE_DIFF('second', s.START_DATE, s.END_DATE)", int="BIGINT"),
}

_FORECAST = """\
import numpy as np
import pandas as pd
from mission_control import MissionControl

RATE = 19.56             # $ per connected hour from 1 April
PLAN_HOURS = {plan_hours}   # the dispatcher's plan: connected hours, periods ending in May
MAY_WEEKS = 4            # periods ending 5, 12, 19 and 26 May

w = pd.read_parquet("results/sql_0001.parquet")
closed, open_week = w[w.adjustments.notna()].copy(), w.iloc[-1]

# 1. How a week's ADJUSTMENTS are made. The engine tops the week up by the larger of the
#    individual floor and the aggregate gap (19.56 x connected hours - countable pay); the
#    invoices then carry that top-up plus a small deduction per connected hour.
closed["gap"] = RATE * closed.engine_hours - closed.countable_pay
closed["topup"] = closed.topup_individual + closed.topup_aggregate
tested = closed[closed.topup_aggregate > 0]            # the aggregate test runs from mid-April
assert np.allclose(tested.topup, np.maximum(tested.topup_individual, tested.gap), rtol=1e-4)

april = closed[closed.status == "ENFORCED"]           # the rule and the pay rate reset on 1 April
engine_share = (april.engine_hours / april.shift_hours).mean()
deduction = ((april.adjustments - april.topup) / april.engine_hours).mean()   # $ per hour
individual = april.topup_individual.mean()                                    # $ per week

# 2. May's pay. Pay is earned per delivery and follows demand, not the plan. A week of April
#    below its norm is a demand dip; if the open week's Monday and Tuesday are back at the
#    norm, May runs at the level of April's normal weeks.
normal = april.orders >= 0.95 * april.orders.median()
mon_tue_norm = april[normal].orders_mon_tue.median()
assert open_week.orders_mon_tue >= 0.95 * mon_tue_norm, "demand has not recovered"
pay_week = april[normal].countable_pay.mean()

#    How well is a four-week level known? Backtest: four weeks of orders forecast by the
#    trailing four, over 2024 so far; plus April's week-to-week drift in pay per order.
orders = closed.orders.to_numpy()
errors = [orders[i:i + 4].sum() / (4 * orders[i - 4:i].mean()) - 1 for i in range(4, len(orders) - 3)]
pay_per_order = april.countable_pay / april.orders
spread = np.hypot(np.std(errors, ddof=1), pay_per_order.std() / pay_per_order.mean())

# 3. May. The plan sets the hours, and with them what is owed.
hours = engine_share * PLAN_HOURS
owed = RATE * hours
pay = np.random.default_rng(0).normal(MAY_WEEKS * pay_week, MAY_WEEKS * pay_week * spread, 200_000)
adjustments = np.maximum(MAY_WEEKS * individual, owed - pay) + deduction * hours
lower, point, upper = np.percentile(adjustments, [10, 50, 90])

print(april[["week_start", "orders", "engine_hours", "countable_pay", "gap", "topup", "adjustments"]]
      .round(0).to_string(index=False))
print(f"\\nopen week Mon-Tue orders {{open_week.orders_mon_tue:,.0f}} vs normal {{mon_tue_norm:,.0f}}")
print(f"May: owed {{owed:,.0f}} for {{hours:,.0f}} engine hours; pay {{MAY_WEEKS * pay_week:,.0f}} "
      f"+- {{spread:.1%}}")
print(f"forecast {{point:,.0f}}, 80% interval [{{lower:,.0f}}, {{upper:,.0f}}]")

mc = MissionControl()
mc.file_forecasts(
    [{{"name": "minimum_pay_true_ups_usd", "point": round(point, 2),
      "lower": round(lower, 2), "upper": round(upper, 2)}}],
    horizon="2024-05", level=0.8, unit="usd")
mc.note(
    "minimum_pay_true_ups_usd: a week's ADJUSTMENTS = max(individual floor, 19.56 x engine "
    "hours - countable pay) + a per-hour deduction, exact on the enforced weeks "
    f"(XX_PAY_PERIOD_INCENTIVES). The plan's hours make {{owed:,.0f}} owed in May. Pay follows "
    f"demand, not hours: April's normal weeks earned {{pay_week:,.0f}} each, and the open week's "
    f"Monday and Tuesday are back at the norm, so May earns about {{MAY_WEEKS * pay_week:,.0f}} "
    "and the floor stops binding. April bled on idle hours, not low pay. The interval is May's "
    f"pay level at +-{{spread:.1%}} (backtest of four-week order totals, plus April's drift in "
    "pay per order).")
mc.summary()
"""


def plan_hours(prompt: str) -> int:
    """The plan's connected hours for the periods ending in May, as the prompt states them."""
    m = re.search(r"about ([\d.]+) million connected hours", prompt)
    if m is None:
        raise ValueError("the prompt states no plan for May's connected hours")
    return round(float(m[1]) * 1_000_000)


def steps(prompt: str) -> list:
    return [
        # 1. The weekly ledger of 2024 so far, with the open week's first days.
        ("run_sql", {"sql": {engine: _WEEKLY_LEDGER.format(**parts)
                             for engine, parts in _DIALECT.items()}}),
        # 2. The mechanism, May's pay level, the plan, the forecast.
        ("run_python", {"code": _FORECAST.format(plan_hours=f"{plan_hours(prompt):_}")}),
    ]
