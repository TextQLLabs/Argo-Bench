"""Reference solution for rem-01-uncompensated-failures.

The question asks for a refund to every customer whose order failed through no fault of
their own, by Support's rule: the customer gave up after the time the kitchen had quoted the
food would be ready, and we actually took their money. It is asked at the end of March and
says "all cases", so the scope is every order the warehouse shows, January to March. The
status columns do not answer it: an order the workflow left open (``AWAITING_FULFILLMENT``)
was delivered, and the cancelling party on the header reads ``CUSTOMER`` on every charged
failure, so neither separates fault. What does is four facts in four tables: the order
failed (``OE_ORDER_HEADERS_ALL.CANCELLED_FLAG = 'Y'``, which covers both the cancelled and
the rejected), the payment was captured (an ``APPROVED`` row on ``XX_PAYMENT_AUTHS``; most
failed orders hold only a ``VOIDED`` authorisation, money the customer never parted with,
and a declined retry is not a charge), the customer gave up at the last ``CANCELLED`` stamp
on ``XX_ORDER_STATUS_HIST``, and the kitchen's promise is its first ``QUOTED_READY_AT`` on
``XX_ORDER_PREP_QUOTES``.

Filed: one ``remediate_payments`` refund per owed order, keyed by the order's id
(``HEADER_ID``, the id every order table carries; ``ORDER_NUMBER`` is the customer-facing
number) with the amount captured, plus a note.

Score: 1.0 on the paper's warehouse and 1.0 on the released warehouse.
"""

QUESTION = "rem-01-uncompensated-failures"

#: The four facts per failed order. APPROVED is one row per order, so the join is safe.
_FACTS = """\
WITH failed AS (
  SELECT HEADER_ID, FLOW_STATUS_CODE FROM OE_ORDER_HEADERS_ALL WHERE CANCELLED_FLAG = 'Y'
),
charged AS (
  SELECT HEADER_ID, AMOUNT FROM XX_PAYMENT_AUTHS WHERE STATUS_CODE = 'APPROVED'
),
gave_up AS (
  SELECT HEADER_ID, MAX(STATUS_DATE) AS gave_up_at
  FROM XX_ORDER_STATUS_HIST WHERE STATUS_CODE = 'CANCELLED' GROUP BY HEADER_ID
),
quoted AS (
  SELECT HEADER_ID, MIN(QUOTED_READY_AT) AS quoted_ready_at
  FROM XX_ORDER_PREP_QUOTES GROUP BY HEADER_ID
)"""

STEPS = [
    # 1. What "not successfully completed" looks like on the order headers, and which months
    #    the warehouse shows as of the end of March.
    ("run_sql", {"sql": """\
SELECT FLOW_STATUS_CODE, CANCELLED_FLAG, COUNT(*) AS orders,
       MIN(ORDERED_DATE) AS first_ordered, MAX(ORDERED_DATE) AS last_ordered
FROM OE_ORDER_HEADERS_ALL
GROUP BY FLOW_STATUS_CODE, CANCELLED_FLAG
ORDER BY orders DESC"""}),

    # 2. Whose money actually moved on a failed order: the authorisations by status, rows
    #    against orders (a declined card is retried, so auth rows are not orders).
    ("run_sql", {"sql": """\
SELECT h.FLOW_STATUS_CODE, a.STATUS_CODE AS auth_status, COUNT(*) AS auth_rows,
       COUNT(DISTINCT a.HEADER_ID) AS orders, ROUND(SUM(a.AMOUNT), 2) AS amount
FROM XX_PAYMENT_AUTHS a
JOIN OE_ORDER_HEADERS_ALL h ON h.HEADER_ID = a.HEADER_ID
WHERE h.CANCELLED_FLAG = 'Y'
GROUP BY h.FLOW_STATUS_CODE, a.STATUS_CODE
ORDER BY h.FLOW_STATUS_CODE, auth_status"""}),

    # 3. Support's rule over every failed order: charged or not, and whether the customer
    #    gave up after the kitchen's quoted ready time or still inside it.
    ("run_sql", {"sql": _FACTS + """
SELECT f.FLOW_STATUS_CODE,
       CASE WHEN c.HEADER_ID IS NULL THEN 'not charged' ELSE 'charged' END AS payment,
       CASE WHEN q.quoted_ready_at IS NULL OR g.gave_up_at IS NULL THEN 'no quote or stamp'
            WHEN g.gave_up_at > q.quoted_ready_at THEN 'after quoted ready'
            ELSE 'inside the window' END AS gave_up,
       COUNT(*) AS orders, ROUND(SUM(c.AMOUNT), 2) AS amount
FROM failed f
LEFT JOIN charged c ON c.HEADER_ID = f.HEADER_ID
LEFT JOIN gave_up g ON g.HEADER_ID = f.HEADER_ID
LEFT JOIN quoted q ON q.HEADER_ID = f.HEADER_ID
GROUP BY 1, 2, 3
ORDER BY 1, 2, 3"""}),

    # 4. The orders owed a refund: charged, and given up after the promised time.
    ("run_sql", {"sql": _FACTS + """
SELECT f.HEADER_ID AS order_id, ROUND(c.AMOUNT, 2) AS amount
FROM failed f
JOIN charged c ON c.HEADER_ID = f.HEADER_ID
JOIN gave_up g ON g.HEADER_ID = f.HEADER_ID
JOIN quoted q ON q.HEADER_ID = f.HEADER_ID
WHERE g.gave_up_at > q.quoted_ready_at
ORDER BY order_id"""}),

    # 5. File the refunds, each with the amount we captured.
    ("run_python", {"code": """\
import pandas as pd
from mission_control import MissionControl, Reason

owed = pd.read_parquet("results/sql_0004.parquet")
assert owed["order_id"].is_unique
print(f"{len(owed):,} orders owed a refund, ${owed['amount'].sum():,.2f} in all")

mission_control = MissionControl()
mission_control.remediate_payments(
    [{"order_id": int(r.order_id), "amount": float(r.amount)} for r in owed.itertuples()],
    action="refund", reason=Reason.OTHER,
    note="Failed order, customer gave up after the kitchen's quoted ready time; refund the "
         "amount captured.")
mission_control.note(
    "Failed orders (OE_ORDER_HEADERS_ALL.CANCELLED_FLAG = 'Y', every month shown, January to "
    "March) whose payment was captured (XX_PAYMENT_AUTHS STATUS_CODE = 'APPROVED'; voided "
    "authorisations were never charged) and whose customer gave up (last CANCELLED stamp on "
    "XX_ORDER_STATUS_HIST) after the kitchen's first quoted ready time "
    "(XX_ORDER_PREP_QUOTES.QUOTED_READY_AT). Refunded by HEADER_ID for the captured amount. "
    "Header status and the cancelling party do not separate fault and were not used.")
mission_control.summary()
"""}),
]
