"""Validate operator settings before opening storage or contacting a provider.

Secrets never participate in provider identity: rotating a key should not reroute
persisted jobs, while changing endpoint IDs must require a separate database.
"""
from dataclasses import dataclass, field
import hashlib
import ipaddress
import json
import os
import re
from urllib.parse import urlsplit


def loopback_url(value):
    """Validate static operator configuration without DNS or network access."""
    try:
        url = urlsplit(value)
        host = url.hostname
        port = url.port
        if url.scheme not in ("http","https") or not host or url.username is not None or url.password is not None or url.query or url.fragment:
            return False
        if url.path not in ("","/"):
            return False
        return (port is None or 1<=port<=65535) and (host.lower()=="localhost" or ipaddress.ip_address(host).is_loopback)
    except (ValueError,TypeError):
        return False


@dataclass
class Config:
    database_path: str = "/data/pulsefeed.db"
    base_url: str = "http://localhost:8000"
    session_secret: str = field(default="",repr=False)
    public_access_enabled: bool = True
    visitor_limit: int = 20
    admin_access_param: str = "admin_key"
    admin_access_token: str = field(default="",repr=False)
    admin_session_seconds: int = 3600
    fake_mode: bool = False
    inference_enabled: bool = True
    cookie_secure: bool = True
    session_seconds: int = 86400
    link_seconds: int = 1800
    coordinator_interval: float = 2.0
    coordinator_stage_concurrency: int = 1
    coordinator_pipeline_size: int = 2
    deadline_seconds: int = 600
    image_quota: int = 30
    global_daily_limit: int = 100
    global_hourly_generation_limit: int = 30
    flux_image_size: int = 512
    feed_article_limit: int = 10
    admin_write_limit: int = 10
    live_view_limit: int = 5
    exchange_limit: int = 10
    rss_limit: int = 10
    submit_limit: int = 5
    poll_limit: int = 120
    trusted_proxies: tuple[str, ...] = ()
    runpod_key: str = field(default="",repr=False)
    qwen_endpoint: str = ""
    flux_endpoint: str = ""
    inference_provider: str = "runpod"
    local_qwen_url: str = "http://127.0.0.1:8181"
    local_flux_url: str = "http://127.0.0.1:8182"
    local_worker_token: str = field(default="",repr=False)
    start_coordinator: bool = True

    @classmethod
    def from_env(cls):
        public = os.getenv("PUBLIC_ACCESS_ENABLED", "true").lower()
        if public not in ("true","false"):
            raise ValueError("PUBLIC_ACCESS_ENABLED must be true or false")
        inference = os.getenv("INFERENCE_ENABLED", "true").lower()
        if inference not in ("true", "false"):
            raise ValueError("INFERENCE_ENABLED must be true or false")
        config = cls(
            database_path=os.getenv("DATABASE_PATH", "/data/pulsefeed.db"),
            base_url=os.getenv("APP_BASE_URL", "http://localhost:8000").rstrip("/"),
            session_secret=os.getenv("SESSION_SECRET", ""),
            public_access_enabled=public=="true",
            admin_access_param=os.getenv("ADMIN_ACCESS_PARAM", "admin_key"),
            admin_access_token=os.getenv("ADMIN_ACCESS_TOKEN", ""),
            admin_session_seconds=int(os.getenv("ADMIN_SESSION_SECONDS", "3600")),
            visitor_limit=int(os.getenv("VISITOR_LIMIT_PER_MINUTE", "20")),
            live_view_limit=int(os.getenv("LIVE_VIEW_LIMIT",os.getenv("SAVED_FEED_LIMIT","5"))),
            fake_mode=os.getenv("PULSEFEED_FAKE_MODE", "false").lower() == "true",
            inference_enabled=inference == "true",
            cookie_secure=os.getenv("COOKIE_SECURE", "true").lower() == "true",
            trusted_proxies=tuple(filter(None, os.getenv("TRUSTED_PROXY_CIDRS", "").split(","))),
            runpod_key=os.getenv("RUNPOD_API_KEY", ""),
            qwen_endpoint=os.getenv("RUNPOD_QWEN_ENDPOINT_ID", ""),
            flux_endpoint=os.getenv("RUNPOD_FLUX_ENDPOINT_ID", ""),
            inference_provider=os.getenv("INFERENCE_PROVIDER", "runpod"),
            local_qwen_url=os.getenv("LOCAL_QWEN_URL", "http://127.0.0.1:8181").rstrip("/"),
            local_flux_url=os.getenv("LOCAL_FLUX_URL", "http://127.0.0.1:8182").rstrip("/"),
            local_worker_token=os.getenv("LOCAL_WORKER_TOKEN", ""),
        )
        for name, env in [("session_seconds", "SESSION_SECONDS"), ("link_seconds", "ACCESS_LINK_SECONDS"),
                          ("deadline_seconds", "ITEM_DEADLINE_SECONDS"), ("image_quota", "IMAGE_QUOTA_PER_HOUR"),
                          ("exchange_limit", "EXCHANGE_LIMIT_PER_MINUTE"),
                          ("rss_limit", "RSS_LIMIT_PER_MINUTE"), ("submit_limit", "SUBMIT_LIMIT_PER_MINUTE"),
                          ("poll_limit", "POLL_LIMIT_PER_MINUTE"), ("global_daily_limit", "GLOBAL_DAILY_IMAGE_LIMIT"),
                          ("feed_article_limit", "FEED_ARTICLE_LIMIT"), ("admin_write_limit", "ADMIN_WRITE_LIMIT_PER_MINUTE")]:
            setattr(config, name, int(os.getenv(env, str(getattr(config, name)))))
        config.flux_image_size = int(os.getenv("FLUX_IMAGE_SIZE", "512"))
        config.coordinator_stage_concurrency = int(os.getenv("COORDINATOR_STAGE_CONCURRENCY", "1"))
        config.coordinator_pipeline_size = int(os.getenv(
            "COORDINATOR_PIPELINE_SIZE", str(2*config.coordinator_stage_concurrency)))
        config.global_hourly_generation_limit = int(os.getenv("GLOBAL_HOURLY_GENERATION_LIMIT", "30"))
        config.validate()
        return config

    def validate(self):
        if len(self.session_secret) < 32:
            raise ValueError("SESSION_SECRET must contain at least 32 characters")
        if type(self.public_access_enabled) is not bool:
            raise ValueError("PUBLIC_ACCESS_ENABLED must be true or false")
        if type(self.inference_enabled) is not bool:
            raise ValueError("INFERENCE_ENABLED must be true or false")
        reserved = {"admin","feed","token","window","period","v","view_id","csrf","url","idempotency_key","session","access","next","return"}
        if not isinstance(self.admin_access_param,str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}",self.admin_access_param,flags=re.ASCII) or self.admin_access_param.lower() in reserved:
            raise ValueError("ADMIN_ACCESS_PARAM must be a non-reserved ASCII identifier of 1 to 64 characters")
        if not isinstance(self.admin_access_token,str) or (self.admin_access_token and (not re.fullmatch(r"[A-Za-z0-9_-]{32,256}",self.admin_access_token,flags=re.ASCII) or len(set(self.admin_access_token))<8 or self.admin_access_token==self.session_secret)):
            raise ValueError("ADMIN_ACCESS_TOKEN must be a distinct, randomly generated URL-safe ASCII secret of 32 to 256 characters")
        if type(self.admin_session_seconds) is not int or not 1<=self.admin_session_seconds<=86400:
            raise ValueError("ADMIN_SESSION_SECONDS must be an integer from 1 to 86400")
        if type(self.visitor_limit) is not int or not 1<=self.visitor_limit<=10000:
            raise ValueError("VISITOR_LIMIT_PER_MINUTE must be a positive bounded integer")
        if type(self.live_view_limit) is not int or not 1<=self.live_view_limit<=100:
            raise ValueError("LIVE_VIEW_LIMIT must be an integer from 1 to 100 concurrent unexpired feed pages")
        try:
            origin = urlsplit(self.base_url)
            valid_origin = (isinstance(self.base_url,str) and len(self.base_url)<=2048 and not any(ord(c)<=32 or ord(c)==127 for c in self.base_url)
                            and "\\" not in self.base_url and origin.scheme in ("http","https") and origin.hostname and origin.username is None and origin.password is None
                            and origin.path in ("","/") and not origin.query and not origin.fragment and (origin.port is None or 1<=origin.port<=65535))
        except (ValueError,TypeError):
            valid_origin = False
        if not valid_origin or (not loopback_url(self.base_url) and origin.scheme!="https"):
            raise ValueError("APP_BASE_URL must be an HTTPS origin, or an HTTP(S) loopback development origin")
        if not loopback_url(self.base_url) and not self.cookie_secure:
            raise ValueError("COOKIE_SECURE must be true for a production origin")
        if type(self.coordinator_stage_concurrency) is not int or not 1 <= self.coordinator_stage_concurrency <= 3:
            raise ValueError("COORDINATOR_STAGE_CONCURRENCY must be an integer from 1 to 3")
        if (type(self.coordinator_pipeline_size) is not int
                or not 1 <= self.coordinator_pipeline_size <= 2*self.coordinator_stage_concurrency):
            raise ValueError("COORDINATOR_PIPELINE_SIZE must be between 1 and twice COORDINATOR_STAGE_CONCURRENCY")
        if type(self.global_hourly_generation_limit) is not int or not 1 <= self.global_hourly_generation_limit <= 10000:
            raise ValueError("GLOBAL_HOURLY_GENERATION_LIMIT must be an integer from 1 to 10000")
        if type(self.flux_image_size) is not int or self.flux_image_size not in (256,512,1024):
            raise ValueError("FLUX_IMAGE_SIZE must be 256, 512 or 1024")
        if type(self.feed_article_limit) is not int or not 1<=self.feed_article_limit<=10:
            raise ValueError("FEED_ARTICLE_LIMIT must be an integer from 1 to 10")
        if self.inference_provider not in ("runpod","local_http"):
            raise ValueError("INFERENCE_PROVIDER must be runpod or local_http")
        if self.inference_enabled and not self.fake_mode and self.inference_provider=="runpod" and not all((self.runpod_key, self.qwen_endpoint, self.flux_endpoint)):
            raise ValueError("Configure all Runpod credentials or explicitly enable PULSEFEED_FAKE_MODE")
        if self.inference_enabled and not self.fake_mode and self.inference_provider=="local_http":
            if not all(loopback_url(url) for url in (self.local_qwen_url,self.local_flux_url)):
                raise ValueError("LOCAL_QWEN_URL and LOCAL_FLUX_URL must be loopback HTTP(S) origins")
            if len(self.local_worker_token)<32 or any(ord(char)<33 or ord(char)>126 for char in self.local_worker_token):
                raise ValueError("LOCAL_WORKER_TOKEN must contain at least 32 printable non-space ASCII characters")
        if not self.cookie_secure and not ((self.fake_mode or self.inference_provider=="local_http") and loopback_url(self.base_url)):
            raise ValueError("COOKIE_SECURE=false requires explicit fake/local_http mode and a loopback APP_BASE_URL")
        for value in (self.session_seconds, self.link_seconds, self.deadline_seconds, self.image_quota,
                      self.exchange_limit, self.rss_limit, self.submit_limit, self.poll_limit, self.admin_write_limit):
            if value <= 0:
                raise ValueError("Limits and lifetimes must be positive")

    def provider_profile(self):
        if self.fake_mode:
            identity = {"provider":"synthetic"}
        elif self.inference_provider=="local_http":
            identity = {"provider":"local_http","qwen":self.local_qwen_url.rstrip("/"),"flux":self.local_flux_url.rstrip("/")}
        else:
            identity = {"provider":"runpod","qwen":self.qwen_endpoint,"flux":self.flux_endpoint}
        # Credentials are deliberately absent: rotation never changes job routing.
        return hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(",",":")).encode()).hexdigest()
