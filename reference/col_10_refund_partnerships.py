"""Reference solution for col-10-refund-partnerships.

The question describes the scheme completely: a courier and a few of their regulars split
"never arrived" refunds on deliveries that reached the door, and the platform funds every one.
It also says what a partnership is NOT, and each of those has to be left alone at a price:
a thief (the disputed legs end away from the door, the claimants are strangers), a courier
with a bad week (many claims, all from strangers), a regular whose building has a package
thief (one regular, one to three claims), and the claimant who was right. Deactivating an
honest courier costs the margin they earn, against the refunds a partnership keeps taking, so
the answer is a precision problem: the rule has to say who a "regular" is and how many of them
make a partnership.

Everything needed is in three tables. ``XX_REFUNDS`` has the never-arrived refunds
(``REASON_CODE = 'ORDER_NOT_DELIVERED'``) and who funded them; ``OE_ORDER_HEADERS_ALL``
names the customer account (``SOLD_TO_ORG_ID``) and the ship-to site; ``XX_DELIVERY_LEGS``
says which courier delivered the order and where the leg ended (``TERMINUS_LATITUDE`` and
``TERMINUS_LONGITUDE``). For every never-arrived refund the query works out three facts about
the pair behind it: how many other orders this courier delivered to this customer (and how
many before the claim), and how far the leg's terminus is from where the customer's other
deliveries end.

The rule filed: a claim counts when the leg reached the door (terminus within 300 m of the
site's usual drop point, or nothing to compare it with) and the claimant is the courier's
regular (at least two other deliveries by this courier to this customer, at least one of them
before the claim); a courier with two or more distinct regulars claiming is a partnership,
and those regulars are the partners. One regular claiming is the building with a package
thief and is left alone; strangers' claims are the honest tail or theft, and are left alone.
The rule is applied inside the query, so no result lists anyone the reference does not file.

Score: 0.98 on the paper's warehouse (242 of 243 partnership couriers and 576 of 577 partners;
one partnership is missed because only one of its partners ever claimed) and 0.94 on the
released warehouse, where the same rule reaches fewer of the partners.
"""

QUESTION = "col-10-refund-partnerships"

#: The pairs behind every never-arrived refund, per engine: BigQuery measures the distance
#: with its geography functions, DuckDB with the haversine formula on the same sphere.
_PAIRS = """\
WITH claims AS (
  SELECT r.REFUND_ID, r.HEADER_ID, r.PLATFORM_FUNDED_AMOUNT,
         h.SOLD_TO_ORG_ID AS cust_account_id, h.SHIP_TO_ORG_ID AS ship_to, h.ORDERED_DATE,
         l.VENDOR_ID AS vendor_id,
         l.TERMINUS_LATITUDE AS lat, l.TERMINUS_LONGITUDE AS lon
  FROM XX_REFUNDS r
  JOIN OE_ORDER_HEADERS_ALL h USING (HEADER_ID)
  LEFT JOIN XX_DELIVERY_LEGS l ON l.HEADER_ID = r.HEADER_ID AND l.DELIVERED_DATE IS NOT NULL
  WHERE r.REASON_CODE = 'ORDER_NOT_DELIVERED'
),
legs AS (  -- every delivered order of a claimant's account: who delivered it, where it ended
  SELECT h.SOLD_TO_ORG_ID AS cust_account_id, h.SHIP_TO_ORG_ID AS ship_to, h.HEADER_ID,
         h.ORDERED_DATE, l.VENDOR_ID, l.TERMINUS_LATITUDE AS lat, l.TERMINUS_LONGITUDE AS lon
  FROM OE_ORDER_HEADERS_ALL h
  JOIN XX_DELIVERY_LEGS l USING (HEADER_ID)
  WHERE l.DELIVERED_DATE IS NOT NULL
    AND h.SOLD_TO_ORG_ID IN (SELECT DISTINCT cust_account_id FROM claims)
),
door AS (  -- the site's usual drop point: the median terminus of its undisputed deliveries
  SELECT g.ship_to, {median_lat} AS home_lat, {median_lon} AS home_lon
  FROM legs g
  LEFT JOIN claims c ON c.HEADER_ID = g.HEADER_ID
  WHERE c.HEADER_ID IS NULL
  GROUP BY g.ship_to
),
history AS (  -- the pair: this courier's other deliveries to this customer
  SELECT c.REFUND_ID,
         SUM(CASE WHEN g.ORDERED_DATE < c.ORDERED_DATE THEN 1 ELSE 0 END) AS prior_by_courier,
         COUNT(g.HEADER_ID) AS total_by_courier
  FROM claims c
  LEFT JOIN legs g ON g.cust_account_id = c.cust_account_id AND g.VENDOR_ID = c.vendor_id
                   AND g.HEADER_ID != c.HEADER_ID
  GROUP BY c.REFUND_ID
),
pairs AS (
  SELECT c.REFUND_ID, c.cust_account_id, c.vendor_id, c.PLATFORM_FUNDED_AMOUNT,
         CASE WHEN c.vendor_id IS NULL THEN 'no delivery record'
              WHEN {distance} > 300 THEN 'away from the door'
              WHEN hst.prior_by_courier >= 1 AND hst.total_by_courier >= 2 THEN 'regular'
              ELSE 'stranger' END AS kind
  FROM claims c
  LEFT JOIN history hst USING (REFUND_ID)
  LEFT JOIN door d USING (ship_to)
),
regulars AS (  -- per courier: the distinct regulars claiming at the door
  SELECT vendor_id, COUNT(DISTINCT cust_account_id) AS regulars_claiming
  FROM pairs WHERE kind = 'regular'
  GROUP BY vendor_id
)
"""

