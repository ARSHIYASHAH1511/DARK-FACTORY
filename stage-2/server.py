"""Pocketful stage 2: payments, requests, splits, settlements, authorizations + web UI (stdlib only, in-memory)."""
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

MAX_AMOUNT = 1_000_000_000
MAX_BALANCE = 2 ** 53
HANDLE_RE = re.compile(r"^[a-z0-9_]{1,20}$")
DIGITS_RE = re.compile(r"[0-9]+", re.ASCII)
STATUSES = ("pending", "paid", "declined", "cancelled")
VISIBILITIES = ("public", "private")
AUTH_STATUSES = ("open", "captured", "voided", "expired")
DEFAULT_TTL = 600
STATIC_DIR = os.environ.get("STATIC_DIR", os.path.join(os.path.dirname(os.path.abspath(__file__)), "static"))
SCRYPT_N = 4096
MAX_BODY = 256 * 1024 * 1024

# One global lock serialises every request, so each check-then-write step is atomic.
LOCK = threading.RLock()
S = None      # state: JSON-serialisable dict, replaced atomically
IDX = None    # derived indexes (not exported)


class ApiError(Exception):
    def __init__(self, status, code, message=""):
        super().__init__(code)
        self.status, self.code, self.message = status, code, message or code


def bad(code="validation_failed", msg=""):
    return ApiError(422, code, msg)


def malformed(msg="malformed request"):
    return ApiError(400, "malformed_request", msg)


# ---------------------------------------------------------------- helpers

def now():
    t = time.time_ns() // 1000
    return t, fmt_ts(t)


def fmt_ts(micro):
    return datetime.fromtimestamp(micro / 1_000_000, timezone.utc).isoformat(timespec="seconds")


def parse_json(raw):
    def refuse(c):
        raise ValueError(c)
    try:
        return json.loads(raw.decode("utf-8"), parse_constant=refuse)
    except Exception:
        raise malformed("body is not valid JSON")


def canon(v):
    def norm(x):
        if isinstance(x, float) and math.isfinite(x) and x.is_integer():
            return int(x)
        if isinstance(x, dict):
            return {k: norm(y) for k, y in x.items()}
        if isinstance(x, list):
            return [norm(y) for y in x]
        return x
    return json.dumps(norm(v), sort_keys=True, separators=(",", ":"))


