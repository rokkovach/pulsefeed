"""Advance persisted jobs while retaining uncertainty about provider acceptance.

Saving submission intent before dispatch avoids accidental duplicate GPU work.
An ambiguous submission or cancellation keeps its reservation until a known
outcome can be reconciled; retrying a status read never resubmits the job.
"""
import asyncio
import json
import logging
import time
from .db import dump, new_id, DEFAULT_QWEN_ARTICLE_TEMPLATE
from .errors import AppError
from .provider import SubmissionUnknown, ReadUnavailable, TERMINAL, validate_envelope, metadata_only, nullable_metric, unwrap_output
from .service import compose_prompt

log = logging.getLogger(__name__)
IMAGE_RETRIEVAL_TIMEOUT = 30.0


def error_payload(code, message, stage, retryable=False):
    return {"code":code,"message":message,"stage":stage,"retryable":retryable}


class Coordinator:
    def __init__(self, db, config, provider):
        self.db,self.config,self.provider = db,config,provider
        self.lock = asyncio.Lock()
        self.stopped = asyncio.Event()

    async def loop(self):
        # Persisted batches already reserve global capacity while startup reconciliation runs.
        while not self.stopped.is_set():
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never log worker payloads, credentials or access URLs.
                log.exception("Coordinator tick failed; persisted invocation remains reserved")
            try:
                await asyncio.wait_for(self.stopped.wait(), timeout=self.config.coordinator_interval)
            except asyncio.TimeoutError:
                pass

    def _finish(self, conn, run, error=None, reserved=None):
        conn.execute("UPDATE runs SET state=?,error=?,reserved=COALESCE(?,reserved),quota_finalized_at=?,quota_epoch=(SELECT reset_epoch FROM quota_policy WHERE id=1) WHERE id=?", ("failed" if error else "completed",dump(error) if error else None,reserved,time.time(),run["id"]))
        if not conn.execute("SELECT 1 FROM runs WHERE batch_id=? AND state NOT IN ('failed','completed')", (run["batch_id"],)).fetchone():
            conn.execute("UPDATE batches SET state='finished' WHERE id=?", (run["batch_id"],))

    def finish(self, run, error=None, reserved=None, invocation_id=None):
        with self.db.transaction() as conn:
            if invocation_id:
                conn.execute("UPDATE invocations SET status='failed' WHERE id=?", (invocation_id,))
            self._finish(conn, run, error, reserved)

    def record_result(self, run, invocation, output, response=None, deadline_exceeded=False, recovered=False):
        """One commit records observed results, accounting, stage advancement and batch release."""
        response = response or {}
        stage = invocation["stage"]
        actual = output["counters"]["inference_attempts"]
        reserved = (actual if actual is not None else run["reserved"]) if stage == "flux" else 0
        failure = None
        composed = None
        if deadline_exceeded:
            failure = error_payload("DEADLINE_EXCEEDED", "The item deadline expired; provider termination is confirmed.", stage)
        elif not output["ok"]:
            failure = error_payload(output["error"]["code"], output["error"]["message"][:500], stage, output["error"]["retryable"])
        elif stage == "qwen":
            try:
                composed = compose_prompt(output["data"]["prompt"], json.loads(run["settings"]))
            except AppError as exc:
                failure = error_payload(exc.code, exc.message, stage)
        delay, execution = response.get("delayTime"), response.get("executionTime")
        now = time.time()
        expiry = response.get("expiresAt")
        explicit = isinstance(expiry,(int,float)) and not isinstance(expiry,bool) and nullable_metric(expiry)
        with self.db.transaction() as conn:
            if not recovered:
                conn.execute("UPDATE invocations SET status=?,output=?,delay_ms=?,execution_ms=? WHERE id=?", ("succeeded" if output["ok"] else "failed",dump(metadata_only(output)),delay if nullable_metric(delay) else None,execution if nullable_metric(execution) else None,invocation["id"]))
            if failure:
                self._finish(conn, run, failure, reserved)
            elif stage == "qwen":
                final, matched, fallback = composed
                conn.execute("UPDATE runs SET qwen_prompt=?,final_prompt=?,matched_terms=?,used_fallback=?,state='generating_image' WHERE id=?", (output["data"]["prompt"], final, dump(matched), int(fallback), run["id"]))
            else:
                # Recovery trusts durable verification, not provider retention. Unknown old expiry
                # stays unavailable rather than inventing a new retention window.
                result_expiry = (run["result_expires_at"] or 0) if recovered else (expiry if explicit else now+1800)
                estimated = run["expiry_estimated"] if recovered else int(not explicit)
                conn.execute("UPDATE runs SET completed_at=?,result_expires_at=?,expiry_estimated=? WHERE id=?", (run["completed_at"] or now,result_expiry,estimated,run["id"]))
                self._finish(conn, run, reserved=reserved)

    def prepare(self, run, stage):
        invocation_id = new_id()
        payload = {"schema_version":1,"stage":stage,"run_id":run["id"],"invocation_id":invocation_id,"deadline_unix_ms":int(run["deadline"]*1000)}
        if stage == "qwen":
            settings = json.loads(run["settings"])
            payload.update(article=json.loads(run["original"])["article"], system_prompt=settings["qwen_template"], max_new_tokens=256)
            article_template = settings.get("qwen_article_template")
            if article_template is not None and article_template != DEFAULT_QWEN_ARTICLE_TEMPLATE:
                payload["article_template"] = article_template
        else:
            payload.update(prompt=run["final_prompt"],seed=run["seed"],width=run["width"],height=run["height"],num_inference_steps=4,guidance_scale=1.0,output_format="webp")
        self.db.execute("INSERT INTO invocations(id,run_id,stage,status,input,created,execution_mode) VALUES(?,?,?,?,?,?,?)", (invocation_id,run["id"],stage,"prepared",dump(payload),time.time(),"synthetic" if self.config.fake_mode else "live"))
        return self.db.one("SELECT * FROM invocations WHERE id=?", (invocation_id,))

    def _active_runs(self):
        return self.db.all("SELECT r.* FROM runs r JOIN batches b ON b.id=r.batch_id WHERE b.state='active' AND r.state NOT IN ('completed','failed') ORDER BY r.ordinal,r.rowid")

    def _invocation(self, run):
        stage = "flux" if run["state"] == "generating_image" or (run["state"] == "queued" and run["final_prompt"]) else "qwen"
        return stage,self.db.one("SELECT * FROM invocations WHERE run_id=? AND stage=? ORDER BY created DESC,rowid DESC LIMIT 1",(run["id"],stage))

    def _mark_unknown(self, run, invocation, stage, message):
        with self.db.transaction() as conn:
            conn.execute("UPDATE runs SET state='submission_unknown',error=? WHERE id=?",(dump(error_payload("INFERENCE_OUTCOME_UNKNOWN",message,stage)),run["id"]))
            conn.execute("UPDATE invocations SET status='unknown' WHERE id=?",(invocation["id"],))

    def _dispatch_frozen(self):
        return bool(self.db.one("SELECT 1 FROM runs r JOIN batches b ON b.id=r.batch_id WHERE b.state='active' AND r.state='submission_unknown' LIMIT 1"))

    async def tick(self):
        async with self.lock:
            self.db.compact_expired_views()
            runs = self._active_runs()
            # A persisted acceptance gap is uncertainty, never a resubmission.
            # Resolve it before any concurrent stage operation can dispatch.
            for run in runs:
                if run["state"] in ("queued","submission_unknown"):
                    continue
                stage,invocation = self._invocation(run)
                if invocation and (invocation["status"] == "unknown" or (invocation["status"] == "submitting" and not invocation["provider_id"])):
                    self._mark_unknown(run,invocation,stage,"Submission acceptance is unknown. An operator must reconcile it before retrying.")
            runs = self._active_runs()
            frozen = self._dispatch_frozen()
            started = [run for run in runs if run["state"] != "queued"]
            chosen = {"flux": [], "qwen": []}
            waiting = {"flux": [], "qwen": []}
            limit = self.config.coordinator_stage_concurrency
            # Poll all accepted jobs, even after reducing the configured limit.
            # Losing track of a provider ID can orphan GPU work or refund quota
            # prematurely. Unsent work never takes an accepted job's stage slot.
            for run in started:
                if run["state"] == "submission_unknown":
                    continue
                stage,invocation = self._invocation(run)
                if invocation and invocation["provider_id"]:
                    chosen[stage].append(run)
                else:
                    waiting[stage].append(run)
            for stage in ("flux", "qwen"):
                capacity = max(0, limit-len(chosen[stage]))
                chosen[stage].extend(waiting[stage][:capacity])
            slots = max(0,self.config.coordinator_pipeline_size-len(started))
            if not frozen and slots:
                for run in runs:
                    if run["state"] != "queued":
                        continue
                    stage,_ = self._invocation(run)
                    if len(chosen[stage]) < limit:
                        chosen[stage].append(run)
                        slots -= 1
                        if not slots:
                            break
            # Bound both per-model dispatch and total started stories. Each
            # task persists its own intent before submitting. Drain every task
            # before releasing this coordinator's single-process tick lock.
            tasks = [asyncio.create_task(self._advance(run,not frozen))
                     for stage in ("flux","qwen") for run in chosen[stage]]
            try:
                results = await asyncio.gather(*tasks,return_exceptions=True)
            except BaseException:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks,return_exceptions=True)
                raise
            for result in results:
                if isinstance(result,BaseException):
                    raise result

    async def _advance(self, run, allow_dispatch):
        if run["state"] == "queued":
            if not allow_dispatch or self._dispatch_frozen():
                return
            self.db.execute("UPDATE runs SET state=?,deadline=? WHERE id=?", ("generating_image" if run["final_prompt"] else "creating_prompt",time.time()+self.config.deadline_seconds,run["id"]))
            run = self.db.one("SELECT * FROM runs WHERE id=?", (run["id"],))
        if run["state"] == "submission_unknown":
            return
        stage = "flux" if run["state"] == "generating_image" else "qwen"
        invocation = self.db.one("SELECT * FROM invocations WHERE run_id=? AND stage=? ORDER BY created DESC,rowid DESC LIMIT 1", (run["id"],stage))
        deadline_exceeded = time.time() >= run["deadline"]
        if not invocation:
            if deadline_exceeded:
                self.finish(run,error_payload("DEADLINE_EXCEEDED","The item deadline expired before submission.",stage),0)
                return
            if not allow_dispatch or self._dispatch_frozen():
                return
            invocation = self.prepare(run,stage)
        if invocation["status"] == "succeeded" and invocation["output"]:
            self.record_result(run, invocation, json.loads(invocation["output"]), deadline_exceeded=deadline_exceeded and stage == "qwen", recovered=True)
            return
        if invocation["status"] in ("submitting","unknown") and not invocation["provider_id"]:
            self._mark_unknown(run,invocation,stage,"Submission acceptance is unknown. An operator must reconcile it before retrying.")
            return
        if invocation["status"] == "prepared" and not invocation["provider_id"]:
            if not allow_dispatch or self._dispatch_frozen():
                return
            if deadline_exceeded:
                self.finish(run,error_payload("DEADLINE_EXCEEDED","The item deadline expired before submission.",stage),0)
                return
            self.db.execute("UPDATE invocations SET status='submitting' WHERE id=?", (invocation["id"],))
            # Persist intent before the network call. A crash after acceptance
            # but before saving provider_id is an unknown outcome, never proof
            # that sending the request again would be safe or free.
            try:
                provider_id = await self.provider.submit(stage,json.loads(invocation["input"]))
            except SubmissionUnknown:
                self._mark_unknown(run,invocation,stage,"The provider may have accepted this request. No automatic resubmission occurred; operator reconciliation is required.")
                return
            except AppError as exc:
                self.finish(run,error_payload(exc.code,exc.message,stage),0,invocation["id"])
                return
            except Exception:
                self._mark_unknown(run,invocation,stage,"Submission acceptance is unknown. An operator must reconcile it before retrying.")
                raise
            self.db.execute("UPDATE invocations SET provider_id=?,status='pending' WHERE id=?", (provider_id,invocation["id"]))
            return
        if not invocation["provider_id"]:
            return
        if deadline_exceeded:
            self.db.execute("UPDATE runs SET error=? WHERE id=?", (dump(error_payload("DEADLINE_EXCEEDED","Deadline exceeded; awaiting confirmed provider termination.",stage)),run["id"]))
            if invocation["status"] != "cancelling":
                self.db.execute("UPDATE invocations SET status='cancelling' WHERE id=?", (invocation["id"],))
                await self.provider.cancel(stage,invocation["provider_id"])
        try:
            response = await asyncio.wait_for(self.provider.status(stage,invocation["provider_id"]), timeout=30 if deadline_exceeded else max(0.01,run["deadline"]-time.time()))
        except (ReadUnavailable, asyncio.TimeoutError):
            return
        status = response.get("status")
        if status in ("IN_QUEUE","IN_PROGRESS"):
            return
        if status not in TERMINAL:
            if status == "MISSING":
                self._mark_unknown(run,invocation,stage,"Provider result is missing before success was verified. Operator reconciliation is required.")
            return
        if status != "COMPLETED":
            self.finish(run,error_payload("DEADLINE_EXCEEDED" if deadline_exceeded else ("CANCELLED" if status=="CANCELLED" else "INFERENCE_FAILED"),"Provider job ended without a successful result.",stage,not deadline_exceeded),run["reserved"] if stage == "flux" else 0,invocation["id"])
            return
        try:
            output = unwrap_output(response.get("output"))
            validate_envelope(output,invocation,json.loads(invocation["input"]))
        except AppError as exc:
            self.finish(run,error_payload(exc.code,exc.message,stage),run["reserved"] if stage == "flux" else 0,invocation["id"])
            return
        self.record_result(run, invocation, output, response, deadline_exceeded)

    async def retrieve(self, owner, run_id):
        run = self.db.one("SELECT * FROM runs WHERE id=? AND owner=?", (run_id,owner))
        if not run:
            raise AppError("FORBIDDEN","You cannot access this image.",403)
        if run["state"] != "completed":
            raise AppError("IMAGE_UNAVAILABLE","This generation has no confirmed image.",409)
        invocation = self.db.one("SELECT * FROM invocations WHERE run_id=? AND stage='flux' AND status='succeeded' ORDER BY created DESC LIMIT 1", (run_id,))
        if not invocation or not invocation["provider_id"]:
            raise AppError("IMAGE_UNAVAILABLE","The provider image is no longer available. Regenerate explicitly using the saved prompt.",410)
        try:
            # Bound the complete read, including provider retries/backoff; each
            # transport request timeout alone cannot bound the reader lifetime.
            async with asyncio.timeout(IMAGE_RETRIEVAL_TIMEOUT):
                response = await self.provider.status("flux",invocation["provider_id"])
        except (ReadUnavailable, asyncio.TimeoutError) as exc:
            raise AppError("RESULT_UNAVAILABLE","Image retrieval failed temporarily. Try retrieving again.",503) from exc
        if response.get("status") != "COMPLETED" or not response.get("output"):
            self.db.execute("UPDATE runs SET result_expires_at=0 WHERE id=?", (run_id,))
            raise AppError("IMAGE_UNAVAILABLE","The provider image is no longer available. Regenerate explicitly using the saved prompt.",410)
        output = unwrap_output(response["output"])
        raw = validate_envelope(output,invocation,json.loads(invocation["input"]))
        if not output["ok"] or raw is None:
            raise AppError("IMAGE_UNAVAILABLE","The provider image is no longer available.",410)
        return raw,output["data"]["mime_type"]
