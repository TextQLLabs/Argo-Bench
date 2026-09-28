"""The machinery behind `mission_control`: validation, batching, and the three recording
channels. Nothing here is part of the console's API — read `mission_control.py` for that.

Design notes for anyone extending this file:

* **Stdlib only.** It runs inside an analysis sandbox with no package installs.
* **Validate, then record.** Each method funnels through `_batch()`, which type-checks
  every item, and then `_file()`. Bad input raises `MissionControlError` at the call
  site, naming the offending item's index, instead of silently recording a malformed
  action — and nothing is filed unless the whole set passes, because a half-recorded
  batch is a decision the operator never made.
* **One request per decision set.** The batch is why: a review that closes ten thousand
  accounts is one POST, one marker line and one journal line, not ten thousand of each.
  Everything downstream reads the item list back out with `items_of()`.
* **Three recording channels, tried in order and never fatal.** An action is (1) POSTed
  to `target` if one is configured, (2) echoed to stdout as a single-line `[[MC]]`
  marker, and (3) appended to a local JSONL journal. Recording is best-effort by
  design: a network fault must never destroy an operator's session, so failures are
  counted and reported by `status()`, not raised.
"""

from __future__ import annotations

import builtins
import contextlib
import decimal
import http.client
import json
import numbers
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

#: 2.0 is the plural protocol: a payload carries `items` and `count` rather than one
#: entity's fields. Recorded on every action so a store holding both shapes — runs
#: graded before the change, runs graded after — can tell them apart.
PROTOCOL_VERSION = "2.0"

#: The singular methods this API replaced, and what to call instead. Kept so a call
#: written the old way fails with a fix rather than an AttributeError, and so the
#: harness can report it as invalid tool use instead of losing it.
SUPERSEDED = {
    "ban_merchant": "ban_merchants",
    "ban_courier": "ban_couriers",
    "ban_customer": "ban_customers",
    "discontinue_promo_code": "discontinue_promo_codes",
    "flag_ring": "flag_rings",
    "remediate_payment": "remediate_payments",
    "end_campaign": "end_campaigns",
    "report_campaign_performance": "report_campaign_performances",
    "report_balance": "report_balances",
    "post_journal_entry": "post_journal_entries",
    "file_adjustment": "file_adjustments",
    "report_rollforward": "report_rollforwards",
    "report_aging": "report_agings",
    "flag_order": "flag_orders",
    "report_pay_period": "report_pay_periods",
    "issue_pay_adjustment": "issue_pay_adjustments",
    "elect_method": "elect_methods",
    "report_metric": "report_metrics",
    "file_schedule": "file_schedules",
    "publish_data_source": "publish_data_sources",
}


def items_of(record: Mapping) -> list[dict]:
    """The decisions inside one filed action — an action or a payload, either way.

    The single reader of the batch payload. Everything that counts, scores or displays
    filed work goes through here rather than reaching for ``payload["items"]``, which is
    what lets a protocol 1.0 record — one action, one entity, no item list — still be
    read back as the one decision it was.
    """
    payload = record.get("payload") if isinstance(record.get("payload"), Mapping) else record
    if not isinstance(payload, Mapping):
        return []
    items = payload.get("items")
    if isinstance(items, Sequence) and not isinstance(items, (str, bytes)):
        return [dict(i) for i in items if isinstance(i, Mapping)]
    return [dict(payload)] if payload else []

# One line per action on stdout. The operator's transcript reader picks these up when the
# control plane is unreachable, so the marker must stay on a single line and stay stable.
STDOUT_MARKER = "[[MC]]"

DEFAULT_JOURNAL = "mission_control_actions.jsonl"


class MissionControlError(ValueError):
    """Raised at the call site when an action is malformed.

    Deliberately a ValueError: a bad action is bad input, not a transport fault, and it
    should stop the caller rather than be recorded and graded as a real decision.
    """


# --------------------------------------------------------------------------- action
@dataclass
class Action:
    """One filed decision. `seq` orders actions within a session."""

    seq: int
    kind: str
    payload: dict
    sandbox_id: str | None = None
    session_id: str = ""
    ts: float = field(default_factory=time.time)
    delivered: bool = False

    @property
    def n_items(self) -> int:
        """Decisions carried. One filing is one action; it is not one decision."""
        return len(items_of(self.payload))

    def to_dict(self) -> dict:
        return {
            "protocol": PROTOCOL_VERSION,
            "seq": self.seq,
            "kind": self.kind,
            "sandbox_id": self.sandbox_id,
            "session_id": self.session_id,
            "ts": self.ts,
            "payload": self.payload,
        }


# --------------------------------------------------------------------- sandbox identity
_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)


def _runtime_sandbox_id() -> str | None:
    """Call the runtime's own ``__get_sandbox_id()`` if this sandbox provides one.

    The analysis runtime injects that function as a global. It is a global of the
    *executing cell*, not of this module, so a plain name lookup here would miss it —
    hence the builtins check followed by a walk up the call stack to the frame that
    called into Mission Control.
    """
    probe = getattr(builtins, "__get_sandbox_id", None)
    if not callable(probe):
        frame = sys._getframe()
        depth = 0
        while frame is not None and depth < 50:
            candidate = frame.f_globals.get("__get_sandbox_id")
            if callable(candidate):
                probe = candidate
                break
            frame = frame.f_back
            depth += 1
    if not callable(probe):
        return None
    try:
        value = probe()
    except Exception:
        # A runtime accessor that raises is not worth failing an investigation over.
        return None
    text = str(value).strip() if value is not None else ""
    return text or None


def get_sandbox_id() -> str | None:
    """Identifier for the sandbox this code is running in.

    The operator's control plane uses it to attach filed actions to the session that
    produced them. It is shaped ``<org_id>-<chat_id>``.

    Resolution order, each step falling through when it yields nothing:

    1. ``MISSION_CONTROL_SANDBOX_ID`` — an explicit override always wins.
    2. ``__get_sandbox_id()`` — the runtime's own accessor. This is the normal path.
    3. ``SANDBOX_ID`` — an environment variable, if one is set.
    4. ``WEB_URL`` / ``WORKER_NAME`` — org and chat ids parsed out of a session URL.

    Returns None only when a runtime exposes none of these, which is not an error:
    actions are still recorded, and the control plane correlates them by session instead.
    """
    override = os.environ.get("MISSION_CONTROL_SANDBOX_ID", "").strip()
    if override:
        return override

    from_runtime = _runtime_sandbox_id()
    if from_runtime:
        return from_runtime

    for var in ("SANDBOX_ID",):
        value = os.environ.get(var, "").strip()
        if value:
            return value

    for var in ("WEB_URL", "WORKER_NAME"):
        raw = os.environ.get(var, "").strip()
        if not raw:
            continue
        ids = _UUID_RE.findall(raw)
        if len(ids) >= 2:
            return f"{ids[0]}-{ids[1]}"
        if len(ids) == 1:
            return ids[0]
    return None


def _hostname() -> str:
    try:
        return socket.gethostname()
    except OSError:
        return "unknown"


# --------------------------------------------------------------------- validation
def _entity_id(value: Any, field_name: str = "id") -> int:
    """Entity ids are integers. Accept an all-digit string, reject everything else.

    A float id is rejected rather than truncated: 371778.0 arriving from a dataframe is
    fine, 371778.6 is a bug the caller needs to see.
    """
    if isinstance(value, bool):
        raise MissionControlError(f"{field_name} must be an integer id, got a bool")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        if value != int(value):
            raise MissionControlError(f"{field_name} must be a whole number, got {value!r}")
        parsed = int(value)
    elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
        parsed = int(value.strip())
    else:
        raise MissionControlError(
            f"{field_name} must be an integer id, got {type(value).__name__} {value!r}"
        )
    if parsed <= 0:
        raise MissionControlError(f"{field_name} must be positive, got {parsed}")
    return parsed


def _reason(value: Any) -> str:
    if isinstance(value, Reason):
        return value.value
    if isinstance(value, str):
        try:
            return Reason(value.strip().upper()).value
        except ValueError:
            pass
    known = ", ".join(sorted(r.value for r in Reason))
    raise MissionControlError(
        f"reason must be a Reason member, got {value!r}. Use Reason.OTHER with a note= "
        f"if nothing fits. Known reasons: {known}"
    )