_DIALECT = {
    "bigquery": dict(
        median_lat="APPROX_QUANTILES(g.lat, 2)[OFFSET(1)]",
        median_lon="APPROX_QUANTILES(g.lon, 2)[OFFSET(1)]",
        distance="ST_DISTANCE(ST_GEOGPOINT(c.lon, c.lat), ST_GEOGPOINT(d.home_lon, d.home_lat))"),
    "duckdb": dict(
        median_lat="MEDIAN(g.lat)", median_lon="MEDIAN(g.lon)",
        distance=("2 * 6371008.8 * ASIN(SQRT(POWER(SIN(RADIANS(d.home_lat - c.lat) / 2), 2)"
                  " + COS(RADIANS(c.lat)) * COS(RADIANS(d.home_lat))"
                  " * POWER(SIN(RADIANS(d.home_lon - c.lon) / 2), 2)))")),
}


def _with_pairs(select: str) -> dict:
    return {engine: _PAIRS.format(**parts) + select for engine, parts in _DIALECT.items()}


STEPS = [
    # 1. What the never-arrived refunds are: how many, who funded them, and how many have no
    #    delivery record at all (nobody delivered: that is the theft review's, not ours).
    ("run_sql", {"sql": """\
SELECT r.CHANNEL_CODE,
       COUNT(*) AS refunds,
       ROUND(SUM(r.AMOUNT), 2) AS refunded_usd,
       ROUND(SUM(r.PLATFORM_FUNDED_AMOUNT), 2) AS platform_funded_usd,
       ROUND(SUM(r.MERCHANT_FUNDED_AMOUNT), 2) AS merchant_funded_usd,
       SUM(CASE WHEN l.HEADER_ID IS NULL THEN 1 ELSE 0 END) AS without_delivery_record
FROM XX_REFUNDS r
LEFT JOIN XX_DELIVERY_LEGS l ON l.HEADER_ID = r.HEADER_ID AND l.DELIVERED_DATE IS NOT NULL
WHERE r.REASON_CODE = 'ORDER_NOT_DELIVERED'
GROUP BY r.CHANNEL_CODE
ORDER BY refunds DESC"""}),

    # 2. Every claim sorted by the pair behind it, in aggregate: at the door or not, from the
    #    courier's regular or a stranger; and how many couriers have one, two, three...
    #    distinct regulars claiming. One is a building with a package thief; the long tail
    #    starting at two is the partnerships.
    ("run_sql", {"sql": _with_pairs("""\
SELECT 'claims' AS measure, kind AS bucket, COUNT(*) AS n,
       ROUND(SUM(PLATFORM_FUNDED_AMOUNT), 2) AS platform_funded_usd
FROM pairs GROUP BY kind
UNION ALL
SELECT 'couriers by regulars claiming', CAST(regulars_claiming AS STRING), COUNT(*), NULL
FROM regulars GROUP BY regulars_claiming
ORDER BY measure, bucket""")}),

    # 3. The partnerships: couriers with two or more regulars claiming at the door, and the
    #    regulars' claims. Only these rows leave the warehouse.
    ("run_sql", {"sql": _with_pairs("""\
SELECT p.vendor_id, p.cust_account_id, p.REFUND_ID, p.PLATFORM_FUNDED_AMOUNT
FROM pairs p
JOIN regulars g USING (vendor_id)
WHERE p.kind = 'regular' AND g.regulars_claiming >= 2""")}),

    # 4. File both sides.
    ("run_python", {"code": """\
import pandas as pd
from mission_control import MissionControl, Reason

print(pd.read_parquet("results/sql_0002.parquet").to_string(index=False), "\\n")
claims = pd.read_parquet("results/sql_0003.parquet")
couriers = claims.groupby("vendor_id").agg(
    regulars=("cust_account_id", "nunique"), claims=("REFUND_ID", "size"),
    refunded_usd=("PLATFORM_FUNDED_AMOUNT", "sum"))
partners = claims.groupby("cust_account_id").agg(
    couriers=("vendor_id", lambda s: sorted(set(int(v) for v in s))),
    claims=("REFUND_ID", "size"), refunded_usd=("PLATFORM_FUNDED_AMOUNT", "sum"))
print(len(couriers), "partnership couriers,", len(partners), "partner accounts,",
      len(claims), "claims,", f"${claims['PLATFORM_FUNDED_AMOUNT'].sum():,.2f} platform-funded")

mission_control = MissionControl()
mission_control.ban_couriers(
    [{"id": int(v), "evidence": {"regulars_claiming": int(r.regulars),
                                 "at_door_claims_from_regulars": int(r.claims),
                                 "platform_funded_refunds_usd": round(float(r.refunded_usd), 2)}}
     for v, r in couriers.iterrows()],
    reason=Reason.REFUND_COLLUSION)
mission_control.ban_customers(
    [{"id": int(a), "evidence": {"partner_courier_ids": r.couriers, "at_door_claims": int(r.claims),
                                 "refunded_usd": round(float(r.refunded_usd), 2)}}
     for a, r in partners.iterrows()],
    reason=Reason.REFUND_COLLUSION)
mission_control.note(
    "A never-arrived claim counts toward a partnership when the delivery record exists and "
    "puts the leg at the door (terminus within 300 m of where the customer's other deliveries "
    "end) and the claimant is the courier's regular (two or more other deliveries by this "
    "courier to this customer, at least one before the claim). A courier with two or more "
    "distinct regulars claiming is a partnership and those regulars are the partners. Not "
    "actioned: couriers with one regular claiming (a building with a package thief), claims "
    "from strangers (the honest tail, and thieves, who are another review's), and refunds "
    "with no delivery record.")
mission_control.summary()
"""}),
]
