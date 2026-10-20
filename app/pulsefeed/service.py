"""Product operations that turn a validated feed into an affordable reader page.

A view snapshots article order and settings once. Admission reserves worst-case
image attempts in the same transaction as the jobs; refreshes and exact request
replays only read that work. The coordinator is responsible for provider calls.
"""
import json
from contextlib import closing
import hashlib
import secrets
import time
import unicodedata
from .auth import rate_limit
from .db import dump, new_id, DEFAULT_QWEN_ARTICLE_TEMPLATE
from .errors import AppError
from .rss import canonical_feed_id, source_hostname, safe_link


def normalized_match(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def blocked_terms(settings):
    return [normalized_match(term) for term in settings["blocked_terms"].splitlines() if normalized_match(term)]


QWEN_PROMPT_TOKEN = "{{qwen_prompt}}"


def validate_settings(settings, from_admin=False, current_settings=None):
    fields = {"qwen_template", "flux_prefix", "fallback_prompt", "blocked_terms"}
    allowed = fields | {"qwen_article_template"}
    if not from_admin:
        allowed |= {"flux_template_version"}
    if not isinstance(settings,dict) or not fields <= set(settings) <= allowed or any(not isinstance(settings[key],str) for key in fields):
        raise AppError("INVALID_SETTINGS", "Provide the supported settings text fields.")
    settings = dict(settings)
    if from_admin and "qwen_article_template" not in settings:
        # Existing four-field callers keep an operator's custom article layout;
        # callers without current-settings context use the legacy-equivalent default.
        settings["qwen_article_template"] = (current_settings.get("qwen_article_template", DEFAULT_QWEN_ARTICLE_TEMPLATE)
                                              if isinstance(current_settings,dict) else DEFAULT_QWEN_ARTICLE_TEMPLATE)
    if "qwen_article_template" in settings:
        article_template = settings["qwen_article_template"]
        markers = ("{{article_title}}", "{{article_excerpt}}")
        if not isinstance(article_template,str) or any(article_template.count(marker) != 1 for marker in markers):
            raise AppError("INVALID_SETTINGS", "The Qwen article template must contain exactly one {{article_title}} and one {{article_excerpt}} token.")
        static_text = article_template
        for marker in markers:
            static_text = static_text.replace(marker, "")
        if len(static_text)>2000 or any(unicodedata.category(char) in {"Cs","Cc"} and char not in "\n\r\t" for char in article_template):
            raise AppError("INVALID_SETTINGS", "The Qwen article template permits 2,000 static characters and no unsupported control characters.")
    version = 1 if from_admin else settings.get("flux_template_version",0)
    if type(version) is not int or version not in (0,1):
        raise AppError("INVALID_SETTINGS", "Unsupported FLUX prompt template version.")
    static_text = settings["flux_prefix"].replace(QWEN_PROMPT_TOKEN, "") if version == 1 else settings["flux_prefix"]
    if not settings["qwen_template"].strip() or len(settings["qwen_template"]) > 4000 or len(static_text) > 2000 or not settings["fallback_prompt"].strip() or len(settings["fallback_prompt"]) > 4000 or len(settings["blocked_terms"]) > 4000:
        raise AppError("INVALID_SETTINGS", "Template/fallback must be nonempty and bounded; FLUX static text limit is 2,000 characters.")
    if version == 1 and settings["flux_prefix"].count(QWEN_PROMPT_TOKEN) != 1:
        raise AppError("INVALID_SETTINGS", "The FLUX prompt template must contain exactly one {{qwen_prompt}} token.")
    if any(term in normalized_match(settings["fallback_prompt"]) for term in blocked_terms(settings)):
        raise AppError("INVALID_SETTINGS", "The complete fallback prompt contains a blocked term.")
    return dict(settings, flux_template_version=version)


def compose_prompt(prompt, settings):
    settings = validate_settings(settings)
    template = settings["flux_prefix"]
    final = template.replace(QWEN_PROMPT_TOKEN, prompt.strip()).strip() if settings["flux_template_version"] == 1 else " ".join(x.strip() for x in (template, prompt) if x.strip())
    if not final or len(final) > 4000:
        raise AppError("INVALID_MODEL_OUTPUT", "The composed image prompt exceeds its limit.", 502)
    matched = [term for term in blocked_terms(settings) if term in normalized_match(final)]
    return (settings["fallback_prompt"].strip() if matched else final), matched, bool(matched)


# Reset removes terminal starts from older generations while preserving recent
# unresolved starts. Every hourly read/admission/Retry-After uses this same set.
HOURLY_STARTS = "created>? AND (admission_epoch=? OR state NOT IN ('completed','failed'))"


def bounded_feed(fetched, url):
    """Apply storage bounds even when an injected fetcher bypasses the RSS parser."""
    if not isinstance(url,str) or len(url)>2048 or any(unicodedata.category(char)=="Cs" for char in url):
        raise AppError("INVALID_FEED", "Enter a bounded feed URL.")
    def text(value, limit):
        if not isinstance(value,str):
            raise AppError("INVALID_FEED", "The feed contains invalid article text.")
        # Slice before normalization: custom fetchers must not turn a bounded
        # display field into unbounded normalization or JSON/storage work.
        value = unicodedata.normalize("NFC",value[:limit])
        if any(unicodedata.category(char)=="Cs" for char in value):
            raise AppError("INVALID_FEED", "The feed contains invalid article text.")
        return " ".join(value.split())[:limit]

    if not isinstance(fetched,dict) or not isinstance(fetched.get("items"),list):
        raise AppError("INVALID_FEED", "The feed contains invalid articles.")
    result = {"title":text(fetched.get("title",""),200),"items":[]}
    for item in fetched["items"][:10]:
        if not isinstance(item,dict) or not isinstance(item.get("article"),dict):
            raise AppError("INVALID_FEED", "The feed contains invalid articles.")
        guid = item.get("guid")
        if not isinstance(guid,str) or not guid or len(guid)>2048:
            raise AppError("INVALID_FEED", "The feed contains an invalid article identity.")
        title = text(item.get("title",""),500)
        excerpt = text(item.get("excerpt",""),4000)
        article_title = text(item["article"].get("title",""),4000)
        article_excerpt = text(item["article"].get("excerpt",""),4000-len(article_title))
        # Discard unknown fields rather than persisting arbitrary fetcher data.
        result["items"].append({"guid":text(guid,2048),"title":title,"excerpt":excerpt,
                                "link":safe_link(text(item.get("link",""),2048),url),"published":text(item.get("published") or "",200),
                                "article":{"title":article_title,"excerpt":article_excerpt}})
    return result


class Service:
    def __init__(self, db, config):
        self.db, self.config = db, config

    def limit(self, owner, action):
        limits = {"rss":self.config.rss_limit, "submit":self.config.submit_limit, "poll":self.config.poll_limit}
        with self.db.transaction() as conn:
            rate_limit(conn, owner, action, limits[action])

    @staticmethod
    def validate_key(key):
        if not isinstance(key,str) or not 8 <= len(key) <= 100 or any(ord(char)<32 or unicodedata.category(char)=="Cs" for char in key):
            raise AppError("INVALID_INPUT", "Supply a bounded idempotency key.")

    def require_view_capacity(self, owner, conn=None, now=None):
        """A cheap prefetch check; admission repeats it under the write lock."""
        now = time.time() if now is None else now
        sql = "SELECT COUNT(*) AS total,MIN(expires_at) AS first FROM feed_views WHERE owner=? AND expires_at>?"
        row = conn.execute(sql,(owner,now)).fetchone() if conn else self.db.one(sql,(owner,now))
        if row["total"] >= self.config.live_view_limit:
            raise AppError("VIEW_LIMIT_REACHED", "Too many feed pages are open. Try again after an existing page expires.",429,max(1,int(row["first"]-now)+1))

    def admin_limit(self, owner):
        with self.db.transaction() as conn:
            rate_limit(conn,owner,"admin_write",self.config.admin_write_limit)

    def save_settings(self, body, section=None):
        groups = {"qwen":{"qwen_template","qwen_article_template"}, "flux":{"flux_prefix"}, "content":{"fallback_prompt","blocked_terms"}}
        if section is not None and (section not in groups or not isinstance(body,dict) or set(body)!=groups[section]):
            raise AppError("INVALID_SETTINGS","Provide exactly the settings fields for this section.")
        with self.db.transaction() as conn:
            current = json.loads(conn.execute("SELECT value FROM settings WHERE id=1").fetchone()[0])
            if section is None:
                value = validate_settings(body,from_admin=True,current_settings=current)
            else:
                merged = dict(current,**body)
                if section == "flux":
                    merged["flux_template_version"] = 1
                value = validate_settings(merged)
            conn.execute("UPDATE settings SET value=?,updated=? WHERE id=1",(dump(value),time.time()))
        return value

    @staticmethod
    def _quota_charges(conn, now, policy):
        day_start = int(now)//86400*86400
        window_start = max(day_start,policy["reset_cutoff"])
        # Active/uncertain work survives both UTC rollover and an explicit reset.
        # Only terminal charges belong to a particular reset epoch; settlement
        # stamps the current epoch so a late result cannot evade today's budget.
        rows = conn.execute("""SELECT r.reserved,r.state,
            (SELECT CASE WHEN json_valid(v.output) THEN CASE WHEN json_type(v.output,'$.counters.inference_attempts')='integer' THEN json_extract(v.output,'$.counters.inference_attempts') END END
             FROM invocations v WHERE v.run_id=r.id AND v.stage='flux' ORDER BY v.created DESC,v.rowid DESC LIMIT 1) AS actual
            FROM runs r
            WHERE r.state NOT IN ('completed','failed') OR (r.quota_epoch=? AND (r.reserved_at>=? OR r.quota_finalized_at>=?))""",(policy["reset_epoch"],day_start,day_start)).fetchall()
        totals = {"charged":0,"reserved":0,"used":0,"uncertain_charged":0}
        for row in rows:
            amount = row["reserved"]
            totals["charged"] += amount
            if row["state"] not in ("completed","failed"):
                totals["reserved"] += amount
            elif amount == 0 or (type(row["actual"]) is int and 0<=row["actual"]<=2 and row["actual"]==amount):
                totals["used"] += amount
            else:
                totals["uncertain_charged"] += amount
        return totals,window_start,day_start+86400

    def quota_summary(self, now=None):
        now = time.time() if now is None else now
        with closing(self.db.connect()) as conn:
            conn.execute("BEGIN")
            policy = conn.execute("SELECT * FROM quota_policy WHERE id=1").fetchone()
            totals,window_start,reset_at = self._quota_charges(conn,now,policy)
            hourly = self._hourly_summary(conn,now,policy)
        return {"window":"utc_day","generated_at":now,
                "global":dict(totals,limit=policy["global_daily_limit"],remaining=max(0,policy["global_daily_limit"]-totals["charged"]),window_start=window_start,reset_at=reset_at),
                "page":{"article_limit":policy["feed_article_limit"],"max_articles":10},"hourly":hourly}

    @staticmethod
    def _hourly_summary(conn, now, policy):
        row = conn.execute("SELECT COUNT(*) AS used,MIN(created) AS first FROM runs WHERE "+HOURLY_STARTS,(now-3600,policy["reset_epoch"])).fetchone()
        limit = policy["global_hourly_generation_limit"] or 30
        return {"limit":limit,"used":row["used"],"remaining":max(0,limit-row["used"]),"window_seconds":3600,"next_slot_at":row["first"]+3600 if row["first"] is not None else None}

    def _admission_budget(self, conn, owner, now):
        """Read all admission allowances in the caller's write transaction."""
        policy = conn.execute("SELECT * FROM quota_policy WHERE id=1").fetchone()
        charged = conn.execute("SELECT COALESCE(SUM(reserved),0) FROM runs WHERE owner=? AND (state NOT IN ('completed','failed') OR (quota_epoch=? AND (reserved_at>? OR quota_finalized_at>?)))", (owner,policy["reset_epoch"],now-3600,now-3600)).fetchone()[0]
        hourly = self._hourly_summary(conn,now,policy)
        totals,_,reset_at = self._quota_charges(conn,now,policy)
        return {"now":now,"owner_slots":max(0,(self.config.image_quota-charged)//2),
                "hourly":hourly,"global_slots":max(0,(policy["global_daily_limit"]-totals["charged"])//2),
                "page_limit":policy["feed_article_limit"],"reset_at":reset_at}

    def set_limits(self, actor, body):
        if set(body)!={"global_daily_limit","feed_article_limit"} or type(body.get("global_daily_limit")) is not int or type(body.get("feed_article_limit")) is not int:
            raise AppError("INVALID_LIMITS","Provide integer global daily and per-page article limits.")
        global_limit,feed_limit = body["global_daily_limit"],body["feed_article_limit"]
        if not 1<=global_limit<=10000 or not 1<=feed_limit<=10:
            raise AppError("INVALID_LIMITS","Global daily limit must be 1–10,000; articles per page must be 1–10.")
        with self.db.transaction() as conn:
            now = time.time()
            previous = conn.execute("SELECT global_daily_limit,feed_article_limit FROM quota_policy WHERE id=1").fetchone()
            conn.execute("UPDATE quota_policy SET global_daily_limit=?,feed_article_limit=?,updated=? WHERE id=1",(global_limit,feed_limit,now))
            conn.execute("INSERT INTO quota_audit(actor,event,data,created) VALUES(?,?,?,?)",(actor,"limits_updated",dump({"previous":dict(previous),"current":body}),now))
        return self.quota_summary()

    def reset_limits(self, actor, body):
        if set(body)!={"confirm"} or body["confirm"] is not True:
            raise AppError("CONFIRM_REQUIRED","Confirm the allowance reset explicitly.")
        with self.db.transaction() as conn:
            now = time.time()
            conn.execute("UPDATE quota_policy SET reset_cutoff=?,updated=?,reset_epoch=reset_epoch+1 WHERE id=1",(now,now))
            conn.execute("INSERT INTO quota_audit(actor,event,data,created) VALUES(?,?,?,?)",(actor,"allowance_reset",dump({"cutoff":now,"scope":"daily_and_hourly"}),now))
        return self.quota_summary(now)

    def _save_feed(self, conn, owner, url, fetched):
        feed = conn.execute("SELECT * FROM feeds WHERE owner=? AND url=?", (owner,url)).fetchone()
        if not feed:
            feed_id = new_id()
            conn.execute("INSERT INTO feeds(id,owner,url,title,created,canonical_id) VALUES(?,?,?,?,?,?)", (feed_id,owner,url,fetched["title"],time.time(),canonical_feed_id(url)))
        else:
            feed_id = feed["id"]
            conn.execute("UPDATE feeds SET title=? WHERE id=?", (fetched["title"],feed_id))
        conn.execute("INSERT INTO feed_current_state VALUES(?,?) ON CONFLICT(feed_id) DO UPDATE SET retrieved=excluded.retrieved", (feed_id,time.time()))
        conn.execute("DELETE FROM feed_current_items WHERE feed_id=?", (feed_id,))
        seen = set()
        for item in fetched["items"][:10]:
            conn.execute("INSERT INTO items(id,feed_id,guid,title,excerpt,link,published,article) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(feed_id,guid) DO UPDATE SET title=excluded.title,excerpt=excluded.excerpt,link=excluded.link,published=excluded.published,article=excluded.article", (new_id(),feed_id,item["guid"],item["title"],item["excerpt"],item["link"],item["published"],dump(item["article"])))
            item_id = conn.execute("SELECT id FROM items WHERE feed_id=? AND guid=?", (feed_id,item["guid"])).fetchone()[0]
            if item_id not in seen:
                conn.execute("INSERT INTO feed_current_items VALUES(?,?,?)", (feed_id,item_id,len(seen)))
                seen.add(item_id)
        return feed_id

    def save_feed(self, owner, url, fetched):
        fetched = bounded_feed(fetched,url)
        with self.db.transaction() as conn:
            feed_id = self._save_feed(conn,owner,url,fetched)
        return self.feed(owner,feed_id)

    def _feed_data(self, conn, owner, feed_id):
        feed = conn.execute("SELECT id,url,title FROM feeds WHERE id=? AND owner=?", (feed_id,owner)).fetchone()
        if not feed:
            raise AppError("FORBIDDEN", "You cannot access this feed.", 403)
        items = conn.execute("SELECT i.id,i.title,i.excerpt,i.link,i.published FROM feed_current_items c JOIN items i ON i.id=c.item_id WHERE c.feed_id=? ORDER BY c.ordinal LIMIT 10", (feed_id,)).fetchall()
        if not items and not conn.execute("SELECT 1 FROM feed_current_state WHERE feed_id=?", (feed_id,)).fetchone():
            # Existing pre-viewer records have no membership map until their next refetch.
            items = conn.execute("SELECT id,title,excerpt,link,published FROM items WHERE feed_id=? ORDER BY rowid LIMIT 10", (feed_id,)).fetchall()
        return dict(feed), [dict(item,display_source=source_hostname(item["link"])) for item in items]

    def latest_item_runs(self, owner, feed_id, items, conn=None):
        sql = "SELECT r.* FROM runs r JOIN items i ON i.id=r.item_id WHERE r.owner=? AND i.feed_id=? AND NOT EXISTS (SELECT 1 FROM runs n WHERE n.owner=r.owner AND n.item_id=r.item_id AND (n.created>r.created OR (n.created=r.created AND n.rowid>r.rowid)))"
        rows = conn.execute(sql,(owner,feed_id)).fetchall() if conn else self.db.all(sql,(owner,feed_id))
        latest = {row["item_id"]:row for row in rows}
        return [self.serialize(latest[item["id"]],conn) for item in items if item["id"] in latest]

    def feed(self, owner, feed_id):
        with closing(self.db.connect()) as conn:
            conn.execute("BEGIN")
            return self._feed_snapshot(conn,owner,feed_id)

    def _feed_snapshot(self, conn, owner, feed_id):
        feed,items = self._feed_data(conn,owner,feed_id)
        active = conn.execute("SELECT id FROM batches WHERE owner=? AND state='active'",(owner,)).fetchone()
        return {"feed":feed,"items":items,"runs":self.latest_item_runs(owner,feed_id,items,conn),"active_batch_id":active["id"] if active else None}

    def cached_view(self, owner, key, url):
        row = self.db.one("SELECT * FROM feed_views WHERE owner=? AND idempotency_key=?", (owner,key))
        if not row:
            return None
        self.require_view(owner,row["id"])
        if row["url"] != url:
            raise AppError("IDEMPOTENCY_CONFLICT", "This request key was already used for a different feed URL.",409)
        return self.view_result(owner,row,reused=True)

    def view_result(self, owner, row, reused=False):
        with closing(self.db.connect()) as conn:
            conn.execute("BEGIN")
            return self._view_result(conn,owner,self._live_view(conn,owner,row["id"]),reused)

    @staticmethod
    def unavailable_view():
        raise AppError("VIEW_NOT_FOUND","This feed page is unavailable or has expired.",404)

    def _live_view(self, conn, owner, view_id):
        if not isinstance(view_id,str) or not 1<=len(view_id)<=100:
            self.unavailable_view()
        row = conn.execute("SELECT * FROM feed_views WHERE id=? AND owner=? AND expires_at>?",(view_id,owner,time.time())).fetchone()
        if not row:
            self.unavailable_view()
        return row

    def require_view(self, owner, view_id, run_id=None, batch_id=None):
        with closing(self.db.connect()) as conn:
            conn.execute("BEGIN")
            row = self._live_view(conn,owner,view_id)
            self._view_membership(conn,owner,view_id,run_id,batch_id)
            return {"id":row["id"],"url":"/feeds/"+row["id"],"created_at":row["created"],"expires_at":row["expires_at"]}

    def _view_membership(self, conn, owner, view_id, run_id=None, batch_id=None):
        if run_id is not None and not conn.execute("SELECT 1 FROM view_runs v JOIN runs r ON r.id=v.run_id WHERE v.view_id=? AND r.id=? AND r.owner=?",(view_id,run_id,owner)).fetchone():
            self.unavailable_view()
        if batch_id is not None:
            batch = conn.execute("SELECT 1 FROM batches WHERE id=? AND owner=?",(batch_id,owner)).fetchone()
            counts = conn.execute("SELECT COUNT(*) AS total,SUM(EXISTS(SELECT 1 FROM view_runs v WHERE v.view_id=? AND v.run_id=r.id)) AS included FROM runs r WHERE r.batch_id=?",(view_id,batch_id)).fetchone()
            if not batch or not counts["total"] or counts["total"]!=counts["included"]:
                self.unavailable_view()

    def _batch_data(self, conn, owner, batch_id):
        row = conn.execute("SELECT * FROM batches WHERE id=? AND owner=?",(batch_id,owner)).fetchone()
        return {"id":row["id"],"state":row["state"],"runs":[self.serialize(run,conn) for run in conn.execute("SELECT * FROM runs WHERE batch_id=? ORDER BY ordinal",(batch_id,)).fetchall()]}

    def view_batch(self, owner, view_id, batch_id):
        with closing(self.db.connect()) as conn:
            conn.execute("BEGIN")
            self._live_view(conn,owner,view_id)
            self._view_membership(conn,owner,view_id,batch_id=batch_id)
            return self._batch_data(conn,owner,batch_id)

    def reader_view(self, owner, view_id):
        with closing(self.db.connect()) as conn:
            conn.execute("BEGIN")
            return self._view_result(conn,owner,self._live_view(conn,owner,view_id))

    def _view_result(self, conn, owner, row, reused=False):
        items = [dict(item,display_source=source_hostname(item.get("link"))) for item in json.loads(row["items_snapshot"])[:10]]
        bound = conn.execute("SELECT r.* FROM view_runs v JOIN runs r ON r.id=v.run_id WHERE v.view_id=? AND r.owner=? ORDER BY r.created,r.rowid",(row["id"],owner)).fetchall()
        latest = {run["item_id"]:run for run in bound}
        runs = [self.serialize(latest[item["id"]],conn) for item in items if item["id"] in latest]
        active = conn.execute("SELECT DISTINCT b.id FROM batches b JOIN runs r ON r.batch_id=b.id JOIN view_runs v ON v.run_id=r.id WHERE v.view_id=? AND b.owner=? AND b.state='active'",(row["id"],owner)).fetchone()
        batch = self._batch_data(conn,owner,row["batch_id"]) if row["batch_id"] else None
        return {"feed":json.loads(row["feed_snapshot"]),"items":items,"runs":runs,"active_batch_id":active["id"] if active else None,
                "generation":{"status":"reused" if reused and batch else row["generation_status"],"batch":batch,"error":json.loads(row["error"]) if row["error"] else None,"retry_after":row["retry_after"]},
                "view":{"id":row["id"],"url":"/feeds/"+row["id"],"created_at":row["created"],"expires_at":row["expires_at"]},"server_now":time.time()}

    def view_feed(self, owner, url, key, fetched):
        self.validate_key(key)
        if not isinstance(url,str) or len(url)>2048 or any(unicodedata.category(char)=="Cs" for char in url):
            raise AppError("INVALID_FEED", "Enter a bounded feed URL.")
        self.db.compact_expired_views()
        with self.db.transaction() as conn:
            row = conn.execute("SELECT * FROM feed_views WHERE owner=? AND idempotency_key=?", (owner,key)).fetchone()
            if row:
                self._live_view(conn,owner,row["id"])
                if row["url"] != url:
                    raise AppError("IDEMPOTENCY_CONFLICT", "This request key was already used for a different feed URL.",409)
                reused = True
            else:
                reused = False
                now,view_id = time.time(),new_id()
                # Count and insert share BEGIN IMMEDIATE with quota admission.
                # Concurrent fresh keys cannot overrun the per-owner payload cap;
                # exact-key replays above never consume another view slot.
                self.require_view_capacity(owner,conn,now)
                fetched = bounded_feed(fetched,url)
                feed_id = self._save_feed(conn,owner,url,fetched)
                feed,items = self._feed_data(conn,owner,feed_id)
                conn.execute("INSERT INTO feed_views(owner,idempotency_key,url,feed_id,feed_snapshot,items_snapshot,generation_status,created,id,expires_at) VALUES(?,?,?,?,?,?,'idle',?,?,?)",(owner,key,url,feed_id,dump(feed),dump(items),now,view_id,now+3600))
                # Bound raw source positions before deduplication; never drain
                # later entries after this page's one atomic admission.
                candidates = []
                for source_item in fetched["items"][:10]:
                    item_id = conn.execute("SELECT id FROM items WHERE feed_id=? AND guid=?", (feed_id,source_item["guid"])).fetchone()[0]
                    if item_id not in candidates:
                        candidates.append(item_id)
                status,batch_id,error,retry_after = "idle",None,None,None
                if candidates:
                    try:
                        rate_limit(conn,owner,"submit",self.config.submit_limit)
                        internal_key = "view:" + hashlib.sha256(key.encode()).hexdigest()
                        budget = self._admission_budget(conn,owner,time.time())
                        count = min(len(candidates),budget["owner_slots"],budget["hourly"]["remaining"],budget["global_slots"],budget["page_limit"])
                        # At zero capacity, one candidate lets authoritative admission
                        # report its existing denial code instead of an idle page.
                        selected = candidates[:max(1,count)]
                        batch_id = self._admit(conn,owner,selected,internal_key,view_id=view_id,budget=budget)
                        conn.execute("INSERT INTO view_runs SELECT ?,id FROM runs WHERE batch_id=?",(view_id,batch_id))
                        status = "started"
                    except AppError as exc:
                        status,error,retry_after = "blocked",dump({"code":exc.code,"message":exc.message}),exc.retry_after
                conn.execute("UPDATE feed_views SET batch_id=?,generation_status=?,error=?,retry_after=? WHERE id=?",(batch_id,status,error,retry_after,view_id))
                row = conn.execute("SELECT * FROM feed_views WHERE owner=? AND idempotency_key=?", (owner,key)).fetchone()
        return self.view_result(owner,dict(row),reused)

    def retry_view(self, owner, view_id, run_id, key):
        self.validate_key(key)
        with self.db.transaction() as conn:
            self._live_view(conn,owner,view_id)
            self._view_membership(conn,owner,view_id,run_id=run_id)
            old = conn.execute("SELECT item_id FROM runs WHERE id=?",(run_id,)).fetchone()
            if not conn.execute("SELECT 1 FROM batches WHERE owner=? AND idempotency_key=?",(owner,key)).fetchone():
                rate_limit(conn,owner,"submit",self.config.submit_limit)
            batch_id = self._admit(conn,owner,[old["item_id"]],key,run_id,view_id=view_id)
            conn.execute("INSERT OR IGNORE INTO view_runs SELECT ?,id FROM runs WHERE batch_id=?",(view_id,batch_id))
        return self.view_batch(owner,view_id,batch_id)

    def _admit(self, conn, owner, item_ids, key, parent_id=None, view_id=None, budget=None):
        if view_id is not None:
            self._live_view(conn,owner,view_id)
        existing = conn.execute("SELECT id FROM batches WHERE owner=? AND idempotency_key=?", (owner,key)).fetchone()
        if existing:
            saved = conn.execute("SELECT item_id,parent_run_id FROM runs WHERE batch_id=? ORDER BY ordinal", (existing["id"],)).fetchall()
            if [row["item_id"] for row in saved] != item_ids or any(row["parent_run_id"] != parent_id for row in saved):
                raise AppError("IDEMPOTENCY_CONFLICT", "This request key was already used for different generation inputs.",409)
            return existing["id"]
        if conn.execute("SELECT 1 FROM batches WHERE state='active'").fetchone():
            raise AppError("BUSY", "Another batch is active. Try again after it finishes.",429,5)
        budget = budget or self._admission_budget(conn,owner,time.time())
        now = budget["now"]
        if len(item_ids)>budget["owner_slots"]:
            raise AppError("QUOTA_EXCEEDED", "Hourly image attempt allowance is exhausted.",429,3600)
        parent = None
        if parent_id:
            parent = conn.execute("SELECT * FROM runs WHERE id=? AND owner=?", (parent_id,owner)).fetchone()
            if not parent:
                raise AppError("FORBIDDEN","You cannot retry this run.",403)
            if parent["state"] not in ("failed","completed"):
                raise AppError("NOT_RETRYABLE","Wait for this run to reach a confirmed terminal state.",409)
        snapshots = []
        for item_id in item_ids:
            item = conn.execute("SELECT i.*,f.canonical_id FROM items i JOIN feeds f ON f.id=i.feed_id WHERE i.id=? AND f.owner=?", (item_id,owner)).fetchone()
            if not item:
                raise AppError("FORBIDDEN","You cannot generate this item.",403)
            snapshots.append(item)
        policy = conn.execute("SELECT * FROM quota_policy WHERE id=1").fetchone()
        hourly = budget["hourly"]
        if hourly["used"]+len(item_ids)>hourly["limit"]:
            needed = hourly["used"]+len(item_ids)-hourly["limit"]
            slot = conn.execute("SELECT created FROM runs WHERE "+HOURLY_STARTS+" ORDER BY created LIMIT 1 OFFSET ?",(now-3600,policy["reset_epoch"],max(0,needed-1))).fetchone()
            retry_at = slot["created"]+3600 if slot else now+3600
            raise AppError("GLOBAL_HOURLY_LIMIT","The shared hourly generation limit is exhausted.",429,max(1,int(retry_at-now)+1))
        if len(item_ids)>budget["global_slots"]:
            raise AppError("GLOBAL_DAILY_LIMIT","The shared daily image allowance is exhausted.",429,max(1,int(budget["reset_at"]-now)+1))
        settings = validate_settings(json.loads(parent["settings"]) if parent else json.loads(conn.execute("SELECT value FROM settings WHERE id=1").fetchone()[0]))
        if view_id is not None:
            self._live_view(conn,owner,view_id)
        batch_id = new_id()
        conn.execute("INSERT INTO batches VALUES(?,?,?,?,?)", (batch_id,owner,key,"active",now))
        for ordinal,item in enumerate(snapshots):
            snapshot = {"title":item["title"],"excerpt":item["excerpt"],"link":item["link"],"published":item["published"],"article":json.loads(item["article"])}
            if parent:
                snapshot = json.loads(parent["original"])
            # Snapshot dimensions at admission. Retries and already queued runs
            # keep their original preset if an operator changes the default.
            width = parent["width"] if parent else self.config.flux_image_size
            height = parent["height"] if parent else self.config.flux_image_size
            conn.execute(
                "INSERT INTO runs(id,batch_id,owner,item_id,ordinal,state,parent_run_id,settings,original,"
                "qwen_prompt,final_prompt,matched_terms,used_fallback,seed,created,reserved_at,quota_epoch,"
                "admission_epoch,width,height) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (new_id(),batch_id,owner,item["id"],ordinal,"queued",parent_id,dump(settings),dump(snapshot),
                 parent["qwen_prompt"] if parent else None,parent["final_prompt"] if parent else None,
                 parent["matched_terms"] if parent else "[]",parent["used_fallback"] if parent else 0,
                 secrets.randbits(32),now,now,policy["reset_epoch"],policy["reset_epoch"],width,height),
            )
        return batch_id

    def admit(self, owner, item_ids, key, parent_id=None):
        self.validate_key(key)
        if not isinstance(item_ids,list) or not 1 <= len(item_ids) <= 3 or any(not isinstance(x,str) for x in item_ids) or len(set(item_ids)) != len(item_ids):
            raise AppError("INVALID_INPUT", "Select one to three distinct items.")
        if not self.db.one("SELECT id FROM batches WHERE owner=? AND idempotency_key=?", (owner,key)):
            self.limit(owner,"submit")
        with self.db.transaction() as conn:
            batch_id = self._admit(conn,owner,item_ids,key,parent_id)
        return self.batch(owner,batch_id)

    def run(self, owner, run_id):
        row = self.db.one("SELECT * FROM runs WHERE id=? AND owner=?", (run_id,owner))
        if not row:
            raise AppError("FORBIDDEN","You cannot access this generation.",403)
        return self.serialize(row)

    def serialize(self, row, conn=None):
        result = {key:row[key] for key in ("id","batch_id","item_id","state","parent_run_id","qwen_prompt","final_prompt","seed","width","height","completed_at","result_expires_at","created")}
        result.update(original=json.loads(row["original"]),matched_terms=json.loads(row["matched_terms"]),used_fallback=bool(row["used_fallback"]),expiry_estimated=bool(row["expiry_estimated"]),error=json.loads(row["error"]) if row["error"] else None)
        result["original"].pop("article",None)
        result["invocations"] = []
        sql = "SELECT * FROM invocations WHERE run_id=? ORDER BY created"
        invocations = conn.execute(sql,(row["id"],)).fetchall() if conn else self.db.all(sql,(row["id"],))
        for invocation in invocations:
            output = json.loads(invocation["output"]) if invocation["output"] else {}
            result["invocations"].append({"id":invocation["id"],"stage":invocation["stage"],"provider_id":invocation["provider_id"],"status":invocation["status"],"model":output.get("model"),"usage":output.get("usage"),"timings":output.get("timings"),"counters":output.get("counters"),"delay_ms":invocation["delay_ms"],"execution_ms":invocation["execution_ms"]})
        if row["state"] == "completed":
            result["availability"] = "available_to_retrieve" if row["result_expires_at"] and row["result_expires_at"] > 0 and (row["expiry_estimated"] or row["result_expires_at"] > time.time()) else "unavailable"
        else:
            result["availability"] = "pending" if row["state"] not in ("failed",) else "unavailable"
        return result

    def batch(self, owner, batch_id):
        row = self.db.one("SELECT * FROM batches WHERE id=? AND owner=?", (batch_id,owner))
        if not row:
            raise AppError("FORBIDDEN","You cannot access this batch.",403)
        return {"id":row["id"],"state":row["state"],"runs":[self.serialize(run) for run in self.db.all("SELECT * FROM runs WHERE batch_id=? ORDER BY ordinal", (batch_id,))]}

    def history(self, owner):
        return {"runs":[self.serialize(run) for run in self.db.all("SELECT * FROM runs WHERE owner=? ORDER BY created DESC LIMIT 30", (owner,))],"active_batch_id":(self.db.one("SELECT id FROM batches WHERE owner=? AND state='active'", (owner,)) or {}).get("id")}