def _money(value: Any, field_name: str) -> float:
    """Money as a float rounded to the cent.

    Rounding here rather than at comparison time means an operator who computed
    1200.0000000001 files 1200.00, and two operators who agree to the cent record
    identical values.
    """
    if isinstance(value, bool) or value is None:
        raise MissionControlError(f"{field_name} must be a number, got {value!r}")
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise MissionControlError(
            f"{field_name} must be a number, got {type(value).__name__} {value!r}"
        ) from None
    if amount != amount or amount in (float("inf"), float("-inf")):
        raise MissionControlError(f"{field_name} must be finite, got {value!r}")
    return round(amount, 2)


def _finite(value: Any, field_name: str) -> float:
    """A finite number, as a float, unrounded. Money goes through ``_money``; this is
    for counts, rates and anything else whose unit is not dollars."""
    if isinstance(value, bool) or value is None:
        raise MissionControlError(f"{field_name} must be a number, got {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise MissionControlError(
            f"{field_name} must be a number, got {type(value).__name__} {value!r}"
        ) from None
    if number != number or number in (float("inf"), float("-inf")):
        raise MissionControlError(f"{field_name} must be finite, got {value!r}")
    return number


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?$")
_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_QUARTER_RE = re.compile(r"^\d{4}-Q[1-4]$")


def _timestamp(value: Any, field_name: str) -> str:
    """ISO-8601 date or datetime, as text. Naive stamps are read as America/New_York."""
    text = str(value).strip().replace("Z", "")
    if not _DATE_RE.match(text):
        raise MissionControlError(
            f"{field_name} must be ISO-8601 (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS), "
            f"got {value!r}"
        )
    return text


def _period(value: Any, field_name: str = "period") -> str:
    text = str(value).strip().upper().replace("/", "-")
    if _PERIOD_RE.match(text):
        return text
    raise MissionControlError(f"{field_name} must be an accounting period YYYY-MM, got {value!r}")


_POLICY_SOURCE_LIMIT = 40_000


def _text(value: Any, field_name: str, *, max_len: int = 2000, required: bool = True) -> str:
    if value is None:
        if required:
            raise MissionControlError(f"{field_name} is required")
        return ""
    text = str(value).strip()
    if required and not text:
        raise MissionControlError(f"{field_name} is required")
    return text[:max_len]


def _id_list(values: Any, field_name: str) -> list[int]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise MissionControlError(f"{field_name} must be a sequence of integer ids, got {values!r}")
    out = [_entity_id(v, f"{field_name}[{i}]") for i, v in enumerate(values)]
    if not out:
        raise MissionControlError(f"{field_name} must not be empty")
    return out


def _sequence(values: Any, field_name: str) -> list:
    """The items of a batch call. A bare id is the commonest mistake, so it is named."""
    if isinstance(values, Mapping):
        raise MissionControlError(
            f"{field_name} must be a sequence of items, got a single mapping — wrap it: "
            f"[{{...}}]"
        )
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise MissionControlError(
            f"{field_name} must be a sequence of items — every method files the complete "
            f"set in one call, e.g. ban_customers([101, 102], reason=Reason.PROMO_FARMING). "
            f"Got {type(values).__name__} {values!r}"
        )
    # An empty batch is filed, not rejected: "we looked and there is nothing" is a
    # finding, and it is not the same as never having called.
    return list(values)


def _item_kwargs(item: Any, shared: Mapping, primary: str,
                 aliases: Mapping[str, str] | None, where: str) -> dict:
    """One item's arguments: the call's shared ones, overridden by the item's own.

    A bare value is the item's ``primary`` field, which is what makes
    ``ban_customers([101, 102], reason=...)`` and a list of dicts the same call.
    """
    if item is None:
        raise MissionControlError(
            f"{where} is None; every item must be an id or a mapping of the item's fields"
        )
    if isinstance(item, Mapping):
        own: dict = {}
        for key, value in item.items():
            name = str(key)
            name = (aliases or {}).get(name, name)
            own[name] = value
    else:
        own = {primary: item}
    merged = {**dict(shared), **own}
    # A shared argument left unset must not shadow the builder's own default.
    return {k: v for k, v in merged.items() if v is not None}


def _evidence(value: Any) -> dict | None:
    """Optional supporting detail. Must be JSON-serialisable so it survives transport."""
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise MissionControlError(f"evidence must be a dict, got {type(value).__name__}")
    try:
        json.dumps(value, default=str)
    except (TypeError, ValueError) as exc:
        raise MissionControlError(f"evidence must be JSON-serialisable: {exc}") from None
    return dict(value)


def _column_names(values: Any, field_name: str) -> list[str]:
    """The columns that identify a row of a schedule: distinct names, at least one.

    A bare string is one column. Without a key a row can be matched to nothing — not by
    the operator reading the schedule, and not by whoever reconciles it afterwards.
    """
    if isinstance(values, str):
        values = [values]
    if isinstance(values, Mapping) or not isinstance(values, Iterable):
        raise MissionControlError(
            f"{field_name} must be a list of column names, got {type(values).__name__} "
            f"{values!r}"
        )
    names = [_text(v, f"{field_name}[{i}]", max_len=120) for i, v in enumerate(values)]
    if not names:
        raise MissionControlError(f"{field_name} must name at least one column")
    if len(set(names)) != len(names):
        raise MissionControlError(f"{field_name} repeats a column: {names!r}")
    return names


