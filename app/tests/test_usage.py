import json
from datetime import datetime, timezone, timedelta
import sqlite3

import pytest

from pulsefeed.usage import MODELS, aggregate_usage


NOW = datetime(2026, 10, 14, 12, tzinfo=timezone.utc)
START = NOW.replace(hour=0).timestamp()
PRIVATE = ("private-account-a", "private-account-b", "private-feed-url", "private-feed-title",
           "private-article-title", "private-article-excerpt", "private-prompt", "private-provider-id", "private-run-id")


class UsageDatabase:
    """Production-shaped SQLite data with intentionally private unrelated fields."""
    def __init__(self, path, mode_column=True):
        self.path, self.sql = str(path), []
        mode = ",execution_mode TEXT" if mode_column else ""
        with self.connect() as connection:
            connection.executescript(f"""
              CREATE TABLE accounts(id TEXT PRIMARY KEY);
              CREATE TABLE feeds(id TEXT PRIMARY KEY,owner TEXT,url TEXT,title TEXT);
              CREATE TABLE items(id TEXT PRIMARY KEY,feed_id TEXT,title TEXT,excerpt TEXT);
              CREATE TABLE runs(id TEXT PRIMARY KEY,owner TEXT,item_id TEXT,original TEXT,qwen_prompt TEXT);
              CREATE TABLE invocations(id TEXT PRIMARY KEY,run_id TEXT,stage TEXT,provider_id TEXT,
                status TEXT,input TEXT,output TEXT,created REAL{mode});
            """)
            for suffix in ("a", "b"):
                owner = f"private-account-{suffix}"
                connection.execute("INSERT INTO accounts VALUES(?)", (owner,))
                connection.execute("INSERT INTO feeds VALUES(?,?,?,?)", (f"feed-{suffix}", owner, PRIVATE[2], PRIVATE[3]))
                connection.execute("INSERT INTO items VALUES(?,?,?,?)", (f"item-{suffix}", f"feed-{suffix}", PRIVATE[4], PRIVATE[5]))
                connection.execute("INSERT INTO runs VALUES(?,?,?,?,?)", (f"private-run-id-{suffix}", owner, f"item-{suffix}", json.dumps({"title": PRIVATE[4]}), PRIVATE[6]))

    def connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.set_trace_callback(self.sql.append)
        return connection


@pytest.fixture
def usage_db(tmp_path):
    return UsageDatabase(tmp_path / "usage.db")


