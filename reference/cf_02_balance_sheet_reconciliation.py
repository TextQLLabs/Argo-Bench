"""Reference solution for cf-02-balance-sheet-reconciliation.

The question asks whether the summary accounts in the balances table are the sum of their
base accounts, for the monthly movement and year to date, in every period of 2024; it files
one row per GL period with the summary net, the detail net and the variance, and reports how
many summary rows were checked and how many periods failed. It does not say how a summary
account is told apart from a base one, nor where the roll-up lives, and the verdict alone is
guessable (a well-formed ledger ties): the twelve period nets are what has to be computed.

A summary account is a combination with ``GL_CODE_COMBINATIONS.SUMMARY_FLAG = 'Y'``;
``GL_ACCOUNT_HIERARCHIES`` maps each ``SUMMARY_CODE_COMBINATION_ID`` to the base
(``DETAIL_CODE_COMBINATION_ID``) accounts beneath it. In ``GL_BALANCES`` the monthly movement
is ``PERIOD_NET_DR - PERIOD_NET_CR`` and the year to date is the begin balance plus that
movement. Each summary row is compared with the sum of its own base rows for the same period
(a full outer join, so a base account with activity under a summary that carries no row would
show up as a break); the schedule is those comparisons summed per period.

Score: 1.0 on the paper's warehouse and 1.0 on the released warehouse.
"""

QUESTION = "cf-02-balance-sheet-reconciliation"

STEPS = [
    # 1. How summaries and base accounts are told apart: every hierarchy link should run from
    #    a summary combination to a detail one, with nothing nested.
    ("run_sql", {"sql": """\
SELECT s.SUMMARY_FLAG AS summary_side_flag, d.SUMMARY_FLAG AS detail_side_flag,
       COUNT(*) AS links,
       COUNT(DISTINCT h.SUMMARY_CODE_COMBINATION_ID) AS summary_accounts,
       COUNT(DISTINCT h.DETAIL_CODE_COMBINATION_ID) AS base_accounts
FROM GL_ACCOUNT_HIERARCHIES h
JOIN GL_CODE_COMBINATIONS s ON s.CODE_COMBINATION_ID = h.SUMMARY_CODE_COMBINATION_ID
JOIN GL_CODE_COMBINATIONS d ON d.CODE_COMBINATION_ID = h.DETAIL_CODE_COMBINATION_ID
GROUP BY s.SUMMARY_FLAG, d.SUMMARY_FLAG"""}),

    # 2. The tie-out: each summary row against the sum of its base rows, for the period's
    #    movement and the year to date, rolled up per 2024 period.
    ("run_sql", {"sql": """\
WITH bal AS (
  SELECT CODE_COMBINATION_ID AS ccid, PERIOD_NAME, PERIOD_NUM,
         PERIOD_NET_DR - PERIOD_NET_CR AS net,
         BEGIN_BALANCE_DR - BEGIN_BALANCE_CR + PERIOD_NET_DR - PERIOD_NET_CR AS ytd
  FROM GL_BALANCES
  WHERE PERIOD_YEAR = 2024),
summary AS (
  SELECT b.ccid, b.PERIOD_NAME, b.PERIOD_NUM, b.net, b.ytd
  FROM bal b JOIN GL_CODE_COMBINATIONS c ON c.CODE_COMBINATION_ID = b.ccid
  WHERE c.SUMMARY_FLAG = 'Y'),
rolled AS (
  SELECT h.SUMMARY_CODE_COMBINATION_ID AS ccid, b.PERIOD_NAME,
         MIN(b.PERIOD_NUM) AS PERIOD_NUM, SUM(b.net) AS net, SUM(b.ytd) AS ytd
  FROM GL_ACCOUNT_HIERARCHIES h JOIN bal b ON b.ccid = h.DETAIL_CODE_COMBINATION_ID
  GROUP BY h.SUMMARY_CODE_COMBINATION_ID, b.PERIOD_NAME),
pairs AS (
  SELECT COALESCE(s.PERIOD_NAME, r.PERIOD_NAME) AS period,
         COALESCE(s.PERIOD_NUM, r.PERIOD_NUM) AS period_num,
         s.ccid AS summary_ccid,
         COALESCE(s.net, 0) AS summary_net, COALESCE(r.net, 0) AS detail_net,
         COALESCE(s.ytd, 0) AS summary_ytd, COALESCE(r.ytd, 0) AS detail_ytd
  FROM summary s
  FULL OUTER JOIN rolled r ON r.ccid = s.ccid AND r.PERIOD_NAME = s.PERIOD_NAME)
SELECT period, MIN(period_num) AS period_num,
       COUNT(summary_ccid) AS summary_rows,
       ROUND(SUM(summary_net), 2) AS summary_net, ROUND(SUM(detail_net), 2) AS detail_net,
       ROUND(SUM(summary_ytd), 2) AS summary_ytd, ROUND(SUM(detail_ytd), 2) AS detail_ytd,
       SUM(CASE WHEN ABS(summary_net - detail_net) >= 0.005 THEN 1 ELSE 0 END)
         AS rows_off_movement,
       SUM(CASE WHEN ABS(summary_ytd - detail_ytd) >= 0.005 THEN 1 ELSE 0 END) AS rows_off_ytd
FROM pairs
GROUP BY period
ORDER BY period_num"""}),

    # 3. File the schedule and the two counts; a period is mismatched when any summary row in
    #    it breaks on either the movement or the year to date.
    ("run_python", {"code": """\
import pandas as pd
from mission_control import MissionControl

df = pd.read_parquet("results/sql_0002.parquet").sort_values("period_num")
df["variance"] = (df["summary_net"] - df["detail_net"]).round(2)
df["mismatched"] = (df["rows_off_movement"] + df["rows_off_ytd"]) > 0
print(df.to_string(index=False))

rows = [{"period": r.period, "summary_net": round(float(r.summary_net), 2),
         "detail_net": round(float(r.detail_net), 2), "variance": round(float(r.variance), 2)}
        for r in df.itertuples()]
checked = int(df["summary_rows"].sum())
mismatched = int(df["mismatched"].sum())

mission_control = MissionControl()
mission_control.file_schedules([
    {"name": "summary_to_detail", "key_columns": ["period"], "rows": rows},
])
mission_control.report_metrics([
    {"name": "summary_rows_checked", "value": checked},
    {"name": "mismatched_periods", "value": mismatched},
], unit="count")
mission_control.note(
    f"Summary accounts are GL_CODE_COMBINATIONS.SUMMARY_FLAG = 'Y'; GL_ACCOUNT_HIERARCHIES maps "
    f"each to its base accounts. For every 2024 period in GL_BALANCES, each summary row was "
    f"compared with the sum of its base rows on the movement (PERIOD_NET_DR - PERIOD_NET_CR) and "
    f"the year to date (begin balance + movement). {checked} summary rows checked across "
    f"{len(df)} periods; {mismatched} period(s) mismatched, largest period variance "
    f"{df['variance'].abs().max():.2f}.")
mission_control.summary()
"""}),
]
