"""Reference solution for pe-11-support-customer-exposure.

The question asks how much of the human refund desk's Q3 2024 backlog sat on the same
customers: waiting time at case grain, then at customer grain (a customer counted once at an
instant), the time a customer had two or more cases waiting at once, and the time they had
any, or only, week-old cases waiting. The traps are the customer grain (summing case hours
double-counts a customer with overlapping cases), the difference between ANY and ALL
week-old (a customer with one fresh and one old case counts in the first, not the second),
and the month edges (cases opened in June or handled in October still wait inside Q3).

``XX_SUPPORT_CASES`` holds the desk: the human refund queue is ``QUEUE_CODE = 'HUMAN'``, a
case waits from ``OPENED_DATE`` to ``ASSIGNED_DATE`` (first human handling starts; every
human case carries one), and it becomes week-old 168 hours after opening. The dates are
New York wall-clock times already. Each customer's timeline is swept in SQL: a wait adds one
waiting case at opening and removes it at assignment, a week-old wait adds one old case at
open + 168h and removes it at assignment; running sums give, between consecutive events, how
many cases the customer had waiting and how many of them were week-old. Each stretch is cut at
the month boundaries and summed per month, so no customer leaves the warehouse.

Score: 1.0 on the paper's warehouse and 1.0 on the released warehouse.
"""

QUESTION = "pe-11-support-customer-exposure"

_SWEEP = """\
WITH waits AS (
  SELECT CUST_ACCOUNT_ID AS customer, OPENED_DATE AS opened, ASSIGNED_DATE AS assigned,
         {week_old_at} AS week_old_at
  FROM XX_SUPPORT_CASES
  WHERE QUEUE_CODE = 'HUMAN'
    AND OPENED_DATE < '2024-10-01' AND ASSIGNED_DATE > '2024-07-01'
), spells AS (
  -- a case waiting, and the part of its wait after it turned a week old, inside Q3
  SELECT customer, GREATEST(opened, {q_lo}) AS s, LEAST(assigned, {q_hi}) AS e,
         1 AS waiting, 0 AS week_old
  FROM waits
  UNION ALL
  SELECT customer, GREATEST(week_old_at, {q_lo}), LEAST(assigned, {q_hi}), 0, 1
  FROM waits WHERE assigned > week_old_at
), events AS (
  SELECT customer, s AS t, waiting AS d_waiting, week_old AS d_week_old FROM spells WHERE e > s
  UNION ALL
  SELECT customer, e, -waiting, -week_old FROM spells WHERE e > s
), steps AS (
  SELECT customer, t, SUM(d_waiting) AS d_waiting, SUM(d_week_old) AS d_week_old
  FROM events GROUP BY customer, t
), states AS (
  -- between this event and the customer's next one: cases waiting, and how many week-old
  SELECT t, LEAD(t) OVER (PARTITION BY customer ORDER BY t) AS t_next,
         SUM(d_waiting) OVER (PARTITION BY customer ORDER BY t) AS waiting,
         SUM(d_week_old) OVER (PARTITION BY customer ORDER BY t) AS week_old
  FROM steps
), months AS (
  SELECT '2024-07' AS month, {m7} AS lo, {m8} AS hi
  UNION ALL SELECT '2024-08', {m8}, {m9}
  UNION ALL SELECT '2024-09', {m9}, {q_hi}
), pieces AS (
  SELECT m.month, s.waiting, s.week_old,
         {seconds} / 3600.0 AS hours
  FROM states s JOIN months m ON s.t < m.hi AND s.t_next > m.lo
  WHERE s.waiting > 0
)
SELECT month,
       SUM(waiting * hours) AS waiting_case_hours,
       SUM(hours) AS waiting_customer_hours,
       SUM(CASE WHEN waiting >= 2 THEN hours ELSE 0 END) AS multiple_case_customer_hours,
       SUM(CASE WHEN week_old >= 1 THEN hours ELSE 0 END) AS any_week_old_customer_hours,
       SUM(CASE WHEN week_old = waiting THEN hours ELSE 0 END) AS all_week_old_customer_hours
FROM pieces
GROUP BY month
ORDER BY month"""

_DIALECT = {
    "bigquery": {"week_old_at": "DATETIME_ADD(OPENED_DATE, INTERVAL 168 HOUR)",
                 "seconds": "DATETIME_DIFF(LEAST(s.t_next, m.hi), GREATEST(s.t, m.lo), SECOND)"},
    "duckdb": {"week_old_at": "OPENED_DATE + INTERVAL 168 HOUR",
               "seconds": "date_diff('second', GREATEST(s.t, m.lo), LEAST(s.t_next, m.hi))"},
}
_BOUNDS = {"q_lo": "DATETIME '2024-07-01'", "q_hi": "DATETIME '2024-10-01'",
           "m7": "DATETIME '2024-07-01'", "m8": "DATETIME '2024-08-01'",
           "m9": "DATETIME '2024-09-01'"}

STEPS = [
    # 1. Which cases reach a human, and does every human case record when handling started?
    ("run_sql", {"sql": """\
SELECT QUEUE_CODE, COUNT(*) AS cases, COUNT(ASSIGNED_DATE) AS with_assigned_date,
       SUM(CASE WHEN ASSIGNED_DATE < OPENED_DATE THEN 1 ELSE 0 END) AS assigned_before_opened,
       MIN(OPENED_DATE) AS first_opened, MAX(OPENED_DATE) AS last_opened
FROM XX_SUPPORT_CASES
GROUP BY QUEUE_CODE
ORDER BY QUEUE_CODE"""}),

    # 2. Sweep each customer's waiting cases through Q3 and total the hours per month.
    ("run_sql", {"sql": {engine: _SWEEP.format(**d, **_BOUNDS) for engine, d in _DIALECT.items()}}),

    # 3. Round to two decimals and publish.
    ("run_python", {"code": """\
import pandas as pd
from mission_control import MissionControl

df = pd.read_parquet("results/sql_0002.parquet")
for c in df.columns[1:]:
    df[c] = df[c].astype(float).round(2)
df = df.sort_values("month").reset_index(drop=True)
print(df.to_string(index=False))

mission_control = MissionControl()
mission_control.publish_data_sources([
    {"name": "support_customer_waiting_exposure", "frame": df},
])
mission_control.summary()
"""}),
]