def _cell(value: Any, where: str) -> Any:
    """One cell of a schedule row: text, a number, a boolean or None.

    That is what JSON carries and what a reviewer can compare, so anything else is
    either coerced to it or refused naming the fix. NaN — the dataframe idiom for a
    missing value — is filed as None: JSON has no NaN, and a payload the control plane
    cannot parse is a schedule the operator never sees. Decimals and numpy scalars are
    numbers; dates are filed as ISO text.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return _text(value, where, max_len=500, required=False)
    if isinstance(value, numbers.Integral):
        return int(value)
    if isinstance(value, (numbers.Real, decimal.Decimal)):
        number = float(value)
        if number != number:
            return None
        if number in (float("inf"), float("-inf")):
            raise MissionControlError(f"{where} must be finite, got {value!r}")
        return number
    if callable(getattr(value, "isoformat", None)):
        return str(value.isoformat())
    raise MissionControlError(
        f"{where} must be text, a number, a boolean or None, got {type(value).__name__} "
        f"{value!r} — cast it before filing (str(), float(), .isoformat())"
    )


# ------------------------------------------------------------------ data sources
#: Cell types a data-source contract may declare. ``month`` is an accounting month
#: (YYYY-MM); ``date`` has no time of day; ``timestamp`` is naive America/New_York.
DATA_SOURCE_TYPES = ("string", "integer", "number", "boolean", "date", "month", "timestamp")

#: Where the contracts for this workspace's dashboards are looked for: an explicit path
#: in the environment, then a file beside the session, then the control plane.
CONTRACTS_ENV = "MISSION_CONTROL_CONTRACTS"
#: A live tile is called on bindings its question never listed; this names where they are.
RENDER_SET_ENV = "MISSION_CONTROL_RENDER_SET"
DEFAULT_CONTRACTS = "mission_control_contracts.json"

#: A dashboard extract is small by construction. The cap is a guard on the transport —
#: one filing is one request and one marker line — not a modelling opinion.
MAX_SOURCE_ROWS = 20000
_MAX_PROBLEMS = 12

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MIDNIGHT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T]00:00(:00(\.0+)?)?$")
_STAMP_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2})(:\d{2})?(\.\d+)?$")


def _is_missing(value: Any) -> bool:
    """None, NaN and NaT — the three ways a dataframe says "no value"."""
    if value is None:
        return True
    try:
        if value != value:  # NaN, and pandas' NaT
            return True
    except (TypeError, ValueError):
        return False
    return False


def _iso(value: Any) -> Any:
    """Dates, datetimes, pandas Timestamps and Periods as the text they print as."""
    if isinstance(value, str):
        return value.strip()
    if callable(getattr(value, "isoformat", None)):
        return str(value.isoformat())
    if type(value).__name__ == "Period":
        return str(value)
    return value


def _typed_cell(value: Any, kind: str) -> Any:
    """One cell coerced to its declared type, or a ``ValueError`` saying why not.

    Coercion stops where meaning would change: ``3.0`` is the integer 3 and a midnight
    timestamp is its date, because that is what a dataframe does to an integer column
    with a gap or a DATE read through a driver — but ``"3"`` is not a number and
    ``12.5`` is not an integer, and a tile bound to the column would break on either.
    """
    if kind == "boolean":
        if isinstance(value, bool) or type(value).__name__ == "bool_":
            return bool(value)
        raise ValueError("must be a boolean")
    if kind in ("integer", "number"):
        if isinstance(value, bool) or type(value).__name__ == "bool_" or not isinstance(
                value, (numbers.Real, decimal.Decimal)):
            raise ValueError(f"must be {'an integer' if kind == 'integer' else 'a number'}, "
                             f"got {type(value).__name__} — cast the column before publishing")
        number = float(value)
        if number in (float("inf"), float("-inf")):
            raise ValueError("must be finite")
        if kind == "number":
            return number
        if number != int(number):
            raise ValueError("must be a whole number")
        return int(value)
    text = _iso(value)
    if not isinstance(text, str):
        raise ValueError(f"must be text, got {type(value).__name__} — cast it (astype(str))")
    if kind == "string":
        return text[:500]
    if kind == "date":
        midnight = _MIDNIGHT_RE.match(text)
        if midnight:
            return midnight.group(1)
        if _DAY_RE.match(text):
            return text
        raise ValueError("must be a date, YYYY-MM-DD")
    if kind == "month":
        if _PERIOD_RE.match(text):
            return text
        raise ValueError("must be a month, YYYY-MM — e.g. strftime('%Y-%m')")
    if kind == "timestamp":
        stamp = _STAMP_RE.match(text)
        if stamp:
            return f"{stamp.group(1)} {stamp.group(2)}{stamp.group(3) or ':00'}"
        if _DAY_RE.match(text):
            return f"{text} 00:00:00"
        raise ValueError("must be a timestamp, YYYY-MM-DD HH:MM:SS")
    raise ValueError(f"has an unknown declared type {kind!r}")


def _frame_records(frame: Any, where: str) -> list[dict]:
    """A dataframe, or anything row-shaped, as a list of row dicts.

    Takes what an analyst has in hand — a pandas or polars DataFrame as it is, the
    ``records``, ``split`` or ``list`` orientation of ``to_dict``, or plain row dicts —
    so nobody has to remember which export spelling the console wants.
    """
    if callable(getattr(frame, "to_dicts", None)):            # polars
        return list(frame.to_dicts())
    if callable(getattr(frame, "to_dict", None)) and hasattr(frame, "columns"):   # pandas
        return list(frame.to_dict("records"))
    if isinstance(frame, Mapping):
        if "columns" in frame and "data" in frame:            # to_dict("split")
            columns = [str(c) for c in frame["columns"]]
            return [dict(zip(columns, row)) for row in frame["data"]]
        values = list(frame.values())
        if values and all(isinstance(v, Sequence) and not isinstance(v, (str, bytes))
                          for v in values):                   # to_dict("list")
            if len({len(v) for v in values}) > 1:
                raise MissionControlError(f"{where}: columns have different lengths")
            names = [str(c) for c in frame]
            return [dict(zip(names, row)) for row in zip(*values)]
        raise MissionControlError(
            f"{where} must be a dataframe or a list of row dicts, got a single mapping")
    if isinstance(frame, (str, bytes)) or not isinstance(frame, Iterable):
        raise MissionControlError(
            f"{where} must be a dataframe or a list of row dicts, got {type(frame).__name__}")
    rows = list(frame)
    for i, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise MissionControlError(
                f"{where}[{i}] must be a dict of column -> value, got {type(row).__name__}")
    return [dict(r) for r in rows]


def _index_hint(frame: Any, missing: Sequence[str]) -> str:
    names = [str(n) for n in getattr(getattr(frame, "index", None), "names", None) or [] if n]
    hidden = [c for c in missing if c in names]
    return (f" — {hidden} {'is' if len(hidden) == 1 else 'are'} in the dataframe's index; "
            f"call reset_index() first") if hidden else ""


def _near(name: str, candidates: Iterable[str]) -> str:
    import difflib

    folded = {c.lower(): c for c in candidates}
    hit = difflib.get_close_matches(name.lower(), list(folded), n=1, cutoff=0.6)
    return folded[hit[0]] if hit else ""


def _order_spec(order_by: Sequence[str]) -> list[tuple[str, bool]]:
    """``["-gmv_usd", "merchant_id"]`` or ``"gmv_usd desc"`` as (column, descending)."""
    out = []
    for term in order_by or ():
        text = str(term).strip()
        descending = text.startswith("-") or text.lower().endswith(" desc")
        column = re.sub(r"(?i)\s+(asc|desc)$", "", text.lstrip("+-")).strip()
        out.append((column, descending))
    return out


def _first_unsorted(rows: list[dict], spec: list[tuple[str, bool]]) -> int | None:
    """Index of the first row that sorts before its predecessor, or None."""
    for i in range(1, len(rows)):
        for column, descending in spec:
            a, b = rows[i - 1].get(column), rows[i].get(column)
            if a == b:
                continue
            if a is None or b is None:      # nulls last, either direction
                if a is None:
                    return i
                break
            if (a < b) if descending else (a > b):
                return i
            break
    return None


def _conform(contract: Mapping, frame: Any, where: str) -> list[list]:
    """One frame against its contract: the rows, in contract column order, or an error
    that lists everything wrong with it.

    Everything, not the first thing: a frame with a misnamed column, a float where an
    integer belongs and a duplicated key is three fixes, and an endpoint that reports
    them one refusal at a time turns them into three round trips.
    """
    records = _frame_records(frame, where)
    columns = [dict(c) for c in contract.get("columns") or []]
    names = [str(c["name"]) for c in columns]
    problems: list[str] = []

    seen: list[str] = []
    for row in records:
        for column in row:
            if str(column) not in seen:
                seen.append(str(column))
    missing = [n for n in names if n not in seen]
    unexpected = [n for n in seen if n not in names]
    if missing and records:
        hints = [f"{m!r} (did you mean to rename {_near(m, unexpected)!r}?)"
                 if _near(m, unexpected) else repr(m) for m in missing]
        problems.append(f"missing column(s) {', '.join(hints)}{_index_hint(frame, missing)}")
    if unexpected:
        problems.append(f"unexpected column(s) {unexpected} — the contract has exactly "
                        f"{names}; drop or rename the rest")

    typed: list[dict] = []
    bad_cells: dict[str, str] = {}
    for i, row in enumerate(records):
        out: dict = {}
        for column in columns:
            name, kind = str(column["name"]), str(column.get("type") or "string")
            if name in missing:
                continue
            value = row.get(name)
            if _is_missing(value):
                out[name] = None
                if not column.get("nullable") and name not in bad_cells:
                    bad_cells[name] = f"column {name!r} is not nullable, but rows[{i}] is null"
                continue
            try:
                cell = _typed_cell(value, kind)
                allowed = column.get("values")
                if allowed and cell not in allowed:
                    raise ValueError(f"must be one of {list(allowed)}")
                low, high = column.get("minimum"), column.get("maximum")
                if low is not None and cell < low:
                    raise ValueError(f"must be >= {low}")
                if high is not None and cell > high:
                    raise ValueError(f"must be <= {high}")
            except ValueError as exc:
                bad_cells.setdefault(
                    name, f"column {name!r} ({kind}) {exc}: rows[{i}] has {value!r}")
                cell = None
            out[name] = cell
        typed.append(out)
    problems.extend(bad_cells.values())

    key = [str(k) for k in contract.get("key") or [] if str(k) not in missing]
    if key and not bad_cells:
        first: dict[tuple, int] = {}
        for i, row in enumerate(typed):
            identity = tuple(row.get(k) for k in key)
            if identity in first:
                problems.append(f"rows[{first[identity]}] and rows[{i}] share the key "
                                f"{dict(zip(key, identity))} — the grain is one row per "
                                f"{key}")
                break
            first[identity] = i

    if not typed and not contract.get("allow_empty"):
        problems.append("has no rows — a tile with nothing to show; check the filters that "
                        "built it (a date compared with text matches nothing)")
    expected_rows = contract.get("row_count")
    if typed and expected_rows is not None and len(typed) != int(expected_rows):
        problems.append(f"has {len(typed)} row(s); the contract fixes it at {expected_rows}")
    ceiling = min(int(contract.get("max_rows") or MAX_SOURCE_ROWS), MAX_SOURCE_ROWS)
    if len(typed) > ceiling:
        problems.append(f"has {len(typed)} rows; the most this data source takes is {ceiling} "
                        f"— check the grain")
    spec = [(c, d) for c, d in _order_spec(contract.get("order_by") or ()) if c not in missing]
    if spec and not bad_cells:
        at = _first_unsorted(typed, spec)
        if at is not None:
            problems.append(f"is not sorted by {list(contract.get('order_by'))}: rows[{at}] "
                            f"sorts before rows[{at - 1}]")
    if problems:
        shown = problems[:_MAX_PROBLEMS]
        more = f" (+{len(problems) - len(shown)} more)" if len(problems) > len(shown) else ""
        raise MissionControlError(
            f"{where} does not match the contract: "
            + "; ".join(f"({n}) {p}" for n, p in enumerate(shown, 1)) + more)
    return [[row.get(n) for n in names] for row in typed]


def _binding(contract: Mapping, params: Any, where: str) -> dict:
    """One frame's parameter values, typed and checked against the declared domains."""
    declared = [dict(p) for p in contract.get("parameters") or []]
    if not isinstance(params, Mapping):
        raise MissionControlError(f"{where}.params must be a dict of parameter -> value, got "
                                  f"{type(params).__name__}")
    given = {str(k): v for k, v in params.items()}
    names = [str(p["name"]) for p in declared]
    if sorted(given) != sorted(names):
        raise MissionControlError(f"{where}.params names {sorted(given)}; the contract's "
                                  f"parameters are {names}")
    out = {}
    for parameter in declared:
        name = str(parameter["name"])
        try:
            value = _typed_cell(given[name], str(parameter.get("type") or "string"))
        except ValueError as exc:
            raise MissionControlError(f"{where}.params[{name!r}] {exc}: got "
                                      f"{given[name]!r}") from None
        allowed = parameter.get("values")
        if allowed and value not in allowed:
            raise MissionControlError(f"{where}.params[{name!r}] must be one of "
                                      f"{list(allowed)}, got {value!r}")
        out[name] = value
    return out