def output(stage="qwen", *, usage=None, attempts=1, model_id=None, synthetic=False):
    return {"model": {"id": model_id if model_id is not None else MODELS[stage],
                      "revision": "synthetic-fixture" if synthetic else "pinned-revision",
                      "precision": "synthetic" if synthetic else "NVFP4" if stage == "qwen" else "bfloat16",
                      "runtime": "fake-adapter-v1" if synthetic else "pinned-runtime"},
            "usage": usage if usage is not None else {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
            "counters": {"inference_attempts": attempts,
                         "inference_retries": max(attempts - 1, 0) if type(attempts) is int else None},
            "data": {"prompt": PRIVATE[6]}, "error": {"message": PRIVATE[4]},
            "run_id": PRIVATE[8], "invocation_id": "private-invocation-id"}


def insert(db, identifier, *, stage="qwen", status="succeeded", data=None,
           mode="live", created=START + 1, owner="a", raw=False, old=False):
    value = data if raw or data is None else json.dumps(data)
    columns = "id,run_id,stage,provider_id,status,input,output,created"
    values = [identifier, f"private-run-id-{owner}", stage, PRIVATE[7], status,
              json.dumps({"article": PRIVATE[4], "system_prompt": PRIVATE[6]}), value, created]
    if not old:
        columns += ",execution_mode"; values.append(mode)
    with db.connect() as connection:
        connection.execute(f"INSERT INTO invocations({columns}) VALUES({','.join('?' for _ in values)})", values)


def model(report, *, stage="qwen", mode="live", identity="default"):
    identity = MODELS[stage] if identity == "default" else identity
    return next(row for row in report["models"] if (row["stage"], row["execution_mode"], row["model_id"]) == (stage, mode, identity))


def test_multi_visitor_totals_include_failed_retry_measurements_without_multiplication_or_private_data(usage_db):
    insert(usage_db, "private-first-id", data=output(usage={"input_tokens": 40, "output_tokens": 10, "total_tokens": 50}, attempts=2))
    insert(usage_db, "private-second-id", owner="b", status="failed", data=output(usage={"input_tokens": 20, "output_tokens": 0, "total_tokens": 20}))
    report = aggregate_usage(usage_db, now=NOW)
    row = model(report)
    assert row["invocations"] == 2
    assert (row["succeeded"], row["failed"], row["pending"], row["unknown"]) == (1, 1, 0, 0)
    assert row["input_tokens"] == {"total": 60, "reported": 2, "missing": 0}
    assert row["output_tokens"] == {"total": 10, "reported": 2, "missing": 0}
    assert row["total_tokens"] == {"total": 70, "reported": 2, "missing": 0}
    assert row["inference_attempts"] == {"total": 3, "reported": 2, "missing": 0}
    assert row["inference_retries"] == {"total": 1, "reported": 2, "missing": 0}
    assert row["image_count"] == {"total": None, "reported": 0, "missing": 0}
    encoded = json.dumps(report, allow_nan=False)
    assert all(secret not in encoded for secret in (*PRIVATE, "private-first-id", "private-second-id", "private-invocation-id"))
    assert aggregate_usage(usage_db, now=NOW) == report  # Repeated polling is a pure read.


def test_partial_measurements_actual_zero_and_unreported_model_are_distinct(usage_db):
    insert(usage_db, "partial", data=output(usage={"input_tokens": 12, "output_tokens": None, "total_tokens": None}, attempts=None))
    insert(usage_db, "zero", status="failed", data=output(usage={"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}, attempts=0))
    insert(usage_db, "unreported", status="pending", data=None)
    report = aggregate_usage(usage_db, now=NOW)
    row = model(report)
    assert row["input_tokens"] == {"total": 12, "reported": 2, "missing": 0}
    assert row["output_tokens"] == {"total": 0, "reported": 1, "missing": 1}
    assert row["total_tokens"] == {"total": 0, "reported": 1, "missing": 1}
    assert row["inference_attempts"] == {"total": 0, "reported": 1, "missing": 1}
    assert row["inference_retries"] == {"total": 0, "reported": 1, "missing": 1}
    unreported = model(report, identity=None)
    assert unreported["pending"] == 1
    assert unreported["input_tokens"] == {"total": None, "reported": 0, "missing": 1}


def test_live_synthetic_and_unproven_historical_modes_are_separate(usage_db):
    insert(usage_db, "live", data=output(), mode="live")
    insert(usage_db, "explicit-synthetic", data=output(), mode="synthetic")
    insert(usage_db, "legacy-unproven", data=output(), mode=None)
    insert(usage_db, "legacy-fake", data=output(synthetic=True), mode=None)
    insert(usage_db, "contradictory-fake", data=output(synthetic=True), mode="live")
    report = aggregate_usage(usage_db, now=NOW)
    assert model(report, mode="live")["invocations"] == 1
    assert model(report, mode="synthetic")["invocations"] == 3
    assert model(report, mode="unknown")["invocations"] == 1


@pytest.mark.parametrize("field,value", [("runtime", "fake-adapter-v1"), ("revision", "synthetic-fixture"), ("precision", "synthetic")])
def test_each_documented_legacy_synthetic_marker_is_recognized(usage_db, field, value):
    data = output(); data["model"][field] = value
    insert(usage_db, "legacy", data=data, mode=None)
    assert model(aggregate_usage(usage_db, now=NOW), mode="synthetic")["invocations"] == 1


def test_flux_images_have_no_token_equivalent_even_when_corrupt_token_fields_exist(usage_db):
    insert(usage_db, "image", stage="flux", data=output("flux", usage={"image_count": 1, "input_tokens": 99, "output_tokens": 9, "total_tokens": 108}))
    insert(usage_db, "failed-image", stage="flux", status="failed", data=output("flux", usage={"image_count": 0}, attempts=2))
    row = model(aggregate_usage(usage_db, now=NOW), stage="flux")
    assert row["tokens_applicable"] is False and row["images_applicable"] is True
    for key in ("input_tokens", "output_tokens", "total_tokens"):
        assert row[key] == {"total": None, "reported": 0, "missing": 0}
    assert row["image_count"] == {"total": 1, "reported": 2, "missing": 0}
    assert row["inference_attempts"]["total"] == 3
    assert row["inference_retries"] == {"total": 1, "reported": 2, "missing": 0}


def test_pending_unknown_and_operator_resolution_are_counted_without_estimated_usage(usage_db):
    for status in ("prepared", "submitting", "pending", "cancelling", "unknown", "unexpected-private-status", "operator_resolved"):
        insert(usage_db, status, status=status, data=output())
    row = model(aggregate_usage(usage_db, now=NOW))
    assert (row["invocations"], row["pending"], row["unknown"], row["failed"]) == (7, 4, 2, 1)
    assert row["input_tokens"] == {"total": 10, "reported": 1, "missing": 6}
    assert row["inference_attempts"] == {"total": 1, "reported": 1, "missing": 6}
    assert row["inference_retries"] == {"total": 0, "reported": 1, "missing": 6}


def test_today_is_utc_start_day_half_open_and_all_is_all_recorded(usage_db):
    for identifier, when in (("yesterday", START-0.001), ("midnight", START), ("end", START+86400-0.001), ("tomorrow", START+86400)):
        insert(usage_db, identifier, created=when, data=output())
    today = aggregate_usage(usage_db, now=NOW.astimezone(timezone(timedelta(hours=-11))))
    assert today["window_start"] == "2026-10-14T00:00:00Z"
    assert today["window_end"] == "2026-10-15T00:00:00Z"
    assert today["generated_at"] == "2026-10-14T12:00:00Z"
    assert model(today)["invocations"] == 2
    all_time = aggregate_usage(usage_db, window="all", now=NOW.timestamp())
    assert all_time["window_start"] is None and all_time["window_end"] is None
    assert model(all_time)["invocations"] == 4


def test_invocation_finishing_after_midnight_remains_in_its_start_day(usage_db):
    insert(usage_db, "crosses-midnight", created=START-1, status="pending")
    assert aggregate_usage(usage_db, now=NOW)["models"] == []
    with usage_db.connect() as connection:
        connection.execute("UPDATE invocations SET status='succeeded',output=?", (json.dumps(output()),))
    assert aggregate_usage(usage_db, now=NOW)["models"] == []
    assert model(aggregate_usage(usage_db, window="all", now=NOW))["input_tokens"]["total"] == 10


@pytest.mark.parametrize("bad", [None, "{broken", "null", "[]", '"private-prompt"', '{"model":[],"usage":1,"counters":false}'])
def test_corrupt_or_absent_old_outputs_are_unknown_measurements_not_crashes(usage_db, bad):
    insert(usage_db, "bad", data=bad, raw=True)
    row = model(aggregate_usage(usage_db, now=NOW), identity=None)
    for key in ("input_tokens", "output_tokens", "total_tokens", "inference_attempts", "inference_retries"):
        assert row[key] == {"total": None, "reported": 0, "missing": 1}


@pytest.mark.parametrize("retry_value", [None, True, "1", 1.5, -1, 2, 10**100])
def test_unreported_or_corrupt_retries_are_missing_without_changing_measured_tokens(usage_db, retry_value):
    data = output(attempts=2)
    data["counters"]["inference_retries"] = retry_value
    insert(usage_db, "retry-corrupt", data=data)
    row = model(aggregate_usage(usage_db, now=NOW))
    assert row["inference_retries"] == {"total": None, "reported": 0, "missing": 1}
    assert row["inference_attempts"]["total"] == 2
    assert row["total_tokens"]["total"] == 15


def test_missing_retry_field_is_not_derived_and_inconsistent_retries_are_unavailable(usage_db):
    missing = output(attempts=2); del missing["counters"]["inference_retries"]
    insert(usage_db, "legacy-no-retry", data=missing)
    inconsistent = output(attempts=1); inconsistent["counters"]["inference_retries"] = 1
    insert(usage_db, "bad-retry", data=inconsistent)
    row = model(aggregate_usage(usage_db, now=NOW))
    assert row["inference_retries"] == {"total": None, "reported": 0, "missing": 2}
    assert row["inference_attempts"]["total"] == 3
    assert row["total_tokens"]["total"] == 30


def test_invalid_metric_types_ranges_totals_and_private_model_strings_are_not_reported(usage_db):
    data = output(model_id="private-account-a private-prompt", usage={"input_tokens": True, "output_tokens": "5", "total_tokens": 5}, attempts=3)
    insert(usage_db, "invalid-fields", data=data)
    insert(usage_db, "bad-total", data=output(usage={"input_tokens": 2, "output_tokens": 3, "total_tokens": 999}))
    insert(usage_db, "negative", data=output(usage={"input_tokens": -1, "output_tokens": 1.5, "total_tokens": 2}, attempts=False))
    report = aggregate_usage(usage_db, now=NOW)
    unknown = model(report, identity=None)
    assert unknown["inference_attempts"]["total"] is None
    assert unknown["input_tokens"]["total"] is None
    assert model(report)["input_tokens"] == {"total": 2, "reported": 1, "missing": 1}
    assert model(report)["total_tokens"] == {"total": None, "reported": 0, "missing": 2}
    assert "private-account-a" not in json.dumps(report)


def test_zero_attempts_cannot_create_fabricated_nonzero_consumption(usage_db):
    insert(usage_db, "invalid-zero", data=output(usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}, attempts=0))
    row = model(aggregate_usage(usage_db, now=NOW))
    assert row["inference_attempts"]["total"] == 0
    assert all(row[key]["total"] is None for key in ("input_tokens", "output_tokens", "total_tokens"))


def test_large_corrupt_integer_is_unavailable_and_does_not_overflow_sql_sum(usage_db):
    data = output(usage={"input_tokens": 10**100, "output_tokens": 1, "total_tokens": 10**100+1})
    insert(usage_db, "huge", data=data)
    row = model(aggregate_usage(usage_db, now=NOW))
    assert row["input_tokens"]["total"] is None
    assert row["output_tokens"]["total"] == 1
    assert row["total_tokens"]["total"] is None


def test_bounded_cursor_covers_all_rows_once_without_joins_or_private_field_selection(usage_db):
    for number in range(520):
        insert(usage_db, str(number), owner="a" if number % 2 else "b", data=output())
    usage_db.sql.clear()
    report = aggregate_usage(usage_db, now=NOW)
    assert model(report)["invocations"] == 520
    assert model(report)["input_tokens"]["total"] == 5200
    queries = "\n".join(usage_db.sql).lower()
    assert "join " not in queries and "select *" not in queries
    assert "from feeds" not in queries and "from runs" not in queries and "from accounts" not in queries
    assert "provider_id" not in queries and "run_id" not in queries


def test_legacy_database_without_execution_mode_does_not_infer_live_from_current_config(tmp_path):
    db = UsageDatabase(tmp_path / "old.db", mode_column=False)
    insert(db, "old", data=output(), old=True)
    insert(db, "old-fake", data=output(synthetic=True), old=True)
    report = aggregate_usage(db, now=NOW)
    assert model(report, mode="unknown")["invocations"] == 1
    assert model(report, mode="synthetic")["invocations"] == 1


def test_empty_report_and_invalid_window_or_time(usage_db):
    assert aggregate_usage(usage_db, now=NOW)["models"] == []
    with pytest.raises(ValueError):
        aggregate_usage(usage_db, window="week", now=NOW)
    for bad in (True, float("nan"), float("inf"), 10**400, datetime(2026, 10, 14)):
        with pytest.raises(ValueError):
            aggregate_usage(usage_db, now=bad)


def assert_timeline_totals_match_models(report):
    """Merge final aggregate rows, without treating missing subtotals as zero."""
    from pulsefeed.usage import METRICS
    grouped={}
    counts=('invocations','succeeded','failed','pending','unknown')
    for bucket in report['timeline']['buckets']:
        for row in bucket['models']:
            key=(row['stage'],row['model_id'],row['execution_mode'])
            if key not in grouped:
                grouped[key]={**{name:row[name] for name in ('stage','model_id','execution_mode','tokens_applicable','images_applicable')},
                              **{name:0 for name in counts},
                              **{name:{'total':None,'reported':0,'missing':0} for name in METRICS}}
            merged=grouped[key]
            for name in counts:merged[name]+=row[name]
            for name in METRICS:
                for count in ('reported','missing'):merged[name][count]+=row[name][count]
                if row[name]['total'] is not None:
                    merged[name]['total']=(merged[name]['total'] or 0)+row[name]['total']
    key=lambda row:(row['stage'],row['model_id'] or '',row['execution_mode'])
    assert sorted(grouped.values(),key=key)==report['models']


def test_today_timeline_has_24_half_open_utc_hours_and_matches_totals(usage_db):
    for identifier,stamp in [('before',START-.001),('first-hour',START),('next-hour',START+3600),
                              ('last-hour',START+86400-.001),('after',START+86400)]:
        insert(usage_db,identifier,created=stamp,data=output())
    report=aggregate_usage(usage_db,now=NOW)
    timeline=report['timeline']
    assert timeline['bucket_seconds']==3600 and timeline['unbucketed_invocations']==0
    assert timeline['start']==report['window_start']=='2026-10-14T00:00:00Z'
    assert timeline['end']==report['window_end']=='2026-10-15T00:00:00Z'
    assert len(timeline['buckets'])==24
    assert [index for index,bucket in enumerate(timeline['buckets']) if bucket['models']]==[0,1,23]
    assert timeline['buckets'][0]['end']==timeline['buckets'][1]['start']=='2026-10-14T01:00:00Z'
    assert timeline['buckets'][12]['models']==[]  # Future/inactive is explicit, not missing metrics.
    assert_timeline_totals_match_models(report)


def test_empty_timeline_today_and_all_have_clear_bounds(usage_db):
    today=aggregate_usage(usage_db,now=NOW)
    all_time=aggregate_usage(usage_db,window='all',now=NOW)
    assert len(today['timeline']['buckets'])==24
    assert all_time['timeline']=={'bucket_seconds':86400,'start':'2026-10-14T00:00:00Z','end':'2026-10-15T00:00:00Z',
                                'unbucketed_invocations':0,'buckets':[{'start':'2026-10-14T00:00:00Z','end':'2026-10-15T00:00:00Z','models':[]}]}
    assert all(bucket['models']==[] for bucket in today['timeline']['buckets'])


def test_inactive_missing_and_actual_zero_buckets_are_distinct(usage_db):
    insert(usage_db,'zero',created=START+3600,data=output(usage={'input_tokens':0,'output_tokens':0,'total_tokens':0}))
    insert(usage_db,'unreported',created=START+7200,status='pending',data=None)
    report=aggregate_usage(usage_db,now=NOW)
    empty,zero,missing=report['timeline']['buckets'][:3]
    assert empty['models']==[]
    assert zero['models'][0]['input_tokens']=={'total':0,'reported':1,'missing':0}
    assert missing['models'][0]['input_tokens']=={'total':None,'reported':0,'missing':1}
    assert missing['models'][0]['pending']==1
    assert_timeline_totals_match_models(report)


def test_bucket_rows_preserve_stage_mode_model_and_retry_coverage(usage_db):
    insert(usage_db,'live',data=output(attempts=2))
    insert(usage_db,'synthetic',data=output(synthetic=True),mode='live')
    insert(usage_db,'historical',data=output(),mode=None)
    insert(usage_db,'image',stage='flux',data=output('flux',usage={'image_count':1}),created=START+3600)
    insert(usage_db,'partial',data=output(usage={'input_tokens':2,'output_tokens':None,'total_tokens':None}),created=START+3600)
    report=aggregate_usage(usage_db,now=NOW)
    assert len(report['timeline']['buckets'][0]['models'])==3
    assert model({'models':report['timeline']['buckets'][0]['models']})['inference_retries']=={'total':1,'reported':1,'missing':0}
    flux=model({'models':report['timeline']['buckets'][1]['models']},stage='flux')
    assert flux['input_tokens']=={'total':None,'reported':0,'missing':0} and not flux['tokens_applicable']
    assert_timeline_totals_match_models(report)


def test_all_time_widens_whole_day_buckets_without_truncating_old_history(usage_db):
    first=START-800*86400
    for index,day in enumerate((0,1,119,120,400,800)):
        insert(usage_db,'spanning-'+str(index),created=first+day*86400,data=output())
    report=aggregate_usage(usage_db,window='all',now=NOW)
    timeline=report['timeline']
    assert timeline['bucket_seconds']==7*86400 and len(timeline['buckets'])==115
    assert timeline['start']==datetime.fromtimestamp(first,timezone.utc).isoformat().replace('+00:00','Z')
    assert timeline['end']=='2026-10-15T00:00:00Z'
    assert all(bucket['end']==timeline['buckets'][index+1]['start'] for index,bucket in enumerate(timeline['buckets'][:-1]))
    assert sum(row['invocations'] for bucket in timeline['buckets'] for row in bucket['models'])==6
    assert_timeline_totals_match_models(report)
    encoded=json.dumps(report,allow_nan=False)
    assert all(secret not in encoded for secret in PRIVATE)


@pytest.mark.parametrize('bad',[None,'private-corrupt-timestamp',b'private-corrupt-timestamp',float('inf'),float('-inf'),1e300,-1e300])
def test_corrupt_created_remains_in_all_totals_but_is_explicitly_unbucketed(usage_db,bad):
    insert(usage_db,'valid-dated',data=output())
    insert(usage_db,'bad-dated',created=bad,data=output())
    all_time=aggregate_usage(usage_db,window='all',now=NOW)
    assert model(all_time)['invocations']==2 and model(all_time)['input_tokens']['total']==20
    assert all_time['timeline']['unbucketed_invocations']==1
    plotted=[row for bucket in all_time['timeline']['buckets'] for row in bucket['models']]
    assert len(plotted)==1 and plotted[0]['invocations']==1 and plotted[0]['input_tokens']['total']==10
    today=aggregate_usage(usage_db,now=NOW)
    assert model(today)['invocations']==1 and today['timeline']['unbucketed_invocations']==0
    assert 'private-corrupt-timestamp' not in json.dumps(all_time)
    assert_timeline_totals_match_models(today)


def test_only_undated_history_keeps_totals_and_uses_empty_current_day_timeline(usage_db):
    insert(usage_db,'undated',created=None,data=output())
    report=aggregate_usage(usage_db,window='all',now=NOW)
    assert model(report)['invocations']==1
    assert report['timeline']['unbucketed_invocations']==1
    assert len(report['timeline']['buckets'])==1
    assert report['timeline']['buckets'][0]['models']==[]


def test_extreme_supported_utc_dates_do_not_overflow_bucket_bounds(usage_db):
    from pulsefeed.usage import MIN_TIMESTAMP,MAX_TIMESTAMP
    insert(usage_db,'first-supported',created=MIN_TIMESTAMP,data=output())
    insert(usage_db,'last-supported',created=MAX_TIMESTAMP-1,data=output())
    report=aggregate_usage(usage_db,window='all',now=NOW)
    assert len(report['timeline']['buckets'])<=120
    assert report['timeline']['start']=='0001-01-01T00:00:00Z'
    assert report['timeline']['end']=='+010000-01-01T00:00:00Z'
    assert_timeline_totals_match_models(report)


def test_bounds_and_usage_share_one_read_snapshot_when_writer_commits_mid_report(usage_db,monkeypatch):
    # WAL allows a writer to commit after the bounds read while this report's
    # SQLite read transaction must continue seeing its original data snapshot.
    with usage_db.connect() as connection:connection.execute('PRAGMA journal_mode=WAL')
    insert(usage_db,'before-snapshot',data=output())
    connect=usage_db.connect
    inserted=False
    def tracked_connect():
        connection=connect()
        def trace(sql):
            nonlocal inserted
            usage_db.sql.append(sql)
            if 'WITH selected AS' in sql and not inserted:
                inserted=True
                insert(usage_db,'committed-later',created=START-1000*86400,data=output())
        connection.set_trace_callback(trace)
        return connection
    monkeypatch.setattr(usage_db,'connect',tracked_connect)
    first=aggregate_usage(usage_db,window='all',now=NOW)
    assert inserted and model(first)['invocations']==1
    assert first['timeline']['start']=='2026-10-14T00:00:00Z'
    assert_timeline_totals_match_models(first)
    second=aggregate_usage(usage_db,window='all',now=NOW)
    assert model(second)['invocations']==2
    assert second['timeline']['start']!='2026-10-14T00:00:00Z'
    assert_timeline_totals_match_models(second)


def test_wide_history_fraction_just_before_last_midnight_stays_in_final_bucket(usage_db):
    import math
    first=-50462697600.0
    last=math.nextafter(1792022400.0,-math.inf)
    assert last==1792022399.9999998
    insert(usage_db,'ancient-boundary',created=first,data=output())
    insert(usage_db,'fractional-final-boundary',created=last,data=output())
    report=aggregate_usage(usage_db,window='all',now=NOW)
    timeline=report['timeline']
    assert timeline['bucket_seconds']==5040*86400
    assert len(timeline['buckets'])==120
    assert timeline['end']=='2026-10-15T00:00:00Z'
    assert timeline['buckets'][0]['models'][0]['invocations']==1
    assert timeline['buckets'][-1]['models'][0]['invocations']==1
    assert_timeline_totals_match_models(report)
