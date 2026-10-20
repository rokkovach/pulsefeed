"""Authenticated, bounded development transport for a single GPU worker.

Run inside the Spark containers, one process per model. Results are temporary
RAM only. A restart deliberately forgets jobs: clients must reconcile missing
IDs as unknown rather than replaying paid or expensive inference.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import fcntl
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib
import json
import os
import re
import signal
import threading
import time
import uuid

MAX_REQUEST_BYTES = 65_536
MAX_RESPONSE_BYTES = 4_194_304 + 8_192
DEFAULT_RESULT_CAPACITY = 128
MAX_RESULT_CAPACITY = 256


class Rejected(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message


@dataclass
class Job:
    id: str
    invocation: str
    fingerprint: str
    deadline: float
    state: str = "IN_QUEUE"
    cancelled: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    cancel_at: float | None = None
    expires_at: float | None = None
    response: bytes | None = None


def encoded(value):
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).encode()


def result_capacity_from_env():
    value = os.getenv("LOCAL_RESULT_CAPACITY", str(DEFAULT_RESULT_CAPACITY))
    if not re.fullmatch(r"[0-9]{1,3}", value) or not 1 <= int(value) <= MAX_RESULT_CAPACITY:
        raise SystemExit("LOCAL_RESULT_CAPACITY must be an integer from 1 to 256.")
    return int(value)


class Gateway:
    def __init__(self, worker, token, *, lock_path=None, ttl=3600, capacity=DEFAULT_RESULT_CAPACITY,
                 execution_seconds=660, cancel_grace=5, force_exit=os._exit):
        if not isinstance(token, str) or len(token) < 32 or any(c.isspace() for c in token):
            raise ValueError("LOCAL_WORKER_TOKEN must contain at least 32 non-whitespace characters.")
        if ttl < execution_seconds or type(capacity) is not int or not 1 <= capacity <= MAX_RESULT_CAPACITY:
            raise ValueError("Result retention must cover execution, with integer capacity from 1 to 256.")
        self.worker, self.token, self.lock_path = worker, token, lock_path
        self.ttl, self.capacity = ttl, capacity
        self.execution_seconds, self.cancel_grace = execution_seconds, cancel_grace
        self.force_exit = force_exit
        self.lock = threading.RLock()
        self.jobs = {}
        self.closed = threading.Event()
        self.monitor = threading.Thread(target=self._watch, daemon=True)
        self.monitor.start()

    def _prune(self):
        now = time.time()
        self.jobs = {key: job for key, job in self.jobs.items()
                     if job.expires_at is None or job.expires_at > now}

    def health(self):
        with self.lock:
            if self.closed.is_set() or not getattr(self.worker, "ready", True):
                raise Rejected(503, "Worker unavailable; inspect container health and restart if needed.")
            self._prune()
            return {"status": "ready", "active_jobs": sum(not j.done.is_set() for j in self.jobs.values()),
                    "retained_jobs": len(self.jobs), "result_capacity": self.capacity}

    def submit(self, body):
        if not isinstance(body, dict) or set(body) != {"input"} or not isinstance(body["input"], dict):
            raise Rejected(422, "An input object is required; webhooks and extra fields are unsupported.")
        value = body["input"]
        invocation = value.get("invocation_id")
        try:
            if not isinstance(invocation, str) or str(uuid.UUID(invocation)) != invocation.lower():
                raise ValueError
        except (ValueError, AttributeError):
            raise Rejected(422, "A UUID invocation_id is required.") from None
        fingerprint = hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()
        with self.lock:
            self._prune()
            for job in self.jobs.values():
                if job.invocation == invocation:
                    if job.fingerprint != fingerprint:
                        raise Rejected(422, "The invocation ID was already used for different input.")
                    return {"id": job.id, "status": job.state}
            deadline = value.get("deadline_unix_ms")
            if type(deadline) is not int or not 0 < deadline <= 9_007_199_254_740_991:
                raise Rejected(422, "A valid deadline_unix_ms is required.")
            remaining = deadline / 1000 - time.time()
            if remaining <= 0:
                raise Rejected(422, "The invocation deadline has expired.")
            if (self.closed.is_set() or not getattr(self.worker, "ready", True)
                    or any(not j.done.is_set() for j in self.jobs.values())):
                raise Rejected(422, "Worker busy or stopping; no job was accepted.")
            # Do not evict unexpired images that clients may still need to retrieve.
            if len(self.jobs) >= self.capacity:
                raise Rejected(422, "Temporary result capacity reached; no job was accepted.")
            job = Job(uuid.uuid4().hex, invocation, fingerprint,
                      time.monotonic() + min(self.execution_seconds, remaining))
            self.jobs[job.id] = job
            threading.Thread(target=self._execute, args=(job, body), daemon=True).start()
            return {"id": job.id, "status": "IN_QUEUE"}

    def _finish(self, job, status, output=None):
        with self.lock:
            expires = time.time() + self.ttl
            response = {"id": job.id, "status": status, "expiresAt": expires}
            if output is not None:
                response["output"] = {"result": output}
            wire = encoded(response)
            if len(wire) > MAX_RESPONSE_BYTES:
                raise ValueError("Worker result exceeded transport limit.")
            job.response, job.expires_at, job.state = wire, expires, status
            job.done.set()

    def _execute(self, job, body):
        lock_file = None
        try:
            if self.lock_path:
                lock_file = open(self.lock_path, "a", encoding="utf-8")
                while not job.cancelled.is_set():
                    try:
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        job.cancelled.wait(0.05)
            if job.cancelled.is_set():
                self._finish(job, "CANCELLED")
                return
            with self.lock:
                job.state = "IN_PROGRESS"
            # Worker envelopes retain counters even when cancellation races success.
            result = self.worker.handle({"id": job.id, "input": body["input"]}, cancelled=job.cancelled)
            if isinstance(result, dict) and (result.get("error") or {}).get("code") == "INFERENCE_OUTCOME_UNKNOWN":
                # The native operation may still be running. Keep the GPU flock
                # until process termination; never publish a false terminal state
                # that could admit the other model while execution is uncertain.
                self.closed.set()
                self.force_exit(70)
                return  # Only reachable with an injected test exit hook.
            self._finish(job, "COMPLETED", result)
        except Exception:
            # Do not leak model prompts, credentials or exception text over HTTP/logs.
            self._finish(job, "FAILED")
        finally:
            if lock_file:
                lock_file.close()

    def _get(self, identifier):
        self._prune()
        if identifier not in self.jobs:
            raise Rejected(404, "Job unknown or expired. Do not automatically resubmit.")
        return self.jobs[identifier]

    def status(self, identifier):
        with self.lock:
            job = self._get(identifier)
            return job.response or encoded({"id": job.id, "status": job.state})

    def cancel(self, identifier):
        with self.lock:
            job = self._get(identifier)
            if not job.done.is_set() and job.cancel_at is None:
                job.cancel_at = time.monotonic()
                job.cancelled.set()
            # A cancellation request is not evidence that native work has stopped.
            return job.response or encoded({"id": job.id, "status": job.state})

    def _watch(self):
        while not self.closed.wait(0.1):
            with self.lock:
                now = time.monotonic()
                for job in self.jobs.values():
                    if job.done.is_set():
                        continue
                    if job.cancel_at is None and now >= job.deadline:
                        job.cancel_at = now
                        job.cancelled.set()
                    if job.cancel_at is not None and now - job.cancel_at >= self.cancel_grace:
                        # Entrypoint exits: Docker stops the GPU process namespace.
                        # On restart, missing jobs remain unknown to the laptop.
                        self.force_exit(70)
                        return

    def close(self):
        with self.lock:
            self.closed.set()
            active = [j for j in self.jobs.values() if not j.done.is_set()]
            for job in active:
                job.cancelled.set()
        until = time.monotonic() + self.cancel_grace
        for job in active:
            if not job.done.wait(max(0, until - time.monotonic())):
                self.force_exit(70)
                return
        close = getattr(self.worker, "close", None)
        if close:
            close()


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, gateway):
        self.gateway = gateway
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(address, Handler)

    def process_request(self, request, address):
        request.settimeout(5)
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()

    def handle_error(self, request, address):
        pass


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        self._dispatch()

    def do_POST(self):
        self._dispatch()

    def _reply(self, status, body):
        wire = body if isinstance(body, bytes) else encoded(body)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(wire)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(wire)

    def _dispatch(self):
        gateway = self.server.gateway
        try:
            authorization = self.headers.get_all("Authorization", [])
            if len(authorization) != 1 or not hmac.compare_digest(
                authorization[0].encode(), ("Bearer " + gateway.token).encode()
            ):
                raise Rejected(401, "Authentication required.")
            if self.command == "GET" and self.path == "/health":
                result = gateway.health()
            elif self.command == "POST" and self.path == "/run":
                if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) != 1:
                    raise Rejected(400, "One Content-Length header is required; chunked requests are unsupported.")
                try:
                    length = int(self.headers["Content-Length"])
                except ValueError:
                    raise Rejected(400, "Invalid Content-Length.") from None
                if not 0 < length <= MAX_REQUEST_BYTES:
                    raise Rejected(413, "Request exceeds the supported byte limit.")
                if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
                    raise Rejected(400, "Content-Type must be application/json.")
                wire = self.rfile.read(length)
                if len(wire) != length:
                    raise Rejected(400, "Incomplete request.")
                try:
                    def reject_constant(_):
                        raise ValueError
                    body = json.loads(wire, parse_constant=reject_constant)
                    result = gateway.submit(body)
                except (ValueError, UnicodeError, RecursionError):
                    raise Rejected(400, "Invalid JSON request.") from None
            elif match := re.fullmatch(r"/(status|cancel)/([0-9a-f]{32})", self.path):
                operation, identifier = match.groups()
                if (operation == "status" and self.command == "GET"):
                    result = gateway.status(identifier)
                elif operation == "cancel" and self.command == "POST":
                    result = gateway.cancel(identifier)
                else:
                    raise Rejected(405, "Unsupported method.")
            else:
                raise Rejected(404, "Unknown route.")
            self._reply(200, result)
        except Rejected as exc:
            self._reply(exc.status, {"error": exc.message})
        except TimeoutError:
            self._reply(408, {"error": "Request body timed out."})


def main():
    token = os.environ.get("LOCAL_WORKER_TOKEN", "")
    # Validate before loading expensive model weights.
    if len(token) < 32 or any(c.isspace() for c in token):
        raise SystemExit("Set LOCAL_WORKER_TOKEN to at least 32 non-whitespace characters.")
    result_capacity = result_capacity_from_env()  # Reject before model imports/loading.
    startup_seconds = int(os.getenv("LOCAL_STARTUP_TIMEOUT_SECONDS", "660"))
    if not 1 <= startup_seconds <= 1800:
        raise SystemExit("LOCAL_STARTUP_TIMEOUT_SECONDS must be between 1 and 1800.")
    port = int(os.getenv("LOCAL_WORKER_PORT", "8000"))
    if not 1 <= port <= 65535:
        raise SystemExit("LOCAL_WORKER_PORT must be between 1 and 65535.")
    def startup_expired():
        print("Model startup exceeded its time limit; cached downloads are retained.", flush=True)
        os._exit(78)
    startup_timer = threading.Timer(startup_seconds, startup_expired)
    startup_timer.daemon = True
    startup_timer.start()
    worker = gateway = server = None
    try:
        module, factory = os.environ["LOCAL_WORKER_FACTORY"].split(":", 1)
        worker = getattr(importlib.import_module(module), factory)()
        gateway = Gateway(worker, token, lock_path=os.environ.get("LOCAL_GPU_LOCK"), capacity=result_capacity)
        server = Server(("0.0.0.0", port), gateway)
        startup_timer.cancel()
        def stop(*_):
            threading.Thread(target=server.shutdown, daemon=True).start()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        print("Local model worker ready.", flush=True)
        server.serve_forever()
    finally:
        startup_timer.cancel()
        try:
            if gateway is not None:
                gateway.close()
            elif worker is not None and getattr(worker, "close", None):
                worker.close()
        finally:
            if server is not None:
                server.server_close()


if __name__ == "__main__":
    main()
