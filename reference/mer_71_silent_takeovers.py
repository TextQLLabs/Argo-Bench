"""Reference solution for mer-71-silent-takeovers.

Trust & safety wants payouts frozen on every storefront whose payout account was taken over
this year "in the way the reported takeovers were" but which never reported it, so the
account was never put back. The reported takeovers are in the audit trail: every payout
account change is an ``XX_AUDIT_TRAIL`` row on ``IBY_PMT_INSTR_USES_ALL`` (the payee's
``EXT_PAYEE_ID`` in ``SOURCE_KEY_VALUE``, the old and new bank account ids in ``OLD_VALUE`` and
``NEW_VALUE``), made through the merchant portal; the reported ones also carry support's
``PAYOUT ACCOUNT RESTORED`` row. The contact e-mail's history is in ``HZ_CONTACT_POINTS``: a
replaced address stays behind as an inactive (``STATUS = 'I'``) row, retired at its
``LAST_UPDATE_DATE``. Every reported takeover had its e-mail changed a day or two before the
bank account; ordinary bank changes with an e-mail change have it more than a week apart.
Sharing an account is not a tell on its own: hundreds of ordinary changes land on an account
that is also on file for some other supplier, and restaurant groups share one account across
their brands. What the reported takeovers share is the attacker's account: one takeover's
account turns up in the next.

The rule filed, over the 2024 portal changes that were never restored: the e-mail changed
within three days before the bank account (the reported takeovers' gaps all fall inside two
and a quarter days), or the new account is one we were at that moment paying another
storefront into after a change of that same kind (a takeover not yet undone). Storefronts are
reached through ``IBY_EXTERNAL_PAYEES_ALL`` (``SUPPLIER_SITE_ID``) and ``AP_SUPPLIER_SITES_ALL``,
and held by their payables ``VENDOR_ID`` for account takeover. Every step before the last
returns counts only; the last returns only the storefronts held.

Score: 1.0 on the paper's warehouse and 1.0 on the released warehouse.
"""

QUESTION = "mer-71-silent-takeovers"

#: Hours from the last e-mail change to the bank change, per dialect.
_GAP_HOURS = {
    "bigquery": "DATETIME_DIFF(changed_at, last_email_change, MINUTE) / 60.0",
    "duckdb": "date_diff('minute', last_email_change, changed_at) / 60.0",
}

#: Every 2024 portal change of a payout account, with its storefront, whether support
#: restored it, and the last contact e-mail change on or before it.
_CHANGES = """\
WITH portal AS (
  SELECT SOURCE_KEY_VALUE AS payee_key, CHANGED_DATE AS changed_at, NEW_VALUE AS new_account
  FROM XX_AUDIT_TRAIL
  WHERE SOURCE_TABLE = 'IBY_PMT_INSTR_USES_ALL'
    AND CHANGE_REASON LIKE 'PAYOUT ACCOUNT UPDATED BY MERCHANT VIA PORTAL%'
    AND CHANGED_DATE >= '2024-01-01' AND CHANGED_DATE < '2025-01-01'
),
restored AS (
  SELECT SOURCE_KEY_VALUE AS payee_key, MIN(CHANGED_DATE) AS restored_at
  FROM XX_AUDIT_TRAIL
  WHERE SOURCE_TABLE = 'IBY_PMT_INSTR_USES_ALL'
    AND CHANGE_REASON LIKE 'PAYOUT ACCOUNT RESTORED%'
  GROUP BY SOURCE_KEY_VALUE
),
payees AS (
  SELECT CAST(p.EXT_PAYEE_ID AS STRING) AS payee_key, p.PAYEE_PARTY_ID, s.VENDOR_ID
  FROM IBY_EXTERNAL_PAYEES_ALL p
  JOIN AP_SUPPLIER_SITES_ALL s ON s.VENDOR_SITE_ID = p.SUPPLIER_SITE_ID
),
email_changes AS (
  SELECT OWNER_TABLE_ID AS party_id, LAST_UPDATE_DATE AS email_changed_at
  FROM HZ_CONTACT_POINTS
  WHERE OWNER_TABLE_NAME = 'HZ_PARTIES' AND CONTACT_POINT_TYPE = 'EMAIL' AND STATUS = 'I'
),
changes AS (
  SELECT c.payee_key, py.VENDOR_ID, c.changed_at, c.new_account, r.restored_at,
         MAX(e.email_changed_at) AS last_email_change
  FROM portal c
  JOIN payees py ON py.payee_key = c.payee_key
  LEFT JOIN restored r ON r.payee_key = c.payee_key
  LEFT JOIN email_changes e
    ON e.party_id = py.PAYEE_PARTY_ID AND e.email_changed_at <= c.changed_at
  GROUP BY c.payee_key, py.VENDOR_ID, c.changed_at, c.new_account, r.restored_at
),
gaps AS (
  SELECT *, {gap} AS email_gap_hours FROM changes
),
tells AS (
  SELECT *, COALESCE(email_gap_hours <= 72, FALSE) AS email_tell FROM gaps
),
-- An account a change of the takeover kind put on a storefront, while it stayed there.
taken AS (
  SELECT payee_key AS taken_payee, new_account, changed_at AS taken_at, restored_at AS undone_at
  FROM tells WHERE email_tell
),
onto_taken AS (
  SELECT DISTINCT t.payee_key
  FROM tells t
  JOIN taken k ON k.new_account = t.new_account AND k.taken_payee <> t.payee_key
   AND k.taken_at < t.changed_at AND (k.undone_at IS NULL OR k.undone_at > t.changed_at)
),
rule AS (
  SELECT t.*, (o.payee_key IS NOT NULL) AS onto_taken_account
  FROM tells t LEFT JOIN onto_taken o ON o.payee_key = t.payee_key
)"""


