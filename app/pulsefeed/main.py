"""HTTP composition: authenticate, validate input, then call the service layer.

The application factory binds its database to one provider identity and owns one
coordinator lifecycle. Reader routes are owner-scoped; admin views expose only
aggregate usage unless the admin owns the requested reader page.
"""
import asyncio
from contextlib import asynccontextmanager
import hmac
import json
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from .auth import authenticate, exchange, client_ip, bootstrap_visitor, exchange_admin, revoke_stale_admin_sessions, ip_limit, login_challenge, verify_login_challenge, LOGIN_CHALLENGE_SECONDS
from .config import Config
from .db import Database
from .errors import AppError
from .provider import FakeRunpodAdapter, RunpodAdapter, LocalHTTPAdapter
from .coordinator import Coordinator
from .service import Service
from .rss import fetch_feed, demo_feed, public_url, source_hostname
from .assets import asset_url, static_cache_control, utc_timestamp, reader_date, source_origin, reader_created, reader_excerpt

ROOT = Path(__file__).parent


def create_app(config=None, provider=None, feed_fetcher=None):
    config = config or Config.from_env()
    config.validate()
    db = Database(config.database_path,config.global_daily_limit,config.feed_article_limit,config.global_hourly_generation_limit)
    # A production site can come online before its endpoints exist. Pausing must
    # neither bind empty endpoint IDs nor rewrite an existing provider identity.
    # Normal binding and credential validation still run before inference resumes.
    if config.inference_enabled:
        db.bind_provider(config.provider_profile(),synthetic=config.fake_mode)
    revoke_stale_admin_sessions(db,config)
    service = Service(db,config)
    provider = provider or (FakeRunpodAdapter() if config.fake_mode else LocalHTTPAdapter(config) if config.inference_provider=="local_http" else RunpodAdapter(config))
    coordinator = Coordinator(db,config,provider)
    fetcher = feed_fetcher or fetch_feed

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(coordinator.loop()) if config.start_coordinator and config.inference_enabled else None
        yield
        coordinator.stopped.set()
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await provider.close()

    app = FastAPI(title="PulseFeed",lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    app.state.db,app.state.service,app.state.coordinator,app.state.provider,app.state.config = db,service,coordinator,provider,config
    templates = Jinja2Templates(directory=str(ROOT / "templates"))
    templates.env.globals["source_hostname"] = source_hostname
    templates.env.globals["asset_url"] = asset_url
    templates.env.globals["utc_timestamp"] = utc_timestamp
    templates.env.globals["reader_date"] = reader_date
    templates.env.globals["source_origin"] = source_origin
    templates.env.globals["reader_created"] = reader_created
    templates.env.globals["reader_excerpt"] = reader_excerpt
    app.mount("/static",StaticFiles(directory=ROOT / "static",check_dir=False),name="static")

    @app.exception_handler(AppError)
    async def handle_error(request,exc):
        headers = {"Retry-After":str(exc.retry_after)} if exc.retry_after else {}
        return JSONResponse({"error":{"code":exc.code,"message":exc.message}},status_code=exc.status,headers=headers)

    @app.middleware("http")
    async def security_headers(request,call_next):
        # Block admission, retry and provider-backed image retrieval before any
        # fetch, reservation or transport. Disabling the coordinator alone would
        # still admit jobs, and image reads can also contact the provider.
        paused_route = (request.url.path == "/api/view-feed" or
                        (request.url.path.startswith("/api/runs/") and
                         request.url.path.endswith(("/image", "/retry"))))
        if not config.inference_enabled and paused_route:
            response = JSONResponse({"error":{"code":"INFERENCE_PAUSED","message":"Image generation is paused while the model endpoints are being connected. Please check back soon."}},status_code=503)
        else:
            response = await call_next(request)
        response.headers["Cache-Control"] = (static_cache_control(request.url.path,request.query_params.get("v"))
                                             if request.url.path.startswith("/static/") and response.status_code in (200,304)
                                             else "no-store")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin" if getattr(request.state,"admin_login_form",False) else "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        return response

    def session(request):
        if not hasattr(request.state,"session"):
            request.state.session = authenticate(db,config,request.cookies.get("pulsefeed_session"))
        return request.state.session

    def account(session):
        return {"id":session["account_id"],"name":session["name"],"role":session["role"]}

    def access_page(request, message, status=401, retry_after=None):
        response = templates.TemplateResponse(request=request,name="access.html",context={"message":message,"public_access":config.public_access_enabled,"server_now":time.time()},status_code=status)
        if retry_after:
            response.headers["Retry-After"] = str(retry_after)
        return response

    def session_cookie(response, raw, seconds):
        response.set_cookie("pulsefeed_session",raw,max_age=seconds,secure=config.cookie_secure,httponly=True,samesite="lax",path="/")
        return response

    def admin_login_page(request, message="", status=200, retry_after=None):
        # Native form POSTs need their same-origin Origin header; this clean
        # password form contains no URL credential. Every other response keeps
        # no-referrer, especially the compatible query-credential exchange.
        request.state.admin_login_form = True
        nonce, challenge = login_challenge(config)
        response = templates.TemplateResponse(request=request,name="admin_login.html",context={"login_csrf":nonce,"message":message,"login_enabled":bool(config.admin_access_token),"public_access":config.public_access_enabled,"server_now":time.time()},status_code=status)
        response.set_cookie("pulsefeed_admin_login",challenge,max_age=LOGIN_CHALLENGE_SECONDS,secure=config.cookie_secure,httponly=True,samesite="lax",path="/admin")
        if retry_after:
            response.headers["Retry-After"] = str(retry_after)
        return response

    def login_same_origin(request):
        def origin(value, allow_path=False):
            try:
                if not isinstance(value,str) or len(value)>2048 or any(ord(char)<=32 or ord(char)==127 or char=="\\" for char in value):
                    return None
                parsed = urlsplit(value)
                if parsed.scheme not in ("http","https") or not parsed.hostname or parsed.username is not None or parsed.password is not None:
                    return None
                if not allow_path and (parsed.path not in ("","/") or parsed.query or parsed.fragment):
                    return None
                return parsed.scheme, parsed.hostname.lower(), parsed.port if parsed.port is not None else (443 if parsed.scheme=="https" else 80)
            except (ValueError,TypeError):
                return None
        expected = origin(config.base_url)
        origins, referers = request.headers.getlist("origin"), request.headers.getlist("referer")
        return (request.headers.get("sec-fetch-site") not in ("cross-site",)
                and len(origins)<=1 and len(referers)<=1
                and (not origins or origin(origins[0])==expected)
                and (not referers or origin(referers[0],allow_path=True)==expected))

    def expired_page(request, s=None, admin_mode=False):
        return templates.TemplateResponse(request=request,name="expired.html",context={"account":account(s) if s else None,"csrf":s["csrf"] if s else None,"admin_mode":admin_mode,"server_now":time.time(),"public_access":config.public_access_enabled},status_code=404)

    async def json_body(request):
        content = bytearray()
        async for chunk in request.stream():
            content.extend(chunk)
            if len(content) > 65536:
                raise AppError("INPUT_TOO_LARGE","Request body is too large.",413)
        try:
            value = json.loads(content)
        except (ValueError,UnicodeError,RecursionError) as exc:
            raise AppError("INVALID_INPUT","Provide a valid JSON object.") from exc
        if not isinstance(value,dict):
            raise AppError("INVALID_INPUT","Provide a JSON object.")
        return value

    def csrf(request):
        value = request.headers.get("x-csrf-token","")
        if not hmac.compare_digest(value.encode(),session(request)["csrf"].encode()):
            raise AppError("CSRF_FAILED","The request could not be verified. Refresh and try again.",403)

    async def fetched(url):
        if config.fake_mode and url == "demo":
            return demo_feed()
        return await fetcher(url)

    @app.get("/health")
    async def health():
        db.one("SELECT 1 AS ready")
        return {"status":"ok","mode":"generation-paused" if not config.inference_enabled else "synthetic-demo" if config.fake_mode else "live"}

    @app.get("/access")
    async def access(request:Request,token:str|None=None):
        if token is None:
            return access_page(request,"Return home to open an RSS feed." if config.public_access_enabled else "Open your issued access link to begin.",200)
        try:
            if len(token) > 200:
                raise AppError("ACCESS_EXPIRED","Invalid access link.",401)
            raw = exchange(db,config,token,client_ip(request,config))
        except AppError as exc:
            return access_page(request,exc.message,exc.status,exc.retry_after)
        response = RedirectResponse("/",status_code=303)
        return session_cookie(response,raw,config.session_seconds)

    def reader_page(request, view_id=None, opening=False):
        new_raw = None
        try:
            s = session(request)
        except AppError as exc:
            if opening:
                return access_page(request,"Return home to open an RSS feed.",401)
            if config.public_access_enabled and (view_id is not None or "feed" in request.query_params):
                return expired_page(request)
            if config.public_access_enabled and view_id is None and "admin" not in request.query_params:
                try:
                    new_raw = bootstrap_visitor(db,config,client_ip(request,config))
                    s = authenticate(db,config,new_raw)
                    request.state.session = s
                except AppError as denied:
                    return access_page(request,denied.message,denied.status,denied.retry_after)
            else:
                return access_page(request,"Administrator access is required. Return home to read feeds." if "admin" in request.query_params else exc.message,401)
        admin_mode = "admin" in request.query_params and s["role"] == "admin"
        if "admin" in request.query_params and not admin_mode:
            return access_page(request,"Administrator access is required. Return home to read feeds.",403)
        context = {"admin_mode":admin_mode,"account":account(s),"csrf":s["csrf"],"fake_mode":config.fake_mode,"inference_enabled":config.inference_enabled,"initial_feed":None,"view":None,"pending_feed":opening,"server_now":time.time(),"settings":db.settings() if admin_mode else None,"public_access":config.public_access_enabled}
        try:
            if "feed" in request.query_params:
                service.unavailable_view()
            if view_id is not None:
                context["initial_feed"] = service.reader_view(s["account_id"],view_id)
                context["view"] = context["initial_feed"]["view"]
                context["server_now"] = context["initial_feed"]["server_now"]
        except AppError as exc:
            if exc.status != 404:
                raise
            return expired_page(request,s,admin_mode)
        response = templates.TemplateResponse(request=request,name="index.html",context=context)
        return session_cookie(response,new_raw,config.session_seconds) if new_raw else response

    @app.get("/")
    async def index(request:Request):
        if config.admin_access_param in request.query_params:
            try:
                raw = exchange_admin(db,config,request.query_params.getlist(config.admin_access_param),client_ip(request,config),request.cookies.get("pulsefeed_session"))
            except AppError as exc:
                # Even invalid credentials leave the token-bearing URL immediately.
                response = RedirectResponse("/admin",status_code=303)
                if exc.retry_after:
                    response.headers["Retry-After"] = str(exc.retry_after)
                return response
            return session_cookie(RedirectResponse("/admin",status_code=303),raw,config.admin_session_seconds)
        return reader_page(request)

    @app.get("/feeds/new")
    async def opening_feed(request:Request):
        # This shell performs no fetch or admission. The browser transfers one
        # explicit form submission and posts it only after rendering this page.
        return reader_page(request,opening=True)

    @app.get("/feeds/{view_id}")
    async def feed_landing(view_id:str,request:Request):
        return reader_page(request,view_id)

    @app.post("/logout")
    async def logout(request:Request):
        s = session(request)
        if request.headers.get("x-csrf-token"):
            csrf(request)
        else:
            content = bytearray()
            async for chunk in request.stream():
                content.extend(chunk)
                if len(content) > 2048:
                    raise AppError("INPUT_TOO_LARGE","Request body is too large.",413)
            try:
                value = parse_qs(content.decode()).get("csrf",[""])[0]
            except (ValueError, UnicodeError) as exc:
                raise AppError("CSRF_FAILED","The request could not be verified.",403) from exc
            if not hmac.compare_digest(value.encode(),s["csrf"].encode()):
                raise AppError("CSRF_FAILED","The request could not be verified.",403)
        db.execute("UPDATE sessions SET revoked=1 WHERE hash=?",(s["hash"],))
        response = RedirectResponse("/admin" if s["role"]=="admin" else "/access",status_code=303)
        response.delete_cookie("pulsefeed_session",path="/",secure=config.cookie_secure,httponly=True,samesite="lax")
        return response

    @app.get("/api/session")
    async def current_session(request:Request):
        s = session(request)
        return {"account":account(s),"csrf":s["csrf"],"fake_mode":config.fake_mode}

    @app.post("/api/view-feed")
    async def view_feed(request:Request):
        s = session(request); csrf(request)
        body = await json_body(request)
        if set(body) != {"url","idempotency_key"} or not isinstance(body.get("url"),str):
            raise AppError("INVALID_INPUT","Provide a feed URL and an idempotency key.")
        url,key = body["url"].strip(),body["idempotency_key"]
        service.validate_key(key)
        if not (config.fake_mode and url == "demo"):
            public_url(url)
        cached = service.cached_view(s["account_id"],key,url)
        if cached:
            return cached
        service.require_view_capacity(s["account_id"])
        if config.public_access_enabled:
            ip_limit(db,client_ip(request,config),"public_rss",config.rss_limit)
        service.limit(s["account_id"],"rss")
        return service.view_feed(s["account_id"],url,key,await fetched(url))

    @app.api_route("/api/feeds",methods=["GET","POST"])
    @app.api_route("/api/feeds/{feed_id}/items",methods=["GET"])
    @app.api_route("/api/feeds/{feed_id}/refresh",methods=["POST"])
    @app.api_route("/api/batches",methods=["POST"])
    @app.api_route("/api/runs",methods=["GET"])
    @app.api_route("/fragments/history",methods=["GET"])
    async def retired_reader(request:Request):
        session(request)
        service.unavailable_view()

    @app.get("/api/batches/{batch_id}")
    async def batch_status(batch_id:str,request:Request):
        s = session(request)
        # Charge authenticated polls before constructing historical run payloads.
        service.limit(s["account_id"],"poll")
        return service.view_batch(s["account_id"],request.query_params.get("view_id"),batch_id)

    @app.get("/api/runs/{run_id}/image")
    async def image(run_id:str,request:Request):
        s = session(request)
        view_id = request.query_params.get("view_id")
        service.require_view(s["account_id"],view_id,run_id=run_id)
        service.limit(s["account_id"],"poll")
        try:
            raw,mime = await coordinator.retrieve(s["account_id"],run_id)
        except AppError:
            service.require_view(s["account_id"],view_id,run_id=run_id)
            raise
        service.require_view(s["account_id"],view_id,run_id=run_id)
        return Response(raw,media_type=mime,headers={"Content-Disposition":f'inline; filename="pulsefeed-{run_id}.{ "webp" if mime == "image/webp" else "jpg" }"'})

    @app.post("/api/runs/{run_id}/retry")
    async def retry(run_id:str,request:Request):
        s = session(request)
        view_id = request.query_params.get("view_id")
        service.require_view(s["account_id"],view_id,run_id=run_id)
        csrf(request)
        body = await json_body(request)
        if set(body) != {"idempotency_key"}:
            raise AppError("INVALID_INPUT","Provide an idempotency key.")
        return service.retry_view(s["account_id"],view_id,run_id,body["idempotency_key"])

    def require_admin(request):
        s = session(request)
        if s["role"] != "admin":
            raise AppError("FORBIDDEN","Only an administrator can access usage and limits.",403)
        return s

    def usage_report(window):
        if window not in ("today","all"):
            raise AppError("INVALID_WINDOW","Choose today or all recorded usage.")
        from .usage import aggregate_usage
        return dict(aggregate_usage(db,window),quotas=service.quota_summary())

    @app.get("/admin")
    async def admin_page(request:Request):
        try:
            s = require_admin(request)
        except AppError:
            return admin_login_page(request)
        service.limit(s["account_id"],"poll")
        period = request.query_params.get("window",request.query_params.get("period","today"))
        report = usage_report(period)
        return templates.TemplateResponse(request=request,name="admin.html",context={"account":account(s),"csrf":s["csrf"],"fake_mode":config.fake_mode,"period":period,"usage":report,"quotas":report["quotas"],"settings":db.settings(),"public_access":config.public_access_enabled,"server_now":time.time()})

    @app.post("/admin/login")
    async def admin_login(request:Request):
        try:
            # Commit even malformed or cross-site attempts before credential work.
            ip_limit(db,client_ip(request,config),"admin_exchange",config.exchange_limit)
            if not login_same_origin(request) or request.headers.get("content-type","").split(";",1)[0].strip().lower()!="application/x-www-form-urlencoded":
                raise AppError("CSRF_FAILED","The sign-in form could not be verified. Please try again.",403)
            content = bytearray()
            async for chunk in request.stream():
                content.extend(chunk)
                if len(content)>2048:
                    raise AppError("INPUT_TOO_LARGE","The sign-in form is too large.",413)
            try:
                body = parse_qs(content.decode("utf-8"),keep_blank_values=True,strict_parsing=True,encoding="utf-8",errors="strict",max_num_fields=4)
            except (ValueError,UnicodeError) as exc:
                raise AppError("CSRF_FAILED","The sign-in form could not be verified. Please try again.",403) from exc
            if set(body)!={"password","login_csrf"} or any(len(values)!=1 for values in body.values()) or not verify_login_challenge(config,request.cookies.get("pulsefeed_admin_login"),body["login_csrf"][0]):
                raise AppError("CSRF_FAILED","The sign-in form could not be verified. Please try again.",403)
            raw = exchange_admin(db,config,body["password"],client_ip(request,config),request.cookies.get("pulsefeed_session"),rate_checked=True)
        except AppError as exc:
            message = "That password didn’t work. Please try again." if exc.code=="ADMIN_ACCESS_DENIED" else "Too many sign-in attempts. Please try again shortly." if exc.status==429 else "The sign-in form could not be verified. Please try again."
            return admin_login_page(request,message,exc.status,exc.retry_after)
        response = session_cookie(RedirectResponse("/admin",status_code=303),raw,config.admin_session_seconds)
        response.delete_cookie("pulsefeed_admin_login",path="/admin",secure=config.cookie_secure,httponly=True,samesite="lax")
        return response

    @app.get("/api/admin/usage")
    async def admin_usage(request:Request,window:str="today"):
        s = require_admin(request)
        service.limit(s["account_id"],"poll")
        return usage_report(window)

    @app.post("/api/admin/limits")
    async def admin_limits(request:Request):
        s = require_admin(request); csrf(request)
        service.admin_limit(s["account_id"])
        return {"quotas":service.set_limits(s["account_id"],await json_body(request))}

    @app.post("/api/admin/limits/reset")
    async def admin_reset(request:Request):
        s = require_admin(request); csrf(request)
        service.admin_limit(s["account_id"])
        return {"quotas":service.reset_limits(s["account_id"],await json_body(request))}

    @app.post("/api/settings")
    async def settings(request:Request):
        s = session(request); csrf(request)
        if s["role"] != "admin":
            raise AppError("FORBIDDEN","Only an admin can change settings.",403)
        service.admin_limit(s["account_id"])
        return service.save_settings(await json_body(request))

    @app.post("/api/admin/settings/{section}")
    async def section_settings(section:str,request:Request):
        s = require_admin(request); csrf(request)
        service.admin_limit(s["account_id"])
        return service.save_settings(await json_body(request),section)

    return app