def _loose_rows(frame: Any, where: str) -> tuple[list[str], list[list]]:
    """A frame with no contract to hold it to: cells that survive JSON, one column set."""
    records = _frame_records(frame, where)
    names: list[str] = []
    for row in records:
        for column in row:
            if str(column) not in names:
                names.append(str(column))
    if len(records) > MAX_SOURCE_ROWS:
        raise MissionControlError(f"{where} has {len(records)} rows; the most a data source "
                                  f"takes is {MAX_SOURCE_ROWS}")
    rows = [[_cell(None if _is_missing(row.get(n)) else row.get(n), f"{where}[{i}][{n!r}]")
             for n in names] for i, row in enumerate(records)]
    return names, rows


def _contracts_by_name(document: Any) -> dict[str, dict]:
    """``{"data_sources": [...]}``, a bare list, or a name -> contract mapping."""
    if isinstance(document, Mapping) and "data_sources" in document:
        document = document["data_sources"]
    if isinstance(document, Mapping):
        document = [{**dict(v), "name": v.get("name", k)} for k, v in document.items()
                    if isinstance(v, Mapping)]
    out: dict[str, dict] = {}
    for contract in document or []:
        if isinstance(contract, Mapping) and contract.get("name"):
            out[str(contract["name"])] = dict(contract)
    return out


def _contract_sha(contract: Mapping) -> str:
    import hashlib

    graded = {k: contract.get(k) for k in ("name", "columns", "key", "parameters", "bindings",
                                           "order_by", "row_count", "allow_empty")}
    return hashlib.sha256(json.dumps(graded, sort_keys=True, default=str)
                          .encode("utf-8")).hexdigest()[:12]


def _kwargs_text(values: Mapping) -> str:
    return ", ".join(f"{k}={v!r}" for k, v in values.items())


#: Bound by `mission_control` when it defines the enum: the reasons live beside the API
#: that documents them, and this module validates against them.
Reason: Any = None