def _sql(select: str) -> dict:
    return {engine: _CHANGES.format(gap=gap) + "\n" + select
            for engine, gap in _GAP_HOURS.items()}


STEPS = [
    # 1. The payout-account changes in the audit trail: made through the portal, and the ones
    #    support restored after the storefront reported an unauthorised change.
    ("run_sql", {"sql": """\
SELECT CHANGE_TYPE, CHANGE_REASON, COUNT(*) AS changes,
       MIN(CHANGED_DATE) AS first_change, MAX(CHANGED_DATE) AS last_change
FROM XX_AUDIT_TRAIL
WHERE SOURCE_TABLE = 'IBY_PMT_INSTR_USES_ALL'
GROUP BY CHANGE_TYPE, CHANGE_REASON
ORDER BY changes DESC"""}),

    # 2. The pattern of the reported takeovers: how long before the bank change the contact
    #    e-mail last changed, reported (restored) against never restored.
    ("run_sql", {"sql": _sql("""\
SELECT restored_at IS NOT NULL AS restored,
       CASE WHEN email_gap_hours IS NULL THEN 'no e-mail change before it'
            WHEN email_gap_hours <= 72 THEN 'within 3 days'
            WHEN email_gap_hours <= 24 * 60 THEN '3 to 60 days'
            ELSE 'over 60 days' END AS email_changed_before_bank,
       COUNT(*) AS changes,
       ROUND(MIN(email_gap_hours) / 24, 2) AS min_gap_days,
       ROUND(MAX(email_gap_hours) / 24, 2) AS max_gap_days
FROM gaps
GROUP BY 1, 2
ORDER BY 1, 2""")}),

    # 3. Whether sharing an account is the tell: changes onto an account we were paying
    #    another supplier into at the time, against changes onto an account a takeover-like
    #    change had put on another storefront. Counts only.
    ("run_sql", {"sql": _sql("""\
, uses AS (
  SELECT CAST(EXT_PMT_PARTY_ID AS STRING) AS payee_key, CAST(INSTRUMENT_ID AS STRING) AS acct,
         START_DATE, END_DATE
  FROM IBY_PMT_INSTR_USES_ALL
),
shared AS (
  SELECT DISTINCT r.payee_key
  FROM rule r JOIN uses u ON u.acct = r.new_account AND u.payee_key <> r.payee_key
   AND u.START_DATE <= CAST(r.changed_at AS DATE)
   AND (u.END_DATE IS NULL OR u.END_DATE > CAST(r.changed_at AS DATE))
)
SELECT r.restored_at IS NOT NULL AS restored, r.email_tell,
       (s.payee_key IS NOT NULL) AS paying_another_supplier_then,
       r.onto_taken_account, COUNT(*) AS changes
FROM rule r LEFT JOIN shared s ON s.payee_key = r.payee_key
GROUP BY 1, 2, 3, 4
ORDER BY 1, 2, 3, 4""")}),

    # 4. The storefronts to hold: never restored, and taken over the reported way.
    ("run_sql", {"sql": _sql("""\
SELECT VENDOR_ID, CAST(changed_at AS DATE) AS bank_changed_on,
       ROUND(email_gap_hours, 1) AS email_gap_hours, email_tell, onto_taken_account
FROM rule
WHERE restored_at IS NULL AND (email_tell OR onto_taken_account)
ORDER BY VENDOR_ID""")}),

    # 5. Hold their payouts for account takeover.
    ("run_python", {"code": """\
import pandas as pd
from mission_control import MissionControl, Reason

hold = pd.read_parquet("results/sql_0004.parquet")
print(f"{len(hold)} storefronts to hold: {int(hold['email_tell'].sum())} with the e-mail "
      f"change, {int(hold['onto_taken_account'].sum())} onto an account a takeover put on "
      "another storefront")

mission_control = MissionControl()
mission_control.hold_payouts([int(v) for v in hold["VENDOR_ID"]],
                             reason=Reason.ACCOUNT_TAKEOVER)
mission_control.summary()
"""}),
]
