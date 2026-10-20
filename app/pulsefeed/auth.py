"""Visitor sessions, session-scoped admin capabilities, and admission throttles.

Only keyed token digests are stored. Admin access is tied to the current secret
fingerprint, so rotating that secret revokes capabilities without trusting an old
account role. Charge failed login attempts in a committed transaction too.
"""
import hashlib
import hmac
import ipaddress
import re
import secrets
import time
from .db import new_id
from .errors import AppError


def token_hash(token, secret):
    return hmac.new(secret.encode(), token.encode(), hashlib.sha256).hexdigest()


def admin_fingerprint(config):
    if not config.admin_access_token:
        return None
    return token_hash("admin-capability\0"+config.admin_access_param+"\0"+config.admin_access_token,config.session_secret)


def runtime_admin_policy(config):
    return config.public_access_enabled or bool(config.admin_access_token)


def revoke_stale_admin_sessions(db, config):
    current = admin_fingerprint(config)
    with db.transaction() as conn:
        for row in conn.execute("SELECT hash,admin_capability FROM sessions WHERE revoked=0 AND admin_capability IS NOT NULL").fetchall():
            if current is None or not hmac.compare_digest(row["admin_capability"].encode(),current.encode()):
                conn.execute("UPDATE sessions SET revoked=1 WHERE hash=?",(row["hash"],))


def ip_limit(db, ip, action, limit):
    # This transaction commits before credential validation, fetching or minting.
    with db.transaction() as conn:
        rate_limit(conn,ip,action,limit)


def _visitor(conn):
    owner = new_id()
    conn.execute("INSERT INTO accounts VALUES(?,?,?,?)",(owner,"visitor-"+owner,"evaluator",time.time()))
    return owner


def _session(conn, config, owner, seconds, capability=None):
    raw = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO sessions(hash,account_id,csrf,expires,admin_capability) VALUES(?,?,?,?,?)",(token_hash(raw,config.session_secret),owner,secrets.token_urlsafe(32),time.time()+seconds,capability))
    return raw


def bootstrap_visitor(db, config, ip):
    ip_limit(db,ip,"visitor_bootstrap",config.visitor_limit)
    with db.transaction() as conn:
        return _session(conn,config,_visitor(conn),config.session_seconds)


def exchange_admin(db, config, values, ip, old_raw=None, *, rate_checked=False):
    if not rate_checked:
        ip_limit(db,ip,"admin_exchange",config.exchange_limit)
    valid = len(values)==1 and isinstance(values[0],str) and re.fullmatch(r"[A-Za-z0-9_-]{32,256}",values[0],flags=re.ASCII)
    if not config.admin_access_token or not valid or not hmac.compare_digest(values[0].encode(),config.admin_access_token.encode()):
        raise AppError("ADMIN_ACCESS_DENIED","Administrator access is required. Return home to read feeds.",401)
    try:
        old = authenticate(db,config,old_raw)
    except AppError:
        old = None
    if old is None:
        ip_limit(db,ip,"visitor_bootstrap",config.visitor_limit)
    with db.transaction() as conn:
        owner = old["account_id"] if old else _visitor(conn)
        if old:
            conn.execute("UPDATE sessions SET revoked=1 WHERE hash=?",(old["hash"],))
        return _session(conn,config,owner,config.admin_session_seconds,admin_fingerprint(config))


LOGIN_CHALLENGE_SECONDS = 600


def login_challenge(config, now=None):
    nonce = secrets.token_urlsafe(32)
    expires = str(int(time.time() if now is None else now)+LOGIN_CHALLENGE_SECONDS)
    signature = token_hash("admin-login\0"+nonce+"\0"+expires+"\0"+(admin_fingerprint(config) or ""),config.session_secret)
    return nonce, nonce+"."+expires+"."+signature


def verify_login_challenge(config, cookie, nonce, now=None):
    now = time.time() if now is None else now
    if not isinstance(cookie,str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}\.[0-9]{1,13}\.[a-f0-9]{64}",cookie,flags=re.ASCII):
        return False
    if not isinstance(nonce,str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}",nonce,flags=re.ASCII):
        return False
    stored, expires, signature = cookie.split(".")
    if not now < int(expires) <= now+LOGIN_CHALLENGE_SECONDS:
        return False
    expected = token_hash("admin-login\0"+stored+"\0"+expires+"\0"+(admin_fingerprint(config) or ""),config.session_secret)
    return hmac.compare_digest(nonce.encode(),stored.encode()) and hmac.compare_digest(signature.encode(),expected.encode())