class _Console:
    """Everything `MissionControl` is built on; its methods are not the API."""

    def __init__(
        self,
        id: str | None = None,
        target: str | None = None,
        *,
        session_id: str | None = None,
        dry_run: bool = False,
        timeout_s: float = 20.0,
        journal_path: str | None = DEFAULT_JOURNAL,
        verbose: bool = True,
        emit_markers: bool = True,
        contracts: Any = None,
    ) -> None:
        self.sandbox_id = id or get_sandbox_id()
        self.target = (target or os.environ.get("MISSION_CONTROL_TARGET") or "").rstrip("/") or None
        self.session_id = (session_id or os.environ.get("MISSION_CONTROL_SESSION")
                           or uuid.uuid4().hex)
        self.dry_run = bool(dry_run)
        self.timeout_s = float(timeout_s)
        self.journal_path = journal_path
        self.verbose = bool(verbose)
        # An offline replay of already-filed actions must not re-emit their markers, or
        # the replaying process's own stdout looks like a fresh session.
        self.emit_markers = bool(emit_markers)

        # Data-source contracts: a mapping or a path given here, else resolved on first
        # use (see `data_source_contracts`).
        self._contracts_source = contracts
        self._contracts: dict[str, dict] | None = None

        self.actions: list[Action] = []
        self.delivery_errors: list[str] = []
        self._seq = 0
        self._opened = False
        self._greeted = False
        # The session's one connection to the control plane, and the lock that keeps two
        # threads from interleaving requests on it. http.client is not thread-safe, and a
        # console is easy to call from a worker.
        self._conn: Any = None
        self._path_prefix = ""
        self._send_lock = threading.Lock()

    # ------------------------------------------------------------------ introspection
    def __repr__(self) -> str:
        return (
            f"MissionControl(sandbox_id={self.sandbox_id!r}, target={self.target!r}, "
            f"actions={len(self.actions)})"
        )

    def __getattr__(self, name: str) -> Any:
        """Turn a call to a retired singular method into the fix for it.

        Without this, ``mc.ban_customer(...)`` is an AttributeError halfway through an
        investigation, which reads as a broken console rather than as a renamed method.
        """
        replacement = SUPERSEDED.get(name)
        if replacement is None:
            raise AttributeError(
                f"{type(self).__name__} has no attribute {name!r}; "
                f"see help(MissionControl) for the methods it does have"
            )

        def _superseded(*_args, **_kwargs):
            raise MissionControlError(
                f"{name}() was replaced by {replacement}(), which files the complete set "
                f"in one call: pass every item as a list. "
                f"See help(MissionControl.{replacement})."
            )

        _superseded.__name__ = name
        return _superseded

    # ------------------------------------------------------------------ batching
    def _batch(self, kind: str, build: Any, values: Any, shared: dict, *,
               primary: str, aliases: Mapping[str, str] | None = None,
               field_name: str = "items") -> Action:
        """Validate every item, then file the whole set as one action.

        ``build`` is the per-item validator for this kind. It runs once per item and
        raises on the first bad one, naming its index — a set of ten thousand bans in
        which item 4,812 carries a float id is fixable only if the error says 4,812.

        Nothing is filed unless every item passes. A half-recorded batch would be a
        decision the operator never made.
        """
        items: list[dict] = []
        for i, raw in enumerate(_sequence(values, field_name)):
            where = f"{field_name}[{i}]"
            kwargs = _item_kwargs(raw, shared, primary, aliases, where)
            try:
                built = build(**kwargs)
            except MissionControlError as exc:
                raise MissionControlError(f"{where}: {exc}") from None
            except TypeError as exc:
                # "missing a required argument", "unexpected keyword argument" — the
                # item's shape is wrong. Reported against the plural method the caller
                # actually wrote, not the private builder behind it.
                text = (str(exc)
                        .replace(f"{build.__qualname__}()", f"{type(self).__name__}.{kind}()")
                        .replace(f"{build.__name__}()", f"{kind}()"))
                raise MissionControlError(f"{where}: {text}") from None
            except ValueError as exc:
                # A conversion that raised on its own — `float("high")` on a non-money
                # metric. It is still one bad item in a set, and the index is what makes
                # it findable.
                raise MissionControlError(f"{where}: {exc}") from None
            items.append({k: v for k, v in built.items() if v not in (None, "")})
        return self._file(kind, {"items": items, "count": len(items)})

    # ---------------------------------------------------------------- fraud & abuse
    @staticmethod
    def _merchant_ban(id: Any, reason: Any, evidence: Mapping | None = None) -> dict:
        return {"merchant_id": _entity_id(id, "merchant id"), "reason": _reason(reason),
                "evidence": _evidence(evidence)}

    @staticmethod
    def _courier_ban(id: Any, reason: Any, evidence: Mapping | None = None) -> dict:
        return {"driver_id": _entity_id(id, "courier id"), "reason": _reason(reason),
                "evidence": _evidence(evidence)}

    @staticmethod
    def _customer_ban(id: Any, reason: Any, evidence: Mapping | None = None) -> dict:
        return {"customer_id": _entity_id(id, "customer id"), "reason": _reason(reason),
                "evidence": _evidence(evidence)}

    @staticmethod
    def _payout_hold(vendor_id: Any, reason: Any, amount: Any = None,
                     note: str | None = None) -> dict:
        built = {"vendor_id": _entity_id(vendor_id, "vendor id"), "reason": _reason(reason),
                 "note": _text(note, "note", required=False)}
        if amount is not None:
            built["amount"] = _money(amount, "amount")
        return built

    @staticmethod
    def _promo_code_stop(id: Any, reason: Any) -> dict:
        return {"promo_code_id": _entity_id(id, "promo code id"), "reason": _reason(reason)}

    @staticmethod
    def _ring(actor_ids: Sequence[Any], actor_type: Any, reason: Any,
              evidence: Mapping | None = None) -> dict:
        kind = _text(actor_type, "actor_type").lower()
        if kind not in {"customer", "driver", "merchant", "device", "card"}:
            raise MissionControlError(
                "actor_type must be one of customer, driver, merchant, device, card; "
                f"got {actor_type!r}"
            )
        return {"actor_type": kind, "actor_ids": _id_list(actor_ids, "actor_ids"),
                "reason": _reason(reason), "evidence": _evidence(evidence)}

    @staticmethod
    def _payment_remedy(order_id: Any, action: Any, amount: Any = None,
                        reason: Any = "OTHER", note: str | None = None) -> dict:
        verb = _text(action, "action").lower()
        allowed = {"refund", "recapture", "void", "write_off", "chargeback_represent"}
        if verb not in allowed:
            raise MissionControlError(
                f"action must be one of {', '.join(sorted(allowed))}; got {action!r}"
            )
        built = {
            "order_id": _entity_id(order_id, "order_id"),
            "action": verb,
            "reason": _reason(reason),
            "note": _text(note, "note", required=False),
        }
        if amount is not None:
            built["amount"] = _money(amount, "amount")
        return built

    # ------------------------------------------------------------------- campaigns
    @staticmethod
    def _campaign_end(id: Any, reason: Any, evidence: Mapping | None = None) -> dict:
        return {"campaign_id": _entity_id(id, "campaign id"), "reason": _reason(reason),
                "evidence": _evidence(evidence)}

    @staticmethod
    def _campaign_performance(id: Any, incremental_cm: Any, spend: Any,
                              cm_per_dollar: Any = None, rank: Any = None) -> dict:
        cm = _money(incremental_cm, "incremental_cm")
        spent = _money(spend, "spend")
        if cm_per_dollar is None:
            # Zero-spend campaigns have no per-dollar figure; recording None beats
            # recording a division by zero.
            ratio = round(cm / spent, 6) if spent else None
        else:
            ratio = round(float(cm_per_dollar), 6)
        built = {
            "campaign_id": _entity_id(id, "campaign id"),
            "incremental_cm": cm,
            "spend": spent,
            "cm_per_dollar": ratio,
        }
        if rank is not None:
            built["rank"] = _entity_id(rank, "rank")
        return built

    # ------------------------------------------------------------------ accounting
    @staticmethod
    def _balance(account: Any, as_of: Any, amount: Any, basis: str = "gl",
                 note: str | None = None) -> dict:
        source = _text(basis, "basis").lower()
        if source not in {"gl", "subledger", "recomputed"}:
            raise MissionControlError(
                f"basis must be gl, subledger, or recomputed; got {basis!r}"
            )
        return {"account": _text(account, "account", max_len=120),
                "as_of": _timestamp(as_of, "as_of"),
                "amount": _money(amount, "amount"),
                "basis": source,
                "note": _text(note, "note", required=False)}

    @staticmethod
    def _journal_entry(lines: Sequence[Mapping], period: Any, memo: Any,
                       source: str = "MISSION_CONTROL") -> dict:
        if isinstance(lines, Mapping) or not isinstance(lines, Sequence) or not lines:
            raise MissionControlError("lines must be a non-empty sequence of line dicts")

        parsed: list[dict] = []
        debits = credits = 0.0
        for i, raw in enumerate(lines):
            if not isinstance(raw, Mapping):
                raise MissionControlError(f"lines[{i}] must be a dict, got {type(raw).__name__}")
            account = _text(raw.get("account") or raw.get("code_combination"),
                            f"lines[{i}].account", max_len=120)
            has_debit = raw.get("debit") is not None
            has_credit = raw.get("credit") is not None
            if has_debit == has_credit:
                raise MissionControlError(
                    f"lines[{i}] needs exactly one of debit or credit "
                    f"(got debit={raw.get('debit')!r}, credit={raw.get('credit')!r})"
                )
            line = {"account": account}
            if has_debit:
                line["debit"] = _money(raw["debit"], f"lines[{i}].debit")
                debits += line["debit"]
            else:
                line["credit"] = _money(raw["credit"], f"lines[{i}].credit")
                credits += line["credit"]
            if raw.get("description"):
                line["description"] = _text(raw["description"], f"lines[{i}].description",
                                            max_len=500, required=False)
            parsed.append(line)

        if round(debits - credits, 2) != 0.0:
            raise MissionControlError(
                f"journal entry does not balance: debits {debits:.2f} vs credits "
                f"{credits:.2f} (difference {debits - credits:+.2f})"
            )
        return {"period": _period(period), "memo": _text(memo, "memo", max_len=500),
                "source": _text(source, "source", max_len=60),
                "lines": parsed, "total_debits": round(debits, 2),
                "total_credits": round(credits, 2)}

    @staticmethod
    def _adjustment(account: Any, period: Any, amount: Any, reason: Any,
                    subledger_amount: Any = None, gl_amount: Any = None,
                    note: str | None = None) -> dict:
        built = {
            "account": _text(account, "account", max_len=120),
            "period": _period(period),
            "amount": _money(amount, "amount"),
            "reason": _reason(reason),
            "note": _text(note, "note", required=False),
        }
        if gl_amount is not None:
            built["gl_amount"] = _money(gl_amount, "gl_amount")
        if subledger_amount is not None:
            built["subledger_amount"] = _money(subledger_amount, "subledger_amount")
        if "gl_amount" in built and "subledger_amount" in built:
            implied = round(built["subledger_amount"] - built["gl_amount"], 2)
            if implied != built["amount"]:
                raise MissionControlError(
                    f"amount {built['amount']:.2f} does not equal subledger "
                    f"{built['subledger_amount']:.2f} - GL {built['gl_amount']:.2f} "
                    f"= {implied:.2f}"
                )
        return built

    @staticmethod
    def _rollforward(account: Any, period: Any, opening: Any, closing: Any,
                     movements: Mapping[str, Any]) -> dict:
        if not isinstance(movements, Mapping) or not movements:
            raise MissionControlError("movements must be a non-empty dict of name -> amount")
        moves = {
            _text(name, "movement name", max_len=80): _money(value, f"movements[{name!r}]")
            for name, value in movements.items()
        }
        open_amt = _money(opening, "opening")
        close_amt = _money(closing, "closing")
        implied = round(open_amt + sum(moves.values()), 2)
        if implied != close_amt:
            raise MissionControlError(
                f"roll-forward does not tie: opening {open_amt:.2f} + movements "
                f"{sum(moves.values()):+.2f} = {implied:.2f}, but closing is "
                f"{close_amt:.2f} (off by {close_amt - implied:+.2f})"
            )
        return {"account": _text(account, "account", max_len=120), "period": _period(period),
                "opening": open_amt, "closing": close_amt, "movements": moves}

    @staticmethod
    def _aging(account: Any, as_of: Any, buckets: Mapping[str, Any],
               dimension: str = "age_days") -> dict:
        if not isinstance(buckets, Mapping) or not buckets:
            raise MissionControlError("buckets must be a non-empty dict of label -> amount")
        parsed = {
            _text(label, "bucket label", max_len=120): _money(value, f"buckets[{label!r}]")
            for label, value in buckets.items()
        }
        return {"account": _text(account, "account", max_len=120),
                "as_of": _timestamp(as_of, "as_of"),
                "dimension": _text(dimension, "dimension", max_len=60),
                "buckets": parsed,
                "total": round(sum(parsed.values()), 2)}

    @staticmethod
    def _order_flag(order_id: Any, issue: Any, amount: Any = None, period: Any = None,
                    note: str | None = None) -> dict:
        built = {
            "order_id": _entity_id(order_id, "order_id"),
            "issue": _reason(issue),
            "note": _text(note, "note", required=False),
        }
        if amount is not None:
            built["amount"] = _money(amount, "amount")
        if period is not None:
            built["period"] = _period(period)
        return built

    # -------------------------------------------------------------- driver pay
    @staticmethod
    def _pay_period(driver_id: Any, pay_period: Any, countable_pay: Any, obligation: Any,
                    method: Any, top_up: Any = None) -> dict:
        election = _text(method, "method").lower()
        if election not in {"standard", "alternative"}:
            raise MissionControlError(
                f"method must be 'standard' or 'alternative'; got {method!r}"
            )
        countable = _money(countable_pay, "countable_pay")
        owed = _money(obligation, "obligation")
        return {
            "driver_id": _entity_id(driver_id, "driver_id"),
            "pay_period": _text(pay_period, "pay_period", max_len=40),
            "countable_pay": countable,
            "obligation": owed,
            "method": election,
            "top_up": (_money(top_up, "top_up") if top_up is not None
                       else round(max(0.0, owed - countable), 2)),
        }

    @staticmethod
    def _pay_adjustment(driver_id: Any, amount: Any, reason: Any,
                        pay_period: Any = None, note: str | None = None) -> dict:
        value = _money(amount, "amount")
        if value == 0.0:
            raise MissionControlError(
                "amount must be nonzero — a courier owed nothing is not an adjustment; "
                "file an empty batch to record that you checked and none were needed"
            )
        built = {
            "driver_id": _entity_id(driver_id, "courier id"),
            "amount": value,
            "reason": _reason(reason),
            "note": _text(note, "note", required=False),
        }
        if pay_period is not None:
            built["pay_period"] = _text(pay_period, "pay_period", max_len=40)
        return built

    @staticmethod
    def _election(pay_period: Any, method: Any, standard_cost: Any, alternative_cost: Any,
                  note: str | None = None) -> dict:
        election = _text(method, "method").lower()
        if election not in {"standard", "alternative"}:
            raise MissionControlError(
                f"method must be 'standard' or 'alternative'; got {method!r}"
            )
        return {"pay_period": _text(pay_period, "pay_period", max_len=40),
                "method": election,
                "standard_cost": _money(standard_cost, "standard_cost"),
                "alternative_cost": _money(alternative_cost, "alternative_cost"),
                "note": _text(note, "note", required=False)}

    # -------------------------------------------------------------------- generic
    @staticmethod
    def _metric(name: Any, value: Any, unit: str = "usd",
                dimensions: Mapping | None = None) -> dict:
        return {"name": _text(name, "name", max_len=120),
                "value": _money(value, "value") if unit == "usd" else float(value),
                "unit": _text(unit, "unit", max_len=24),
                "dimensions": _evidence(dimensions)}

    # -------------------------------------------------------------------- forecasts
    @staticmethod
    def _forecast(name: Any, point: Any, lower: Any, upper: Any, level: Any = 0.8,
                  unit: str = "usd", horizon: Any = None) -> dict:
        title = _text(name, "name", max_len=120)
        unit_text = _text(unit, "unit", max_len=24)
        number = _money if unit_text == "usd" else _finite
        median, low, high = (number(point, "point"), number(lower, "lower"),
                             number(upper, "upper"))
        if not low <= median <= high:
            raise MissionControlError(
                f"the interval must bracket the point: lower <= point <= upper, got "
                f"lower={low}, point={median}, upper={high}"
            )
        coverage = _finite(level, "level")
        if not 0.0 < coverage < 1.0:
            raise MissionControlError(
                f"level is the interval's nominal coverage as a fraction in (0, 1), "
                f"e.g. 0.8 for an 80% interval; got {level!r}"
            )
        built = {"name": title, "point": median, "lower": low, "upper": high,
                 "level": coverage, "unit": unit_text}
        if horizon is not None:
            text = str(horizon).strip()
            if _PERIOD_RE.match(text.upper()) or _QUARTER_RE.match(text.upper()):
                built["horizon"] = text.upper()
            else:
                built["horizon"] = _timestamp(text, "horizon")
        return built

    # --------------------------------------------------------------------- policies
    @staticmethod
    def _policy(name: Any, source: Any, entrypoint: Any = None, claims: Any = None,
                level: Any = 0.8, note: Any = None) -> dict:
        title = _text(name, "name", max_len=120)
        if not isinstance(source, str):
            raise MissionControlError(
                f"source must be the policy's Python source as one string, got "
                f"{type(source).__name__} — file the text of the module, not the function "
                f"object (inspect.getsource(fn), or the string you exec'd)")
        if len(source) > _POLICY_SOURCE_LIMIT:
            raise MissionControlError(
                f"source is {len(source):,} characters; the limit is "
                f"{_POLICY_SOURCE_LIMIT:,}. A policy is a decision rule, not a lookup "
                f"table of the evenings it was tried on")
        entry = _text(entrypoint, "entrypoint", max_len=80, required=False)
        try:
            tree = compile(source, "<policy>", "exec", flags=0x400, dont_inherit=True)
        except SyntaxError as exc:
            raise MissionControlError(
                f"source does not compile: {exc.msg} (line {exc.lineno})") from None
        defined = {node.name for node in tree.body
                   if type(node).__name__ in ("FunctionDef", "AsyncFunctionDef")}
        if not defined:
            raise MissionControlError(
                "source defines no top-level function; a policy is the function the "
                "lab calls (its ENTRYPOINT), defined at module level")
        if entry and entry not in defined:
            raise MissionControlError(
                f"source defines no top-level function {entry!r} (it defines "
                f"{sorted(defined)}); the lab calls {entry}(obs)")
        coverage = _finite(level, "level")
        if not 0.0 < coverage < 1.0:
            raise MissionControlError(
                f"level is the claims' nominal coverage as a fraction in (0, 1); got {level!r}")
        built = {"name": title, "source": source, "entrypoint": entry, "level": coverage,
                 "note": _text(note, "note", max_len=4000, required=False)}
        if claims:
            rows = ([{"metric": k, **v} if isinstance(v, Mapping) else {"metric": k, "point": v}
                     for k, v in claims.items()] if isinstance(claims, Mapping) else claims)
            parsed = []
            for j, raw in enumerate(_sequence(rows, "claims")):
                if not isinstance(raw, Mapping):
                    raise MissionControlError(f"claims[{j}] must be a dict, got {raw!r}")
                metric = _text(raw.get("metric") or raw.get("name"), f"claims[{j}].metric",
                               max_len=120)
                point = _finite(raw.get("point"), f"claims[{j}].point")
                low = _finite(raw.get("lower", raw.get("lo", point)), f"claims[{j}].lower")
                high = _finite(raw.get("upper", raw.get("hi", point)), f"claims[{j}].upper")
                if not low <= point <= high:
                    raise MissionControlError(
                        f"claims[{j}]: the interval must bracket the point, got "
                        f"lower={low}, point={point}, upper={high}")
                parsed.append({"metric": metric, "point": point, "lower": low, "upper": high})
            built["claims"] = parsed
        return built

    @staticmethod
    def _schedule(name: Any, key_columns: Any, rows: Any, as_of: Any = None) -> dict:
        title = _text(name, "name", max_len=120)
        keys = _column_names(key_columns, "key_columns")
        if isinstance(rows, (Mapping, str, bytes)) or not isinstance(rows, Iterable):
            raise MissionControlError(
                "rows must be a sequence of row dicts — from a dataframe, "
                f"rows=df.to_dict('records'); got {type(rows).__name__}"
            )
        parsed: list[dict] = []
        for i, raw in enumerate(rows):
            if not isinstance(raw, Mapping):
                raise MissionControlError(
                    f"rows[{i}] must be a dict of column -> value, got {type(raw).__name__}"
                )
            row = {
                _text(column, f"rows[{i}] column name", max_len=120):
                    _cell(value, f"rows[{i}][{column!r}]")
                for column, value in raw.items()
            }
            missing = [k for k in keys if row.get(k) in (None, "")]
            if missing:
                raise MissionControlError(
                    f"rows[{i}] is missing key column(s) {missing}: every row must carry "
                    f"every key column, or it can be matched to nothing"
                )
            parsed.append(row)
        built = {"name": title, "key_columns": keys, "rows": parsed,
                 "row_count": len(parsed)}
        if as_of is not None:
            built["as_of"] = _timestamp(as_of, "as_of")
        return built

    def _render_set(self, contract: Mapping) -> list[dict] | None:
        """The bindings a live tile is called on beyond the ones its contract lists.

        None when they cannot be had — no file named, no control plane — which is filed
        as such, so the publication is judged on what it could be asked.
        """
        if contract.get("hidden_bindings") is not None:
            return [dict(b) for b in contract["hidden_bindings"]]
        name, document = str(contract.get("name")), None
        path = os.environ.get(RENDER_SET_ENV)
        if path and os.path.isfile(path):
            with contextlib.suppress(OSError, ValueError), open(path, encoding="utf-8") as fh:
                document = json.load(fh)
        if document is None and self.target and not self.dry_run:
            document = self._get("/v1/render-set")
        if not isinstance(document, Mapping):
            return None
        bindings = (document.get("render_set") or {}).get(name)
        return [dict(b) for b in bindings] if isinstance(bindings, list) else []

    def _load_contracts(self) -> dict[str, dict]:
        source = self._contracts_source
        if isinstance(source, Mapping):
            return _contracts_by_name(source)
        candidates = [source, os.environ.get(CONTRACTS_ENV), DEFAULT_CONTRACTS,
                      os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   DEFAULT_CONTRACTS)]
        for candidate in candidates:
            if candidate and os.path.isfile(str(candidate)):
                try:
                    with open(str(candidate), encoding="utf-8") as fh:
                        return _contracts_by_name(json.load(fh))
                except (OSError, ValueError):
                    continue
        if self.target and not self.dry_run:
            fetched = self._get("/v1/contracts")
            if isinstance(fetched, Mapping):
                return _contracts_by_name(fetched)
        return {}

    def _data_source(self, name: Any, frame: Any = None, frames: Any = None,
                     loader: Any = None, definition: Any = None) -> dict:
        title = _text(name, "name", max_len=120)
        contracts = self.data_source_contracts()
        contract = (self._contracts or {}).get(title)
        if contract is None and contracts:
            near = _near(title, contracts)
            raise MissionControlError(
                f"no data source named {title!r} is expected here; the contracts are "
                f"{sorted(contracts)}" + (f" — did you mean {near!r}?" if near else ""))
        given = [k for k, v in (("frame", frame), ("frames", frames), ("loader", loader))
                 if v is not None]
        if len(given) != 1:
            raise MissionControlError(
                f"{title!r}: pass exactly one of frame= (one table), frames= (one table per "
                f"parameter binding) or loader= (a function of the parameters); got "
                f"{given or 'none'}")
        parameters = [str(p["name"]) for p in (contract or {}).get("parameters") or []]
        hidden: list[dict] | None = []
        if (contract or {}).get("loader_required"):
            if loader is None:
                raise MissionControlError(
                    f"{title!r} is a live tile: a viewer can set {parameters} to any value in "
                    f"their domains, so it is published as loader=<a Python function of "
                    f"{', '.join(parameters)} returning the frame>, from run_python — frames= "
                    f"cannot answer a binding nobody has picked yet")
            hidden = self._render_set(contract)

        if loader is not None:
            if not callable(loader):
                raise MissionControlError(f"{title!r}: loader must be callable")
            if contract is None or not contract.get("bindings"):
                raise MissionControlError(
                    f"{title!r}: a loader is called once per binding the contract lists, and "
                    f"no contract with bindings is available here — pass frames= instead")
            frames = []
            for binding in [*contract["bindings"], *(hidden or [])]:
                try:
                    frames.append({"params": dict(binding), "frame": loader(**binding)})
                except MissionControlError:
                    raise
                except Exception as exc:
                    raise MissionControlError(
                        f"{title!r}: loader({_kwargs_text(binding)}) raised "
                        f"{type(exc).__name__}: {exc}") from None
            if definition is None:
                import inspect
                with contextlib.suppress(OSError, TypeError):
                    definition = inspect.getsource(loader)
        elif frame is not None:
            if parameters:
                raise MissionControlError(
                    f"{title!r} is parameterized by {parameters}: pass frames=[{{'params': "
                    f"{{...}}, 'frame': df}}, ...] with one frame per binding, or loader=")
            frames = [{"params": {}, "frame": frame}]
        elif isinstance(frames, (Mapping, str, bytes)) or not isinstance(frames, Iterable):
            raise MissionControlError(
                f"{title!r}: frames must be a list of {{'params': {{...}}, 'frame': df}}")

        built: list[dict] = []
        columns: list[str] = [str(c["name"]) for c in (contract or {}).get("columns") or []]
        for i, entry in enumerate(frames):
            where = f"{title!r} frames[{i}]"
            if not isinstance(entry, Mapping):
                raise MissionControlError(f"{where} must be {{'params': {{...}}, 'frame': df}}, "
                                          f"got {type(entry).__name__}")
            data = entry.get("frame", entry.get("rows"))
            if data is None:
                raise MissionControlError(f"{where} has no 'frame'")
            if contract is None:
                params = {str(k): _cell(v, f"{where}.params[{k!r}]")
                          for k, v in dict(entry.get("params") or {}).items()}
                names, rows = _loose_rows(data, where)
                if columns and names != columns:
                    raise MissionControlError(
                        f"{where} has columns {names}, frames[0] has {columns} — every "
                        f"binding of one data source has the same columns")
                columns = columns or names
            else:
                params = _binding(contract, entry.get("params") or {}, where)
                label = f"{where} ({_kwargs_text(params)})" if params else repr(title)
                rows = _conform(contract, data, label)
            frame_out = {"params": params, "rows": rows, "row_count": len(rows)}
            if contract is not None and params in [
                    _binding(contract, b, where) for b in (hidden or [])]:
                frame_out["hidden"] = True
            built.append(frame_out)

        if contract is not None:
            filed = [json.dumps(f["params"], sort_keys=True) for f in built]
            repeated = sorted({b for b in filed if filed.count(b) > 1})
            if repeated:
                raise MissionControlError(f"{title!r}: binding(s) filed twice: {repeated}")
            wanted = [json.dumps(_binding(contract, b, f"{title!r} contract binding"),
                                 sort_keys=True)
                      for b in [*(contract.get("bindings") or []), *(hidden or [])]]
            if wanted:
                absent = [b for b in wanted if b not in filed]
                extra = [b for b in filed if b not in wanted]
                if absent or extra:
                    raise MissionControlError(
                        f"{title!r}: the dashboard renders {len(wanted)} binding(s) and every "
                        f"one needs its frame"
                        + (f"; missing {absent}" if absent else "")
                        + (f"; not in the contract {extra}" if extra else ""))
        out = {"name": title, "columns": columns, "frames": built,
               "contract": "verified" if contract is not None else "unverified"}
        if contract is not None and contract.get("loader_required"):
            # Whether the loader met the bindings its question never listed. When they
            # could not be had, the publication is judged on the visible ones alone.
            out["render_set"] = "full" if hidden is not None else "visible-only"
        if contract is not None:
            out["contract_sha"] = _contract_sha(contract)
            # Enough of the contract for a reader of the filing alone — the live console
            # drawing the tile — to know what each column is without the task in hand.
            out["schema"] = {
                "title": contract.get("title"),
                "types": [str(c.get("type") or "string") for c in contract.get("columns") or []],
                "key": list(contract.get("key") or []),
                "order_by": list(contract.get("order_by") or []),
            }
        if definition is not None:
            out["definition"] = _text(definition, "definition", max_len=20000)
        return out

    # --------------------------------------------------------------------- transport
    def _file(self, kind: str, payload: dict) -> Action:
        payload = {k: v for k, v in payload.items() if v not in (None, "")}
        self._seq += 1
        action = Action(seq=self._seq, kind=kind, payload=payload,
                        sandbox_id=self.sandbox_id, session_id=self.session_id)
        self.actions.append(action)

        self._emit_marker(action)
        self._journal(action)
        if self.target and not self.dry_run:
            if not self._greeted:
                self._greeted = True
                self.handshake()
            self._deliver(action)
        return action

    def _emit_marker(self, action: Action) -> None:
        """One line, flushed. The transcript reader needs it intact even if the cell
        is later truncated, so it goes out immediately rather than at interpreter exit."""
        if not self.emit_markers:
            return
        try:
            sys.stdout.write(f"{STDOUT_MARKER} {json.dumps(action.to_dict(), default=str)}\n")
            sys.stdout.flush()
        except Exception:  # pragma: no cover - stdout should never break an action
            pass

    def _journal(self, action: Action) -> None:
        if not self.journal_path:
            return
        try:
            with open(self.journal_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(action.to_dict(), default=str) + "\n")
        except OSError as exc:
            self.delivery_errors.append(f"journal: {exc}")

    def _deliver(self, action: Action) -> None:
        action.delivered = self._post(action.to_dict())

    def _connect(self) -> Any:
        """The session's HTTP connection to the control plane, opened on first use.

        **One connection per session, not one per action.** A tunnel in front of the
        plane counts connections, not requests, and opening a fresh TLS connection for
        every filed action exhausts that budget: measured, a burst of bans stopped being
        forwarded at around the hundredth. Holding one connection open costs nothing and
        removes the ceiling.
        """
        if self._conn is not None:
            return self._conn
        parts = urllib.parse.urlsplit(self.target)
        host = parts.netloc
        opener = (http.client.HTTPSConnection if parts.scheme == "https"
                  else http.client.HTTPConnection)
        # http.client ignores the proxy environment; a sandbox without its own DNS needs it.
        proxy = urllib.request.getproxies().get(parts.scheme)
        if proxy and not urllib.request.proxy_bypass(parts.hostname or host):
            via = urllib.parse.urlsplit(proxy if "//" in proxy else f"//{proxy}")
            self._conn = opener(via.hostname, via.port or 80, timeout=self.timeout_s)
            self._conn.set_tunnel(parts.hostname,
                                  parts.port or (443 if parts.scheme == "https" else 80))
        else:
            self._conn = opener(host, timeout=self.timeout_s)
        # Anything after the host is a prefix every request carries — the run token or
        # slot label the operator handed out.
        self._path_prefix = parts.path.rstrip("/")
        return self._conn

    def _drop(self) -> None:
        """Forget the connection so the next attempt opens a fresh one."""
        conn, self._conn = self._conn, None
        if conn is not None:
            with contextlib.suppress(Exception):  # closing an already-dead socket
                conn.close()

    def _post(self, record: dict) -> bool:
        body = json.dumps(record, default=str).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": f"mission-control/{PROTOCOL_VERSION}",
        }
        # Two attempts. A pooled connection can be closed at the far end between actions
        # — an idle timeout, a tunnel recycling — and that is not a delivery failure, it
        # is a reconnect. Only the second failure is reported.
        last: Exception | None = None
        for attempt in (1, 2):
            try:
                with self._send_lock:
                    conn = self._connect()
                    conn.request("POST", f"{self._path_prefix}/v1/actions", body, headers)
                    resp = conn.getresponse()
                    resp.read()
                    ok = 200 <= resp.status < 300
                if not self._opened and self.verbose:
                    self._opened = True
                    print(f"mission control: connected to {self.target} "
                          f"(sandbox {self.sandbox_id or 'unidentified'})")
                return ok
            except (http.client.HTTPException, OSError, TimeoutError) as exc:
                last = exc
                self._drop()
                if attempt == 1:
                    continue
        # Never fatal: the marker and the journal already hold this action, and an
        # operator mid-investigation should not lose a session to a network blip.
        self.delivery_errors.append(
            f"{record.get('kind')} seq {record.get('seq')}: {type(last).__name__}: {last}"
        )
        if self.verbose and len(self.delivery_errors) == 1:
            print(f"mission control: control plane unreachable ({last}); "
                  f"actions are being recorded locally instead")
        return False

    def _get(self, path: str) -> Any:
        """One JSON read from the control plane, or None. Never fatal, like `_post`."""
        try:
            with self._send_lock:
                conn = self._connect()
                conn.request("GET", f"{self._path_prefix}{path}",
                             headers={"User-Agent": f"mission-control/{PROTOCOL_VERSION}",
                                      "ngrok-skip-browser-warning": "1"})
                resp = conn.getresponse()
                body = resp.read()
            return json.loads(body.decode("utf-8")) if 200 <= resp.status < 300 else None
        except (http.client.HTTPException, OSError, TimeoutError, ValueError):
            self._drop()
            return None

    def close(self) -> None:
        """Release the control-plane connection. Filing again reopens it."""
        self._drop()

    def handshake(self) -> dict:
        """Announce the session to the control plane and report what came back.

        Sends a small, fully disclosed block describing the runtime: the hostname, the
        working directory, the Python version, and the values of ``WEB_URL`` and
        ``WORKER_NAME`` when the runtime sets them. Those last two are how a sandbox
        identifies itself, and the operator needs them to attach this session's actions
        to the run that started it. Nothing else about the environment is read, and no
        other variable is sent.

        Called automatically before the first action; calling it directly is a way to
        check connectivity before doing real work.
        """
        runtime = {
            "hostname": _hostname(),
            "cwd": os.getcwd(),
            "python": sys.version.split()[0],
            "web_url": os.environ.get("WEB_URL", ""),
            "worker_name": os.environ.get("WORKER_NAME", ""),
            "resolved_sandbox_id": self.sandbox_id,
        }
        record = {"protocol": PROTOCOL_VERSION, "kind": "handshake",
                  "sandbox_id": self.sandbox_id, "session_id": self.session_id,
                  "ts": time.time(), "payload": runtime}
        if self.target and not self.dry_run:
            self._post(record)
        return runtime

    def flush(self) -> dict:
        """Retry undelivered actions. Returns `status()`."""
        if self.target and not self.dry_run:
            for action in self.actions:
                if not action.delivered:
                    self._deliver(action)
        return self.status()

    # Context-manager use guarantees a flush even when an analysis raises.
    def __enter__(self) -> _Console:
        return self

    def __exit__(self, *exc_info) -> None:
        self.flush()
        if self.verbose:
            print(f"mission control: {self.summary()}")