def to_int(v):
    """Integral JSON number -> int, else None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float) and math.isfinite(v) and v.is_integer():
        return int(v)
    return None


def parse_amount(v):
    a = to_int(v)
    if a is None or a < 1 or a > MAX_AMOUNT:
        raise bad(msg="amount must be an integer between 1 and 1000000000")
    return a


def parse_note(body):
    if "note" not in body:
        return ""
    n = body["note"]
    if not isinstance(n, str) or len(n) > 200:
        raise bad(msg="note must be a string of at most 200 characters")
    return n


def parse_visibility(body):
    if "visibility" not in body:
        return "public"
    v = body["visibility"]
    if not isinstance(v, str) or v not in VISIBILITIES:
        raise bad(msg="visibility must be public or private")
    return v


def handle_field(body, name):
    if name not in body:
        raise bad(msg=name + " is required")
    v = body[name]
    if not isinstance(v, str):
        raise malformed(name + " must be a string")
    return v


def hash_pw(pw):
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode("utf-8", "surrogatepass"), salt=salt, n=SCRYPT_N, r=8, p=1, dklen=32)
    return {"algo": "scrypt", "n": SCRYPT_N, "r": 8, "p": 1, "salt": salt.hex(), "hash": h.hex()}


def verify_pw(pw, rec):
    h = hashlib.scrypt(pw.encode("utf-8", "surrogatepass"), salt=bytes.fromhex(rec["salt"]),
                       n=rec["n"], r=rec["r"], p=rec["p"], dklen=32)
    return hmac.compare_digest(h.hex(), rec["hash"])


def derive_handle(email):
    local = email.split("@", 1)[0].lower()
    return re.sub(r"[^a-z0-9_]", "_", local)[:20]


def build_idx(state):
    return {
        "email": {u["email"].lower(): uid for uid, u in state["users"].items()},
        "handle": {u["handle"]: uid for uid, u in state["users"].items()},
        "operators": set(state["operators"]),
        "open": {i for i, a in state["authorizations"].items() if a["status"] == "open"},
    }


def install(state):
    global S, IDX
    idx = build_idx(state)
    with LOCK:
        S, IDX = state, idx


def next_id(prefix, existing):
    c = S["counters"]
    while True:
        c[prefix] += 1
        i = "%s_%d" % (prefix, c[prefix])
        if i not in existing:
            return i


def expire_all():
    """Lazily close every open authorization whose deadline has passed (called under LOCK)."""
    if not IDX["open"]:
        return
    ts = now()[0]
    for aid in list(IDX["open"]):
        a = S["authorizations"][aid]
        if a["exp_ts"] <= ts:
            a["status"] = "expired"
            IDX["open"].discard(aid)


def remaining(a):
    return a["amount"] - a["captured_amount"] if a["status"] == "open" else 0


def held_of(uid):
    return sum(remaining(S["authorizations"][i]) for i in IDX["open"]
               if S["authorizations"][i]["from_user_id"] == uid)


def available_of(uid):
    return max(0, S["users"][uid]["balance"] - held_of(uid))


# ---------------------------------------------------------------- output

def pay_out(p):
    u = S["users"]
    return {
        "payment_id": p["payment_id"],
        "from_user_id": p["from_user_id"],
        "from_handle": u[p["from_user_id"]]["handle"],
        "to_user_id": p["to_user_id"],
        "to_handle": u[p["to_user_id"]]["handle"],
        "amount": p["amount"],
        "currency": S["currency"],
        "note": p["note"],
        "visibility": p["visibility"],
        "request_id": p["request_id"],
        "settlement_id": p["settlement_id"],
        "authorization_id": p.get("authorization_id"),
        "created_at": p["created_at"],
    }


def auth_out(a):
    u = S["users"]
    return {
        "authorization_id": a["authorization_id"],
        "from_user_id": a["from_user_id"],
        "from_handle": u[a["from_user_id"]]["handle"],
        "to_user_id": a["to_user_id"],
        "to_handle": u[a["to_user_id"]]["handle"],
        "amount": a["amount"],
        "captured_amount": a["captured_amount"],
        "remaining_amount": remaining(a),
        "currency": S["currency"],
        "note": a["note"],
        "visibility": a["visibility"],
        "status": a["status"],
        "expires_at": a["expires_at"],
        "payment_id": a["payment_ids"][-1] if a["payment_ids"] else None,
        "payment_ids": list(a["payment_ids"]),
        "created_at": a["created_at"],
    }


def req_out(r):
    u = S["users"]
    return {
        "request_id": r["request_id"],
        "requester_id": r["requester_id"],
        "requester_handle": u[r["requester_id"]]["handle"],
        "payer_id": r["payer_id"],
        "payer_handle": u[r["payer_id"]]["handle"],
        "amount": r["amount"],
        "currency": S["currency"],
        "note": r["note"],
        "status": r["status"],
        "payment_id": r["payment_id"],
        "created_at": r["created_at"],
    }


def new_payment(frm, to, amount, note, vis, request_id=None, settlement_id=None, stamp=None,
                authorization_id=None):
    ts, cat = stamp or now()
    pid = next_id("p", S["payments"])
    p = {"payment_id": pid, "from_user_id": frm, "to_user_id": to, "amount": amount, "note": note,
         "visibility": vis, "request_id": request_id, "settlement_id": settlement_id,
         "authorization_id": authorization_id, "created_at": cat, "ts": ts}
    S["payments"][pid] = p
    return p


def new_request(requester, payer, amount, note, stamp=None):
    ts, cat = stamp or now()
    rid = next_id("rq", S["requests"])
    r = {"request_id": rid, "requester_id": requester, "payer_id": payer, "amount": amount,
         "note": note, "status": "pending", "payment_id": None, "created_at": cat, "ts": ts}
    S["requests"][rid] = r
    return r


# ---------------------------------------------------------------- state build / validate

def build_from_fixture(fx):
    if not isinstance(fx, dict):
        raise bad(msg="fixture must be an object")
    cur = fx.get("currency", "EUR")
    if not isinstance(cur, str) or not cur:
        raise bad(msg="invalid currency")
    mu = to_int(fx.get("minor_units", 2))
    if mu not in (0, 2, 3):
        raise bad(msg="invalid minor_units")
    users_in = fx.get("users", [])
    if not isinstance(users_in, list):
        raise bad(msg="users must be a list")
    seen_id, seen_email, seen_handle = set(), set(), set()
    prepared = []
    for u in users_in:
        if not isinstance(u, dict):
            raise bad(msg="user must be an object")
        uid, email, pw = u.get("id"), u.get("email"), u.get("password")
        if not all(isinstance(x, str) and x for x in (uid, email)) or not isinstance(pw, str):
            raise bad(msg="user needs id, email and password")
        handle = u.get("handle")
        if handle is None:
            handle = derive_handle(email)
        if not isinstance(handle, str) or not HANDLE_RE.match(handle):
            raise bad(msg="invalid handle")
        dn = u.get("display_name", "")
        if not isinstance(dn, str):
            raise bad(msg="invalid display_name")
        bal = to_int(u.get("balance", 0))
        if bal is None or bal < 0 or bal > MAX_BALANCE:
            raise bad(msg="invalid balance")
        if uid in seen_id or email.lower() in seen_email or handle in seen_handle:
            raise bad(msg="duplicate user")
        seen_id.add(uid)
        seen_email.add(email.lower())
        seen_handle.add(handle)
        prepared.append((uid, email, dn, handle, bal, pw))
    ttl = fx.get("authorization_ttl_seconds", DEFAULT_TTL)
    if isinstance(ttl, bool) or to_int(ttl) is None or to_int(ttl) < 1:
        raise bad(msg="authorization_ttl_seconds must be a positive integer")
    ttl = to_int(ttl)
    auths_in = fx.get("authorizations", [])
    if not isinstance(auths_in, list):
        raise bad(msg="authorizations must be a list")
    ops = fx.get("settlement_operator_ids", [])
    if not isinstance(ops, list) or not all(isinstance(o, str) for o in ops):
        raise bad(msg="settlement_operator_ids must be a list of strings")
    pays = fx.get("payments", [])
    reqs = fx.get("requests", [])
    if not isinstance(pays, list) or not isinstance(reqs, list):
        raise bad(msg="payments and requests must be lists")

    with ThreadPoolExecutor(4) as ex:
        hashes = list(ex.map(lambda t: hash_pw(t[5]), prepared))
    users = {}
    for (uid, email, dn, handle, bal, _), h in zip(prepared, hashes):
        users[uid] = {"id": uid, "email": email, "display_name": dn, "handle": handle,
                      "balance": bal, "pw": h}
    state = {"currency": cur, "minor_units": mu, "users": users, "tokens": {}, "payments": {},
             "requests": {}, "operators": list(dict.fromkeys(ops)), "idem": {},
             "authorizations": {}, "auth_ttl": ttl,
             "counters": {"p": 0, "rq": 0, "sp": 0, "st": 0, "u": 0, "a": 0}}

    def stamp(o):
        c = o.get("created_at")
        if c is None:
            return now()
        try:
            if not isinstance(c, str):
                raise ValueError
            d = datetime.fromisoformat(c)
            if d.tzinfo is None:
                raise ValueError
            return int(d.timestamp() * 1_000_000), c
        except Exception:
            raise bad(msg="invalid created_at")

    for p in pays:
        if not isinstance(p, dict):
            raise bad(msg="payment must be an object")
        pid = p.get("id")
        amt = to_int(p.get("amount"))
        note = p.get("note", "")
        vis = p.get("visibility", "public")
        if (not isinstance(pid, str) or not pid or pid in state["payments"]
                or p.get("from_user_id") not in users or p.get("to_user_id") not in users
                or amt is None or amt < 0 or not isinstance(note, str)
                or not isinstance(vis, str) or vis not in VISIBILITIES):
            raise bad(msg="invalid seeded payment")
        ts, cat = stamp(p)
        rq = p.get("request_id")
        state["payments"][pid] = {
            "payment_id": pid, "from_user_id": p["from_user_id"], "to_user_id": p["to_user_id"],
            "amount": amt, "note": note, "visibility": vis,
            "request_id": rq if isinstance(rq, str) else None,
            "settlement_id": None, "created_at": cat, "ts": ts}
    for r in reqs:
        if not isinstance(r, dict):
            raise bad(msg="request must be an object")
        rid = r.get("id")
        amt = to_int(r.get("amount"))
        note = r.get("note", "")
        st = r.get("status", "pending")
        pid = r.get("payment_id")
        if (not isinstance(rid, str) or not rid or rid in state["requests"]
                or r.get("requester_id") not in users or r.get("payer_id") not in users
                or amt is None or amt < 0 or not isinstance(note, str)
                or not isinstance(st, str) or st not in STATUSES
                or (pid is not None and pid not in state["payments"])):
            raise bad(msg="invalid seeded request")
        ts, cat = stamp(r)
        state["requests"][rid] = {
            "request_id": rid, "requester_id": r["requester_id"], "payer_id": r["payer_id"],
            "amount": amt, "note": note, "status": st, "payment_id": pid,
            "created_at": cat, "ts": ts}
    held = {}
    now_ts = now()[0]
    for a in auths_in:
        if not isinstance(a, dict):
            raise bad(msg="authorization must be an object")
        aid = a.get("id")
        amt = to_int(a.get("amount"))
        note = a.get("note", "")
        vis = a.get("visibility", "public")
        st = a.get("status", "open")
        exp = a.get("expires_at")
        if (not isinstance(aid, str) or not aid or aid in state["authorizations"]
                or a.get("from_user_id") not in users or a.get("to_user_id") not in users
                or amt is None or amt < 1 or not isinstance(note, str)
                or not isinstance(vis, str) or vis not in VISIBILITIES
                or not isinstance(st, str) or st not in AUTH_STATUSES or not isinstance(exp, str)):
            raise bad(msg="invalid seeded authorization")
        try:
            d = datetime.fromisoformat(exp)
            if d.tzinfo is None:
                raise ValueError
            exp_ts = int(d.timestamp() * 1_000_000)
        except Exception:
            raise bad(msg="invalid expires_at")
        ts, cat = stamp(a)
        cap = 0
        if st == "captured":
            cap = amt
        elif st == "open" and exp_ts > now_ts:
            held[a["from_user_id"]] = held.get(a["from_user_id"], 0) + amt
        state["authorizations"][aid] = {
            "authorization_id": aid, "from_user_id": a["from_user_id"], "to_user_id": a["to_user_id"],
            "amount": amt, "captured_amount": cap, "note": note, "visibility": vis,
            "status": st, "expires_at": exp, "exp_ts": exp_ts, "payment_ids": [],
            "created_at": cat, "ts": ts}
    for uid, h in held.items():
        if h > users[uid]["balance"]:
            raise bad(msg="seeded holds exceed balance")
    return state


def validate_state(st):
    """Validate an imported state; returns a private deep copy or raises ApiError(422)."""
    def need(c):
        if not c:
            raise bad(msg="invalid state")

    def is_int(x):
        return isinstance(x, int) and not isinstance(x, bool)

    def is_str(x):
        return isinstance(x, str)

    try:
        st = json.loads(json.dumps(st))
        need(isinstance(st, dict))
        st.setdefault("authorizations", {})
        st.setdefault("auth_ttl", DEFAULT_TTL)
        if isinstance(st.get("counters"), dict):
            st["counters"].setdefault("a", 0)
        need(is_str(st.get("currency")) and st["currency"])
        need(is_int(st.get("minor_units")) and st["minor_units"] in (0, 2, 3))
        users, tokens, pays, reqs = st.get("users"), st.get("tokens"), st.get("payments"), st.get("requests")
        for x in (users, tokens, pays, reqs, st.get("idem"), st.get("counters")):
            need(isinstance(x, dict))
        need(isinstance(st.get("operators"), list) and all(is_str(o) for o in st["operators"]))
        emails, handles = set(), set()
        for uid, u in users.items():
            need(isinstance(u, dict) and u.get("id") == uid)
            need(is_str(u.get("email")) and is_str(u.get("display_name")) and is_str(u.get("handle")))
            need(HANDLE_RE.match(u["handle"]))
            need(is_int(u.get("balance")) and 0 <= u["balance"] <= MAX_BALANCE)
            pw = u.get("pw")
            need(isinstance(pw, dict) and pw.get("algo") == "scrypt")
            need(all(is_int(pw.get(k)) for k in ("n", "r", "p")))
            need(2 <= pw["n"] <= 2 ** 15 and pw["n"] & (pw["n"] - 1) == 0)
            need(1 <= pw["r"] <= 16 and 1 <= pw["p"] <= 4)
            need(is_str(pw.get("salt")) and is_str(pw.get("hash")))
            bytes.fromhex(pw["salt"])
            bytes.fromhex(pw["hash"])
            need(u["email"].lower() not in emails and u["handle"] not in handles)
            emails.add(u["email"].lower())
            handles.add(u["handle"])
        for t, uid in tokens.items():
            need(is_str(t) and uid in users)
        for pid, p in pays.items():
            need(isinstance(p, dict) and p.get("payment_id") == pid)
            need(p.get("from_user_id") in users and p.get("to_user_id") in users)
            need(is_int(p.get("amount")) and p["amount"] >= 0 and is_str(p.get("note")))
            need(p.get("visibility") in VISIBILITIES)
            need(p.get("request_id") is None or is_str(p["request_id"]))
            need(p.get("settlement_id") is None or is_str(p["settlement_id"]))
            need(is_str(p.get("created_at")) and is_int(p.get("ts")))
        for rid, r in reqs.items():
            need(isinstance(r, dict) and r.get("request_id") == rid)
            need(r.get("requester_id") in users and r.get("payer_id") in users)
            need(is_int(r.get("amount")) and r["amount"] >= 0 and is_str(r.get("note")))
            need(r.get("status") in STATUSES)
            need(r.get("payment_id") is None or r["payment_id"] in pays)
            need(is_str(r.get("created_at")) and is_int(r.get("ts")))
        need(is_int(st["auth_ttl"]) and st["auth_ttl"] >= 1)
        auths = st["authorizations"]
        need(isinstance(auths, dict))
        for aid, a in auths.items():
            need(isinstance(a, dict) and a.get("authorization_id") == aid)
            need(a.get("from_user_id") in users and a.get("to_user_id") in users)
            need(is_int(a.get("amount")) and a["amount"] >= 1 and is_int(a.get("captured_amount"))
                 and 0 <= a["captured_amount"] <= a["amount"])
            need(is_str(a.get("note")) and a.get("visibility") in VISIBILITIES)
            need(a.get("status") in AUTH_STATUSES and is_str(a.get("expires_at")))
            need(is_int(a.get("exp_ts")) and is_str(a.get("created_at")) and is_int(a.get("ts")))
            need(isinstance(a.get("payment_ids"), list) and all(is_str(x) for x in a["payment_ids"]))
        for k, rec in st["idem"].items():
            need(is_str(k) and isinstance(rec, dict) and is_str(rec.get("canon")) and "response" in rec)
        for k in ("p", "rq", "sp", "st", "u", "a"):
            need(is_int(st["counters"].get(k)))
        return st
    except ApiError:
        raise
    except Exception:
        raise bad(msg="invalid state")


# ---------------------------------------------------------------- auth / idempotency

def authenticate(headers):
    h = headers.get("Authorization") or ""
    parts = h.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise ApiError(401, "unauthenticated", "missing or malformed bearer token")
    uid = S["tokens"].get(parts[1].strip())
    if uid is None:
        raise ApiError(401, "unauthenticated", "unknown token")
    return uid


def idem_claim(uid, method, path, headers, raw, empty_ok=False):
    """Returns (kid, canonical_body, body, replay_response_or_None)."""
    key = headers.get("Idempotency-Key")
    if key is None or key == "":
        raise ApiError(400, "missing_idempotency_key", "Idempotency-Key header required")
    if len(key) > 255:
        raise bad(msg="Idempotency-Key must be 1 to 255 characters")
    if empty_ok and not raw.strip():
        body = {}
    else:
        body = parse_json(raw)
    if not isinstance(body, dict):
        raise malformed("body must be a JSON object")
    kid = json.dumps([uid, method, path, key])
    c = canon(body)
    rec = S["idem"].get(kid)
    if rec is not None:
        if rec["canon"] != c:
            raise ApiError(409, "idempotency_key_reuse", "key already used with a different body")
        return kid, c, body, rec["response"]
    return kid, c, body, None


def idem_done(kid, c, resp):
    S["idem"][kid] = {"canon": c, "response": resp}
    return 201, resp


def get_page(q):
    def num(name, default, lo, hi):
        if name not in q:
            return default
        v = q[name][0]
        if not DIGITS_RE.fullmatch(v) or len(v) > 15:
            raise bad(msg=name + " must be a plain decimal integer")
        n = int(v)
        if n < lo or (hi is not None and n > hi):
            raise bad(msg=name + " out of range")
        return n
    return num("limit", 50, 1, 200), num("offset", 0, 0, None)


def page(items, limit, offset):
    chunk = items[offset:offset + limit + 1]
    return chunk[:limit], len(chunk) > limit


def order_key(item):
    i, r = item
    return (r["ts"], i)


# ---------------------------------------------------------------- endpoints

def h_health(ctx):
    return 200, {"status": "ok"}


def h_reset(ctx):
    install(build_from_fixture(parse_json(ctx["raw"])))
    return 204, None


def h_export(ctx):
    snap = json.dumps({"track": "pocketful", "format_version": 1, "state": S})
    return 200, snap


def h_import(ctx):
    body = parse_json(ctx["raw"])
    if not isinstance(body, dict):
        raise malformed("body must be an object")
    fv = body.get("format_version")
    if (body.get("track") != "pocketful" or isinstance(fv, bool) or fv != 1
            or not isinstance(body.get("state"), dict)):
        raise bad(msg="invalid export envelope")
    install(validate_state(body["state"]))
    return 204, None


def auth_reply(uid, status):
    u = S["users"][uid]
    tok = secrets.token_urlsafe(32)
    S["tokens"][tok] = uid
    return status, {"user_id": uid, "display_name": u["display_name"], "token": tok}


def credentials(ctx, fields):
    body = parse_json(ctx["raw"])
    if not isinstance(body, dict):
        raise malformed("body must be an object")
    for f in fields:
        if f in body and not isinstance(body[f], str):
            raise malformed(f + " must be a string")
    for f in fields:
        if f not in body:
            raise bad(msg=f + " is required")
    return body


def h_signup(ctx):
    body = credentials(ctx, ("email", "password", "display_name"))
    email, pw, dn = body["email"], body["password"], body["display_name"]
    if not re.fullmatch(r"[^@\s]+@[^@\s]+", email):
        raise bad(msg="email must be of the form local@domain")
    if len(pw) < 8:
        raise bad(msg="password must be at least 8 characters")
    if not dn.strip():
        raise bad(msg="display_name must not be empty")
    if email.lower() in IDX["email"]:
        raise ApiError(409, "email_taken", "email already registered")
    handle = derive_handle(email)
    if handle in IDX["handle"]:
        raise ApiError(409, "handle_taken", "derived handle already taken")
    uid = next_id("u", S["users"])
    S["users"][uid] = {"id": uid, "email": email, "display_name": dn, "handle": handle,
                       "balance": 0, "pw": hash_pw(pw)}
    IDX["email"][email.lower()] = uid
    IDX["handle"][handle] = uid
    return auth_reply(uid, 201)


def h_login(ctx):
    body = credentials(ctx, ("email", "password"))
    uid = IDX["email"].get(body["email"].lower())
    if uid is None or not verify_pw(body["password"], S["users"][uid]["pw"]):
        raise ApiError(401, "unauthenticated", "invalid credentials")
    return auth_reply(uid, 200)


def h_me(ctx):
    u = S["users"][ctx["uid"]]
    return 200, {"user_id": u["id"], "display_name": u["display_name"], "handle": u["handle"],
                 "balance": u["balance"], "total": u["balance"], "available": available_of(u["id"]),
                 "held": held_of(u["id"]), "currency": S["currency"], "minor_units": S["minor_units"]}


def lookup_handle(h):
    uid = IDX["handle"].get(h)
    if uid is None:
        raise ApiError(404, "not_found", "no such handle")
    return uid


def h_payment(ctx):
    uid = ctx["uid"]
    kid, c, body, replay = idem_claim(uid, "POST", ctx["path"], ctx["headers"], ctx["raw"])
    if replay is not None:
        return 200, replay
    to_handle = handle_field(body, "to_handle")
    amount = parse_amount(body.get("amount"))
    note = parse_note(body)
    vis = parse_visibility(body)
    me = S["users"][uid]
    if to_handle == me["handle"]:
        raise bad("self_payment", "cannot pay yourself")
    to = lookup_handle(to_handle)
    if available_of(uid) < amount:
        raise ApiError(409, "insufficient_funds", "balance too low")
    me["balance"] -= amount
    S["users"][to]["balance"] += amount
    return idem_done(kid, c, pay_out(new_payment(uid, to, amount, note, vis)))


def h_request_create(ctx):
    uid = ctx["uid"]
    kid, c, body, replay = idem_claim(uid, "POST", ctx["path"], ctx["headers"], ctx["raw"])
    if replay is not None:
        return 200, replay
    payer_handle = handle_field(body, "payer_handle")
    amount = parse_amount(body.get("amount"))
    note = parse_note(body)
    if payer_handle == S["users"][uid]["handle"]:
        raise bad("self_request", "cannot request from yourself")
    payer = lookup_handle(payer_handle)
    return idem_done(kid, c, req_out(new_request(uid, payer, amount, note)))


def h_request_pay(ctx):
    uid, rid = ctx["uid"], ctx["rid"]
    kid, c, body, replay = idem_claim(uid, "POST", ctx["path"], ctx["headers"], ctx["raw"], empty_ok=True)
    if replay is not None:
        return 200, replay
    vis = parse_visibility(body)
    r = S["requests"].get(rid)
    if r is None:
        raise ApiError(404, "not_found", "no such request")
    if r["payer_id"] != uid:
        raise ApiError(403, "forbidden", "only the payer may pay")
    if r["status"] != "pending":
        raise ApiError(409, "request_not_pending", "request is not pending")
    me = S["users"][uid]
    if available_of(uid) < r["amount"]:
        raise ApiError(409, "insufficient_funds", "balance too low")
    me["balance"] -= r["amount"]
    S["users"][r["requester_id"]]["balance"] += r["amount"]
    p = new_payment(uid, r["requester_id"], r["amount"], r["note"], vis, request_id=rid)
    r["status"] = "paid"
    r["payment_id"] = p["payment_id"]
    return idem_done(kid, c, pay_out(p))


def request_transition(ctx, party, target):
    r = S["requests"].get(ctx["rid"])
    if r is None:
        raise ApiError(404, "not_found", "no such request")
    if r[party] != ctx["uid"]:
        raise ApiError(403, "forbidden", "not permitted for this request")
    if r["status"] == "pending":
        r["status"] = target
    elif r["status"] != target:
        raise ApiError(409, "request_not_pending", "request is not pending")
    return 200, req_out(r)


def h_request_decline(ctx):
    return request_transition(ctx, "payer_id", "declined")


def h_request_cancel(ctx):
    return request_transition(ctx, "requester_id", "cancelled")


def h_requests_list(ctx):
    q, uid = ctx["query"], ctx["uid"]
    limit, offset = get_page(q)
    direction = q["direction"][0] if "direction" in q else None
    status = q["status"][0] if "status" in q else None
    if direction not in (None, "incoming", "outgoing") or status not in (None,) + STATUSES:
        raise bad(msg="invalid direction or status")
    items = []
    for i, r in enumerate(S["requests"].values()):
        inc, out = r["payer_id"] == uid, r["requester_id"] == uid
        if not (inc or out):
            continue
        if (direction == "incoming" and not inc) or (direction == "outgoing" and not out):
            continue
        if status is not None and r["status"] != status:
            continue
        items.append((i, r))
    items.sort(key=order_key, reverse=True)
    chunk, more = page(items, limit, offset)
    return 200, {"requests": [req_out(r) for _, r in chunk], "has_more": more}


def h_split(ctx):
    uid = ctx["uid"]
    kid, c, body, replay = idem_claim(uid, "POST", ctx["path"], ctx["headers"], ctx["raw"])
    if replay is not None:
        return 200, replay
    amount = parse_amount(body.get("amount"))
    if "participant_handles" not in body:
        raise bad(msg="participant_handles is required")
    hs = body["participant_handles"]
    if not isinstance(hs, list) or not all(isinstance(h, str) for h in hs):
        raise malformed("participant_handles must be a list of strings")
    if not hs or len(set(hs)) != len(hs):
        raise bad(msg="participant_handles must be non-empty and unique")
    note = parse_note(body)
    ids = [lookup_handle(h) for h in hs]
    base, rem = divmod(amount, len(hs))
    shares = [base + (1 if i < rem else 0) for i in range(len(hs))]
    stamp = now()
    reqs = [req_out(new_request(uid, pid, sh, note, stamp))
            for pid, sh in zip(ids, shares) if pid != uid]
    resp = {"split_id": next_id("sp", {}), "amount": amount, "currency": S["currency"], "note": note,
            "shares": [{"handle": h, "amount": sh} for h, sh in zip(hs, shares)],
            "requests": reqs, "created_at": stamp[1]}
    return idem_done(kid, c, resp)


def h_auth_create(ctx):
    uid = ctx["uid"]
    kid, c, body, replay = idem_claim(uid, "POST", ctx["path"], ctx["headers"], ctx["raw"])
    if replay is not None:
        return 200, replay
    to_handle = handle_field(body, "to_handle")
    amount = parse_amount(body.get("amount"))
    note = parse_note(body)
    vis = parse_visibility(body)
    me = S["users"][uid]
    if to_handle == me["handle"]:
        raise bad("self_payment", "cannot authorize yourself")
    to = lookup_handle(to_handle)
    if available_of(uid) < amount:
        raise ApiError(409, "insufficient_funds", "available balance too low")
    ts, cat = now()
    aid = next_id("a", S["authorizations"])
    exp_ts = ts + S["auth_ttl"] * 1_000_000
    a = {"authorization_id": aid, "from_user_id": uid, "to_user_id": to, "amount": amount,
         "captured_amount": 0, "note": note, "visibility": vis, "status": "open",
         "expires_at": fmt_ts(exp_ts), "exp_ts": exp_ts, "payment_ids": [],
         "created_at": cat, "ts": ts}
    S["authorizations"][aid] = a
    IDX["open"].add(aid)
    return idem_done(kid, c, auth_out(a))


def h_auth_capture(ctx):
    uid = ctx["uid"]
    kid, c, body, replay = idem_claim(uid, "POST", ctx["path"], ctx["headers"], ctx["raw"], empty_ok=True)
    if replay is not None:
        return 200, replay
    a = S["authorizations"].get(ctx["aid"])
    if a is None:
        raise ApiError(404, "not_found", "no such authorization")
    if a["to_user_id"] != uid:
        raise ApiError(403, "forbidden", "only the receiver may capture")
    amount = parse_amount(body["amount"]) if "amount" in body else None
    final = body.get("final", True)
    if not isinstance(final, bool):
        raise bad(msg="final must be a boolean")
    if a["status"] == "expired":
        raise ApiError(409, "authorization_expired", "authorization has expired")
    if a["status"] != "open":
        raise ApiError(409, "authorization_not_open", "authorization is not open")
    left = remaining(a)
    if amount is None:
        amount = left
    if amount > left:
        raise bad("capture_exceeds_authorization", "amount exceeds the remaining hold")
    S["users"][a["from_user_id"]]["balance"] -= amount
    S["users"][uid]["balance"] += amount
    p = new_payment(a["from_user_id"], uid, amount, a["note"], a["visibility"],
                    authorization_id=a["authorization_id"])
    a["captured_amount"] += amount
    a["payment_ids"].append(p["payment_id"])
    if final or a["captured_amount"] >= a["amount"]:
        a["status"] = "captured"
        IDX["open"].discard(a["authorization_id"])
    return idem_done(kid, c, pay_out(p))


def h_auth_void(ctx):
    a = S["authorizations"].get(ctx["aid"])
    if a is None:
        raise ApiError(404, "not_found", "no such authorization")
    if a["from_user_id"] != ctx["uid"]:
        raise ApiError(403, "forbidden", "only the payer may void")
    if a["status"] == "open":
        a["status"] = "voided"
        IDX["open"].discard(a["authorization_id"])
    elif a["status"] != "voided":
        raise ApiError(409, "authorization_not_open", "authorization is not open")
    return 200, auth_out(a)


def h_auth_list(ctx):
    q, uid = ctx["query"], ctx["uid"]
    limit, offset = get_page(q)
    direction = q["direction"][0] if "direction" in q else None
    status = q["status"][0] if "status" in q else None
    if direction not in (None, "incoming", "outgoing") or status not in (None,) + AUTH_STATUSES:
        raise bad(msg="invalid direction or status")
    items = []
    for i, a in enumerate(S["authorizations"].values()):
        out, inc = a["from_user_id"] == uid, a["to_user_id"] == uid
        if not (inc or out):
            continue
        if (direction == "incoming" and not inc) or (direction == "outgoing" and not out):
            continue
        if status is not None and a["status"] != status:
            continue
        items.append((i, a))
    items.sort(key=order_key, reverse=True)
    chunk, more = page(items, limit, offset)
    return 200, {"authorizations": [auth_out(a) for _, a in chunk], "has_more": more}


def h_activity(ctx):
    uid = ctx["uid"]
    limit, offset = get_page(ctx["query"])
    items = [(i, p) for i, p in enumerate(S["payments"].values())
             if p["visibility"] == "public" or uid in (p["from_user_id"], p["to_user_id"])]
    items.sort(key=order_key, reverse=True)
    chunk, more = page(items, limit, offset)
    return 200, {"payments": [pay_out(p) for _, p in chunk], "has_more": more}


def h_settlement(ctx):
    uid = ctx["uid"]
    if uid not in IDX["operators"]:
        raise ApiError(403, "forbidden", "settlement operator required")
    kid, c, body, replay = idem_claim(uid, "POST", ctx["path"], ctx["headers"], ctx["raw"])
    if replay is not None:
        return 200, replay
    tr = body.get("transfers")
    if not isinstance(tr, list) or not 1 <= len(tr) <= 32 or not all(isinstance(t, dict) for t in tr):
        raise bad(msg="transfers must be 1..32 objects")
    entries = []
    for t in tr:
        fh = handle_field(t, "from_handle")
        th = handle_field(t, "to_handle")
        amount = parse_amount(t.get("amount"))
        note = parse_note(t)
        vis = parse_visibility(t)
        if fh == th:
            raise bad("self_payment", "self transfer")
        entries.append((lookup_handle(fh), lookup_handle(th), amount, note, vis))
    delta = {}
    for f, t, a, _, _ in entries:
        delta[f] = delta.get(f, 0) - a
        delta[t] = delta.get(t, 0) + a
    if any(d < 0 and S["users"][u]["balance"] + d < held_of(u) for u, d in delta.items()):
        raise ApiError(409, "insufficient_funds", "settlement not affordable")
    for u, d in delta.items():
        S["users"][u]["balance"] += d
    stamp = now()
    sid = next_id("st", {})
    pays = [pay_out(new_payment(f, t, a, n, v, settlement_id=sid, stamp=stamp))
            for f, t, a, n, v in entries]
    return idem_done(kid, c, {"settlement_id": sid, "committed_at": stamp[1], "payments": pays})


# ---------------------------------------------------------------- routing

STATIC = {
    ("GET", "/health"): (h_health, False),
    ("POST", "/_test/reset"): (h_reset, False),
    ("GET", "/_test/export"): (h_export, False),
    ("POST", "/_test/import"): (h_import, False),
    ("POST", "/auth/signup"): (h_signup, False),
    ("POST", "/auth/login"): (h_login, False),
    ("GET", "/me"): (h_me, True),
    ("POST", "/payments"): (h_payment, True),
    ("POST", "/requests"): (h_request_create, True),
    ("GET", "/requests"): (h_requests_list, True),
    ("POST", "/splits"): (h_split, True),
    ("GET", "/activity"): (h_activity, True),
    ("POST", "/settlements"): (h_settlement, True),
    ("POST", "/authorizations"): (h_auth_create, True),
    ("GET", "/authorizations"): (h_auth_list, True),
}
AUTH_ACTIONS = {"capture": h_auth_capture, "void": h_auth_void}
AUTH_RE = re.compile(r"^/authorizations/([^/]+)/(capture|void)$")
UI_PATHS = ("/", "/requests", "/split", "/signup", "/login", "/authorizations")
STATIC_TYPES = {".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".woff2": "font/woff2", ".svg": "image/svg+xml"}


class Raw:
    def __init__(self, data, ctype, cache="no-cache"):
        self.data, self.ctype, self.cache = data, ctype, cache


def read_static(name):
    try:
        with open(os.path.join(STATIC_DIR, name), "rb") as f:
            return f.read()
    except OSError:
        return None


def ui_response(method, path, headers):
    """HTML shell for UI routes and static assets; None when the request is an API call."""
    if method != "GET":
        return None
    if path.startswith("/static/") and "/" not in path[8:] and ".." not in path:
        data = read_static(path[8:])
        if data is None:
            raise ApiError(404, "not_found", "no such asset")
        ext = os.path.splitext(path)[1]
        cache = "public, max-age=31536000, immutable" if ext == ".woff2" else "no-cache"
        return Raw(data, STATIC_TYPES.get(ext, "application/octet-stream"), cache)
    if path in UI_PATHS:
        shared = path in ("/requests", "/authorizations")
        if shared and "text/html" not in (headers.get("Accept") or ""):
            return None
        data = read_static("index.html")
        if data is None:
            raise ApiError(404, "not_found", "ui not built")
        return Raw(data, "text/html; charset=utf-8")
    return None
REQ_ACTIONS = {"pay": h_request_pay, "decline": h_request_decline, "cancel": h_request_cancel}
REQ_RE = re.compile(r"^/requests/([^/]+)/(pay|decline|cancel)$")


def route(method, path):
    r = STATIC.get((method, path))
    if r:
        return r[0], r[1], {}
    m = REQ_RE.match(path)
    if m and method == "POST":
        return REQ_ACTIONS[m.group(2)], True, {"rid": m.group(1)}
    m = AUTH_RE.match(path)
    if m and method == "POST":
        return AUTH_ACTIONS[m.group(2)], True, {"aid": m.group(1)}
    raise ApiError(404, "not_found", "no such route")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def read_body(self):
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te:
            out = b""
            while True:
                line = self.rfile.readline(65537).split(b";", 1)[0].strip()
                n = int(line or b"0", 16)
                if n == 0:
                    while self.rfile.readline(65537).strip():
                        pass
                    return out
                out += self.rfile.read(n)
                self.rfile.readline()
                if len(out) > MAX_BODY:
                    raise ValueError("too large")
        n = int(self.headers.get("Content-Length") or 0)
        if n < 0 or n > MAX_BODY:
            raise ValueError("bad length")
        return self.rfile.read(n) if n else b""

    def send(self, status, body):
        ctype, cache = "application/json; charset=utf-8", None
        if isinstance(body, Raw):
            data, ctype, cache = body.data, body.ctype, body.cache
        elif body is None:
            data = b""
        elif isinstance(body, str):
            data = body.encode()
        else:
            data = json.dumps(body).encode()
        self.send_response(status)
        if status != 204:
            self.send_header("Content-Type", ctype)
        if cache:
            self.send_header("Cache-Control", cache)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def handle_any(self, method):
        try:
            try:
                raw = self.read_body()
            except Exception:
                self.close_connection = True
                raise malformed("bad body framing")
            u = urlsplit(self.path)
            ui = ui_response(method, u.path, self.headers)
            if ui is not None:
                self.send(200, ui)
                return
            fn, needs_auth, extra = route(method, u.path)
            ctx = {"raw": raw, "headers": self.headers, "path": u.path, "uid": None,
                   "query": parse_qs(u.query, keep_blank_values=True), **extra}
            with LOCK:
                expire_all()
                if needs_auth:
                    ctx["uid"] = authenticate(self.headers)
                status, body = fn(ctx)
            self.send(status, body)
        except ApiError as e:
            self.send(e.status, {"error": {"code": e.code, "message": e.message}})
        except Exception:
            self.send(500, {"error": {"code": "internal_error", "message": "internal error"}})

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def do_PUT(self):
        self.handle_any("PUT")

    do_PATCH = do_DELETE = do_PUT


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 256


def main():
    install(build_from_fixture({"users": []}))
    port = int(os.environ.get("PORT", "8080"))
    Server(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