def rate_limit(conn, scope, action, limit, now=None):
    now = time.time() if now is None else now
    conn.execute("DELETE FROM rate_events WHERE created<?", (now - 3600,))
    rows = conn.execute("SELECT created FROM rate_events WHERE scope=? AND action=? AND created>? ORDER BY created", (scope, action, now - 60)).fetchall()
    if len(rows) >= limit:
        raise AppError("RATE_LIMITED", "Too many requests. Try again shortly.", 429, max(1, int(rows[0][0] + 61 - now)))
    conn.execute("INSERT INTO rate_events(scope,action,created) VALUES(?,?,?)", (scope, action, now))


def seed_account(db, name, role):
    if role not in ("evaluator", "admin") or not name.strip():
        raise ValueError("Choose a nonempty name and evaluator/admin role")
    with db.transaction() as conn:
        existing = conn.execute("SELECT * FROM accounts WHERE name=?", (name,)).fetchone()
        if existing:
            if existing["role"] != role:
                raise ValueError("Account already exists with another role")
            return existing["id"]
        account_id = new_id()
        conn.execute("INSERT INTO accounts VALUES(?,?,?,?)", (account_id, name, role, time.time()))
        return account_id


def issue_link(db, config, account_id):
    token = secrets.token_urlsafe(32)
    with db.transaction() as conn:
        if not conn.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone():
            raise ValueError("Unknown account")
        conn.execute("INSERT INTO access_tokens(hash,account_id,expires) VALUES(?,?,?)", (token_hash(token, config.session_secret), account_id, time.time() + config.link_seconds))
    return config.base_url + "/access?token=" + token


def exchange(db, config, token, ip):
    # Commit the rate event even for invalid credentials.
    with db.transaction() as conn:
        rate_limit(conn, ip, "exchange", config.exchange_limit)
    now = time.time()
    with db.transaction() as conn:
        row = conn.execute("SELECT t.*,a.role FROM access_tokens t JOIN accounts a ON a.id=t.account_id WHERE t.hash=?", (token_hash(token, config.session_secret),)).fetchone()
        if not row or row["revoked"] or row["used"] or row["expires"] <= now:
            raise AppError("ACCESS_EXPIRED", "This access link has expired, was revoked, or was already used.", 401)
        if row["role"]=="admin" and runtime_admin_policy(config):
            raise AppError("ADMIN_ACCESS_DENIED","Administrator access is required. Return home to read feeds.",403)
        conn.execute("UPDATE access_tokens SET used=? WHERE hash=?", (now, row["hash"]))
        raw = secrets.token_urlsafe(32)
        conn.execute("INSERT INTO sessions(hash,account_id,csrf,expires) VALUES(?,?,?,?)", (token_hash(raw, config.session_secret), row["account_id"], secrets.token_urlsafe(32), now + config.session_seconds))
        return raw


def authenticate(db, config, raw):
    if not isinstance(raw,str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}",raw,flags=re.ASCII):
        raise AppError("ACCESS_EXPIRED", "Your session has expired. Open a new access link.", 401)
    session = db.one("SELECT s.*,a.name,a.role FROM sessions s JOIN accounts a ON a.id=s.account_id WHERE s.hash=? AND s.revoked=0 AND s.expires>?", (token_hash(raw, config.session_secret), time.time()))
    if not session:
        raise AppError("ACCESS_EXPIRED", "Your session has expired. Open a new access link.", 401)
    if session["admin_capability"] is not None:
        current = admin_fingerprint(config)
        if current is None or not hmac.compare_digest(session["admin_capability"].encode(),current.encode()):
            db.execute("UPDATE sessions SET revoked=1 WHERE hash=?",(session["hash"],))
            raise AppError("ACCESS_EXPIRED","Your session has expired. Return home to continue.",401)
        session["role"] = "admin"
    elif runtime_admin_policy(config):
        # Environment-granted admin access belongs to this session only. An
        # account's historical role cannot elevate another visitor session or
        # bypass revocation when the bearer credential is rotated/removed.
        session["role"] = "evaluator"
    return session


def client_ip(request, config):
    direct = request.client.host if request.client else "unknown"
    try:
        trusted = any(ipaddress.ip_address(direct) in ipaddress.ip_network(cidr.strip()) for cidr in config.trusted_proxies)
        # Railway's forwarded chain must be configured for the actual trusted proxy hops.
        if trusted:
            chain = [x.strip() for x in request.headers.get("x-forwarded-for", "").split(",") if x.strip()]
            for address in reversed(chain):
                parsed = ipaddress.ip_address(address)
                if not any(parsed in ipaddress.ip_network(cidr.strip()) for cidr in config.trusted_proxies):
                    return str(parsed)
    except ValueError:
        pass
    return direct
