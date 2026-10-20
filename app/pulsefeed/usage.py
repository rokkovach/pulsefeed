"""Privacy-limited usage totals, attributed to invocation start time (UTC).

This module has no authorization or quota policy. Its caller must require admin
access. Worker usage already aggregates retries; never multiply it by attempts.
"""
from contextlib import closing
from datetime import datetime, timezone
import math
import time


MODELS = {"qwen": "nvidia/Qwen3-8B-NVFP4", "flux": "black-forest-labs/FLUX.2-klein-4B"}
METRICS = ("input_tokens", "output_tokens", "total_tokens", "image_count", "inference_attempts", "inference_retries")
MAX_SAFE_INTEGER = 9_007_199_254_740_991
# Python datetime's UTC year1..9999 range. SQLite nonnumeric/NULL/extreme
# historical timestamps remain in All time totals, but cannot be plotted.
MIN_TIMESTAMP = -62_135_596_800
MAX_TIMESTAMP = 253_402_300_800
DATED = f"typeof(created) IN ('integer','real') AND created >= {MIN_TIMESTAMP} AND created < {MAX_TIMESTAMP}"


def _utc_now(now):
    if now is None:
        now = time.time()
    if isinstance(now, datetime):
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return now.astimezone(timezone.utc)
    if isinstance(now, bool) or not isinstance(now, (int, float)):
        raise ValueError("now must be an aware datetime or Unix timestamp")
    try:
        if not math.isfinite(now):
            raise ValueError("now must be finite")
        return datetime.fromtimestamp(now, timezone.utc)
    except (OverflowError, OSError) as exc:
        raise ValueError("now is outside the supported timestamp range") from exc


def _iso(value):
    return value.isoformat().replace("+00:00", "Z") if value is not None else None


def _integer(path, maximum=MAX_SAFE_INTEGER):
    # SQLite's integer JSON type also covers out-of-range literals whose extracted
    # value is a REAL. Both checks are required; booleans/strings are never usage.
    value = f"json_extract(safe_output, '{path}')"
    return (f"CASE WHEN finalized AND json_type(safe_output, '{path}')='integer' "
            f"AND typeof({value})='integer' AND {value} BETWEEN 0 AND {maximum} "
            f"THEN {value} ELSE NULL END")


def _query(execution_mode, where):
    # The SQLite engine inspects output JSON, but Python receives only approved
    # scalar metadata. No input, article, prompt, error message, or identifier is
    # selected, and no join can multiply a persisted invocation.
    counter_limits = {"inference_attempts": 2, "inference_retries": 1}
    metrics = ",\n".join(
        f"{_integer('$.counters.' + name, counter_limits[name]) if name in counter_limits else _integer('$.usage.' + name, 1 if name == 'image_count' else MAX_SAFE_INTEGER)} AS {name}"
        for name in METRICS)
    return f"""
WITH selected AS (
  SELECT CASE WHEN {DATED} THEN created ELSE NULL END AS bucket_time,
    CASE WHEN stage IN ('qwen','flux') THEN stage ELSE 'unknown' END AS stage,
    CASE WHEN status='succeeded' THEN 'succeeded'
         WHEN status IN ('failed','operator_resolved') THEN 'failed'
         WHEN status IN ('prepared','submitting','pending','cancelling') THEN 'pending'
         ELSE 'unknown' END AS outcome,
    status IN ('succeeded','failed','operator_resolved') AS finalized,
    {execution_mode} AS recorded_mode,
    CASE WHEN json_valid(output) THEN
      CASE WHEN json_type(output)='object' THEN output ELSE '{{}}' END
      ELSE '{{}}' END AS safe_output
  FROM invocations {where}
)
SELECT stage, outcome, bucket_time,
  CASE WHEN stage='qwen' AND json_extract(safe_output,'$.model.id')='{MODELS['qwen']}' THEN '{MODELS['qwen']}'
       WHEN stage='flux' AND json_extract(safe_output,'$.model.id')='{MODELS['flux']}' THEN '{MODELS['flux']}'
       ELSE NULL END AS model_id,
  CASE WHEN json_extract(safe_output,'$.model.runtime')='fake-adapter-v1'
         OR json_extract(safe_output,'$.model.revision')='synthetic-fixture'
         OR json_extract(safe_output,'$.model.precision')='synthetic' THEN 'synthetic'
       WHEN recorded_mode IN ('synthetic','live') THEN recorded_mode
       ELSE 'unknown' END AS execution_mode,
  {metrics}
FROM selected
"""


def _new_row(stage, model_id, execution_mode):
    return {"stage": stage, "model_id": model_id, "execution_mode": execution_mode,
            "invocations": 0, "succeeded": 0, "failed": 0, "pending": 0, "unknown": 0,
            "tokens_applicable": stage == "qwen", "images_applicable": stage == "flux",
            **{name: {"total": None, "reported": 0, "missing": 0} for name in METRICS}}


