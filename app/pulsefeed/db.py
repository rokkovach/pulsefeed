"""SQLite persistence for reader snapshots, durable jobs, and quota accounting.

Write transactions use BEGIN IMMEDIATE so checking capacity and reserving it are
one operation. Keep network/GPU work outside transactions; the app intentionally
uses a single coordinator process, and synchronous lock waits block its loop.
"""
import json
import sqlite3
from contextlib import contextmanager, closing
from pathlib import Path
import time
import uuid
from .prompt_defaults import DEFAULT_QWEN_INSTRUCTION, DEFAULT_FLUX_TEMPLATE, DEFAULT_CONTENT_FILTERS, DEFAULT_FALLBACK_PROMPT

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts(id TEXT PRIMARY KEY,name TEXT UNIQUE NOT NULL,role TEXT NOT NULL CHECK(role IN ('evaluator','admin')),created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS app_metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS access_tokens(hash TEXT PRIMARY KEY,account_id TEXT NOT NULL REFERENCES accounts(id),expires REAL NOT NULL,used REAL,revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS sessions(hash TEXT PRIMARY KEY,account_id TEXT NOT NULL REFERENCES accounts(id),csrf TEXT NOT NULL,expires REAL NOT NULL,revoked INTEGER NOT NULL DEFAULT 0,admin_capability TEXT);
CREATE TABLE IF NOT EXISTS settings(id INTEGER PRIMARY KEY CHECK(id=1),value TEXT NOT NULL,updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS feeds(id TEXT PRIMARY KEY,owner TEXT NOT NULL REFERENCES accounts(id),url TEXT NOT NULL,title TEXT NOT NULL,created REAL NOT NULL,canonical_id TEXT,UNIQUE(owner,url));
CREATE TABLE IF NOT EXISTS items(id TEXT PRIMARY KEY,feed_id TEXT NOT NULL REFERENCES feeds(id),guid TEXT NOT NULL,title TEXT NOT NULL,excerpt TEXT NOT NULL,link TEXT NOT NULL,published TEXT,article TEXT NOT NULL,UNIQUE(feed_id,guid));
CREATE TABLE IF NOT EXISTS feed_current_state(feed_id TEXT PRIMARY KEY REFERENCES feeds(id),retrieved REAL NOT NULL);
CREATE TABLE IF NOT EXISTS feed_current_items(feed_id TEXT NOT NULL REFERENCES feeds(id),item_id TEXT NOT NULL REFERENCES items(id),ordinal INTEGER NOT NULL,PRIMARY KEY(feed_id,item_id),UNIQUE(feed_id,ordinal));
CREATE TABLE IF NOT EXISTS feed_views(owner TEXT NOT NULL REFERENCES accounts(id),idempotency_key TEXT NOT NULL,url TEXT NOT NULL,feed_id TEXT NOT NULL REFERENCES feeds(id),feed_snapshot TEXT NOT NULL,items_snapshot TEXT NOT NULL,batch_id TEXT REFERENCES batches(id),generation_status TEXT NOT NULL,error TEXT,retry_after INTEGER,created REAL NOT NULL,id TEXT,expires_at REAL,PRIMARY KEY(owner,idempotency_key));
CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY,owner TEXT NOT NULL REFERENCES accounts(id),idempotency_key TEXT NOT NULL,state TEXT NOT NULL,created REAL NOT NULL,UNIQUE(owner,idempotency_key));
CREATE UNIQUE INDEX IF NOT EXISTS one_active_batch ON batches(state) WHERE state='active';
CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY,batch_id TEXT NOT NULL REFERENCES batches(id),owner TEXT NOT NULL REFERENCES accounts(id),item_id TEXT NOT NULL REFERENCES items(id),ordinal INTEGER NOT NULL,state TEXT NOT NULL,parent_run_id TEXT REFERENCES runs(id),settings TEXT NOT NULL,original TEXT NOT NULL,qwen_prompt TEXT,final_prompt TEXT,matched_terms TEXT NOT NULL DEFAULT '[]',used_fallback INTEGER NOT NULL DEFAULT 0,seed INTEGER NOT NULL,width INTEGER NOT NULL DEFAULT 1024,height INTEGER NOT NULL DEFAULT 1024,deadline REAL,error TEXT,created REAL NOT NULL,completed_at REAL,result_expires_at REAL,expiry_estimated INTEGER NOT NULL DEFAULT 1,reserved INTEGER NOT NULL DEFAULT 2,reserved_at REAL NOT NULL,quota_finalized_at REAL,quota_epoch INTEGER NOT NULL DEFAULT 0,admission_epoch INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS invocations(id TEXT PRIMARY KEY,run_id TEXT NOT NULL REFERENCES runs(id),stage TEXT NOT NULL,provider_id TEXT,status TEXT NOT NULL,input TEXT NOT NULL,output TEXT,delay_ms REAL,execution_ms REAL,created REAL NOT NULL,execution_mode TEXT);
CREATE TABLE IF NOT EXISTS quota_policy(id INTEGER PRIMARY KEY CHECK(id=1),global_daily_limit INTEGER NOT NULL CHECK(global_daily_limit BETWEEN 1 AND 10000),feed_article_limit INTEGER NOT NULL CHECK(feed_article_limit BETWEEN 1 AND 10),reset_cutoff REAL NOT NULL DEFAULT 0,updated REAL NOT NULL,reset_epoch INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS quota_audit(id INTEGER PRIMARY KEY,actor TEXT NOT NULL REFERENCES accounts(id),event TEXT NOT NULL,data TEXT NOT NULL,created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS rate_events(id INTEGER PRIMARY KEY,scope TEXT NOT NULL,action TEXT NOT NULL,created REAL NOT NULL);
CREATE INDEX IF NOT EXISTS rate_lookup ON rate_events(scope,action,created);
-- Cleanup has no scope/action predicate and needs its own leading key.
CREATE INDEX IF NOT EXISTS rate_expiry ON rate_events(created);
CREATE INDEX IF NOT EXISTS run_batch ON runs(batch_id,ordinal);
CREATE INDEX IF NOT EXISTS run_item_history ON runs(owner,item_id,created DESC);
CREATE INDEX IF NOT EXISTS invocation_run ON invocations(run_id,stage);
CREATE INDEX IF NOT EXISTS run_start ON runs(created);

"""
DEFAULT_QWEN_ARTICLE_TEMPLATE = 'Article data:\n{"title": {{article_title}}, "excerpt": {{article_excerpt}}}'

DEFAULT_SETTINGS = {
    "qwen_template": DEFAULT_QWEN_INSTRUCTION,
    "qwen_article_template": DEFAULT_QWEN_ARTICLE_TEMPLATE,
    "flux_prefix": DEFAULT_FLUX_TEMPLATE,
    "fallback_prompt": DEFAULT_FALLBACK_PROMPT,
    "blocked_terms": DEFAULT_CONTENT_FILTERS,
    "flux_template_version": 1,
}


def new_id():
    return str(uuid.uuid4())


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class Database:
    def __init__(self, path, global_daily_limit=100, feed_article_limit=10, global_hourly_generation_limit=None):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as conn:
            conn.executescript(SCHEMA)
            conn.execute("INSERT OR IGNORE INTO settings VALUES(1,?,?)", (dump(DEFAULT_SETTINGS), time.time()))
            # Upgrade only current settings; historical snapshots retain their original text.
            conn.execute("BEGIN IMMEDIATE")
            try:
                for table,column,declaration in (("feeds","canonical_id","TEXT"),("runs","quota_finalized_at","REAL"),("invocations","execution_mode","TEXT"),("runs","quota_epoch","INTEGER NOT NULL DEFAULT 0"),("quota_policy","reset_epoch","INTEGER NOT NULL DEFAULT 0"),("quota_policy","global_hourly_generation_limit","INTEGER"),("feed_views","id","TEXT"),("feed_views","expires_at","REAL"),("sessions","admin_capability","TEXT")):
                    if column not in {row[1] for row in conn.execute("PRAGMA table_info("+table+")")}:
                        conn.execute("ALTER TABLE "+table+" ADD COLUMN "+column+" "+declaration)
                if "feed_article_limit" not in {row[1] for row in conn.execute("PRAGMA table_info(quota_policy)")}:
                    # Retire the old daily canonical-feed budget and its coupled
                    # global constraint without changing accounting or history.
                    conn.execute("ALTER TABLE quota_policy RENAME TO quota_policy_legacy")
                    conn.execute("CREATE TABLE quota_policy(id INTEGER PRIMARY KEY CHECK(id=1),global_daily_limit INTEGER NOT NULL CHECK(global_daily_limit BETWEEN 1 AND 10000),feed_article_limit INTEGER NOT NULL CHECK(feed_article_limit BETWEEN 1 AND 10),reset_cutoff REAL NOT NULL DEFAULT 0,updated REAL NOT NULL,reset_epoch INTEGER NOT NULL DEFAULT 0,global_hourly_generation_limit INTEGER)")
                    conn.execute("INSERT INTO quota_policy SELECT id,global_daily_limit,MIN(10,MAX(1,feed_daily_limit)),reset_cutoff,updated,reset_epoch,global_hourly_generation_limit FROM quota_policy_legacy")
                    conn.execute("DROP TABLE quota_policy_legacy")
                from .rss import canonical_feed_id
                for feed in conn.execute("SELECT id,url FROM feeds WHERE canonical_id IS NULL").fetchall():
                    conn.execute("UPDATE feeds SET canonical_id=? WHERE id=?",(canonical_feed_id(feed["url"]),feed["id"]))
                conn.execute("UPDATE runs SET quota_finalized_at=COALESCE(completed_at,reserved_at) WHERE state IN ('completed','failed') AND quota_finalized_at IS NULL")
                if not conn.execute("SELECT 1 FROM quota_policy WHERE id=1").fetchone():
                    if type(global_daily_limit) is not int or type(feed_article_limit) is not int or not 1<=global_daily_limit<=10000 or not 1<=feed_article_limit<=10:
                        raise ValueError("Quota seed must be global1..10000 and page articles1..10")
                    conn.execute("INSERT INTO quota_policy(id,global_daily_limit,feed_article_limit,reset_cutoff,updated) VALUES(1,?,?,0,?)",(global_daily_limit,feed_article_limit,time.time()))
                if global_hourly_generation_limit is not None:
                    if type(global_hourly_generation_limit) is not int or not 1<=global_hourly_generation_limit<=10000:
                        raise ValueError("Hourly generation seed must be an integer from 1 to 10000")
                    conn.execute("UPDATE quota_policy SET global_hourly_generation_limit=? WHERE id=1 AND global_hourly_generation_limit IS NULL",(global_hourly_generation_limit,))
                if "admission_epoch" not in {row[1] for row in conn.execute("PRAGMA table_info(runs)")}:
                    # Admission identity never changes at settlement. Stamp legacy
                    # starts once so an upgrade alone cannot reset their allowance.
                    conn.execute("ALTER TABLE runs ADD COLUMN admission_epoch INTEGER NOT NULL DEFAULT 0")
                    conn.execute("UPDATE runs SET admission_epoch=(SELECT reset_epoch FROM quota_policy WHERE id=1)")
                # Existing requests are tombstones, never renewed into a new hour.
                for view in conn.execute("SELECT owner,idempotency_key FROM feed_views WHERE id IS NULL OR expires_at IS NULL").fetchall():
                    conn.execute("UPDATE feed_views SET id=COALESCE(id,?),expires_at=COALESCE(expires_at,0) WHERE owner=? AND idempotency_key=?",(new_id(),view["owner"],view["idempotency_key"]))
                conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS view_identity ON feed_views(id)")
                conn.execute("CREATE INDEX IF NOT EXISTS view_owner_expiry ON feed_views(owner,expires_at)")
                conn.execute("CREATE INDEX IF NOT EXISTS view_pending_compaction ON feed_views(expires_at) WHERE feed_snapshot!='{}' OR items_snapshot!='[]'")
                conn.execute("CREATE TABLE IF NOT EXISTS view_runs(view_id TEXT NOT NULL REFERENCES feed_views(id),run_id TEXT NOT NULL REFERENCES runs(id),PRIMARY KEY(view_id,run_id))")
                conn.execute("CREATE INDEX IF NOT EXISTS quota_finalization ON runs(quota_finalized_at)")
                conn.execute("PRAGMA user_version=7")
                settings = json.loads(conn.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])
                settings_changed = False
                if "flux_template_version" not in settings:
                    if "{{qwen_prompt}}" not in settings["flux_prefix"]:
                        settings["flux_prefix"] = (settings["flux_prefix"].strip() + " {{qwen_prompt}}").strip()
                        settings["flux_template_version"] = 1
                    else:
                        # A formerly ordinary literal must not silently become an insertion point.
                        settings["flux_template_version"] = 0
                    settings_changed = True
                if "qwen_article_template" not in settings:
                    # The separate user-message layout never rewrites old system
                    # instructions or historical run/invocation snapshots.
                    settings["qwen_article_template"] = DEFAULT_QWEN_ARTICLE_TEMPLATE
                    settings_changed = True
                if settings_changed:
                    conn.execute("UPDATE settings SET value=?,updated=? WHERE id=1", (dump(settings), time.time()))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
        except BaseException:
            conn.close()
            raise
        return conn

    @contextmanager
    def transaction(self):
        conn = self.connect()
        try:
            # Admission reads capacity and writes reservations under the same
            # SQLite writer lock. A deferred transaction permits two requests to
            # observe the same free quota before either reserves it.
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def one(self, sql, values=()):
        with closing(self.connect()) as conn:
            row = conn.execute(sql, values).fetchone()
            return dict(row) if row else None

    def all(self, sql, values=()):
        with closing(self.connect()) as conn:
            return [dict(row) for row in conn.execute(sql, values).fetchall()]

    def execute(self, sql, values=()):
        with self.transaction() as conn:
            conn.execute(sql, values)

    def settings(self):
        return json.loads(self.one("SELECT value FROM settings WHERE id=1")["value"])

    def compact_expired_views(self, now=None, limit=100):
        """Release expired reader payloads without deleting replay or usage history."""
        now = time.time() if now is None else now
        with self.transaction() as conn:
            # Keep the request key, URL, immutable identity/expiry and admission
            # result as tombstones. Expired replays must remain 404 forever rather
            # than becoming new admissions. Runs, prompts and invocations are
            # durable model history and are deliberately outside this update.
            result = conn.execute("""UPDATE feed_views SET feed_snapshot='{}',items_snapshot='[]'
                WHERE rowid IN (SELECT rowid FROM feed_views WHERE expires_at<=?
                AND (feed_snapshot!='{}' OR items_snapshot!='[]') ORDER BY expires_at LIMIT ?)""",(now,limit))
            return result.rowcount

    def bind_provider(self, profile, synthetic=False):
        # A persisted provider job ID is meaningful only at its original endpoint.
        # Refuse accidental routing changes; rotating credentials is safe because
        # provider_profile excludes credentials while retaining endpoint identity.
        with self.transaction() as conn:
            existing = conn.execute("SELECT value FROM app_metadata WHERE key='provider_profile'").fetchone()
            if existing:
                if existing["value"] != profile:
                    raise ValueError("Database inference provider/endpoints differ. Use the original configuration or a separate DATABASE_PATH.")
            elif conn.execute("SELECT 1 FROM invocations LIMIT 1").fetchone() and not (synthetic and not conn.execute("SELECT 1 FROM invocations WHERE execution_mode IS NULL OR execution_mode!='synthetic' LIMIT 1").fetchone()):
                raise ValueError("Existing jobs have no verified provider identity. Keep this database offline for reconciliation and use a separate DATABASE_PATH.")
            else:
                conn.execute("INSERT INTO app_metadata VALUES('provider_profile',?)",(profile,))