def _add(row, source):
    row["invocations"] += 1
    row[source["outcome"]] += 1
    values = {name: source[name] for name in METRICS}
    # Do not fabricate totals from partial fields or trust inconsistent old totals.
    inp, out, total = (values[name] for name in ("input_tokens", "output_tokens", "total_tokens"))
    if inp is None or out is None or total != inp + out:
        values["total_tokens"] = None
    attempts, retries = values["inference_attempts"], values["inference_retries"]
    if attempts is not None and retries is not None and retries != max(attempts - 1, 0):
        values["inference_retries"] = None
    if values["inference_attempts"] == 0:
        for name in ("input_tokens", "output_tokens", "total_tokens", "image_count"):
            if values[name] not in (None, 0):
                values[name] = None
    for name, value in values.items():
        if name.endswith("tokens") and not row["tokens_applicable"]:
            continue
        if name == "image_count" and not row["images_applicable"]:
            continue
        metric = row[name]
        if value is None:
            metric["missing"] += 1
        else:
            metric["reported"] += 1
            metric["total"] = (metric["total"] or 0) + value


def _models(grouped):
    return sorted(grouped.values(), key=lambda row: (row["stage"], row["model_id"] or "", row["execution_mode"]))


def _group_add(grouped, source):
    key = (source["stage"], source["model_id"], source["execution_mode"])
    _add(grouped.setdefault(key, _new_row(*key)), source)


def _epoch_iso(value):
    if value == MAX_TIMESTAMP:
        # Expanded ISO8601 year keeps the last supported UTC day's exclusive
        # boundary representable, without overflowing Python datetime.
        return "+010000-01-01T00:00:00Z"
    return _iso(datetime.fromtimestamp(value, timezone.utc))


def aggregate_usage(db, window="today", now=None):
    """Return aggregate-only model totals and UTC invocation-start time buckets.

    Today is the half-open UTC day, including its unelapsed hourly buckets. All
    time preserves every historical invocation in totals; unplottable timestamps
    contribute only to ``timeline.unbucketed_invocations``. Valid history is never
    truncated: whole-day bucket widths expand to keep at most 120 buckets.
    Measurements use the same rules in totals/buckets, include final failures and
    actual zeroes, and never multiply worker-reported usage by retry counts.
    """
    if window not in {"today", "all"}:
        raise ValueError("window must be 'today' or 'all'")
    generated = _utc_now(now)
    today = generated.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    grouped = {}
    with closing(db.connect()) as connection:
        # The first data read fixes one snapshot for bounds, totals and buckets.
        connection.execute("BEGIN")
        columns = {column[1] for column in connection.execute("PRAGMA table_info(invocations)")}
        mode = "execution_mode" if "execution_mode" in columns else "NULL"
        if window == "today":
            start,end,width,unbucketed = int(today),int(today)+86400,3600,0
            where = f"WHERE {DATED} AND created >= ? AND created < ?"
            parameters = (start,end)
        else:
            bounds = connection.execute(f"SELECT MIN(CASE WHEN {DATED} THEN created END) AS first,MAX(CASE WHEN {DATED} THEN created END) AS last,COALESCE(SUM(CASE WHEN {DATED} THEN 0 ELSE 1 END),0) AS unbucketed FROM invocations").fetchone()
            start = (math.floor(bounds["first"])//86400)*86400 if bounds["first"] is not None else int(today)
            end = (math.floor(bounds["last"])//86400+1)*86400 if bounds["last"] is not None else start+86400
            days = (end-start)//86400
            width = max(1,(days+119)//120)*86400
            unbucketed = bounds["unbucketed"]
            where,parameters = "",()
        bucket_groups = [{} for _ in range((end-start+width-1)//width)]
        cursor = connection.execute(_query(mode, where), parameters)
        while rows := cursor.fetchmany(256):
            for source in rows:
                _group_add(grouped, source)
                if source["bucket_time"] is not None:
                    index = (math.floor(source["bucket_time"])-start)//width
                    _group_add(bucket_groups[index], source)
    timeline = {"bucket_seconds":width,"start":_epoch_iso(start),"end":_epoch_iso(end),
                "unbucketed_invocations":unbucketed,
                "buckets":[{"start":_epoch_iso(start+index*width),"end":_epoch_iso(min(end,start+(index+1)*width)),"models":_models(group)} for index,group in enumerate(bucket_groups)]}
    return {"window": window, "window_start": _epoch_iso(start) if window=="today" else None,
            "window_end": _epoch_iso(end) if window=="today" else None,
            "generated_at": _iso(generated), "models": _models(grouped), "timeline":timeline}
