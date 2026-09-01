# mypy: ignore
"""
fortune-wheel-llm — weighted LLM fallback rotator for Hermes.

Adapted from llm-keypool (header cooldown, fallback strategies, rpd quota,
slot_count) + position+latency weighting. No secret leakage.

Security & robustness:
- No network calls, no eval/exec/subprocess.
- Local JSON state file only: provider/latency/cooldown/count — never api_key.
- 0o600 permissions on state file.
- Strict JSON validation, ignores corrupted entries.
- No logging of prompts or user content.
- GC scan limited (every 2s) + strict deque filter.
- Gracefully handles missing or single fallback_providers.
"""
import json
import logging
import os
import pathlib
import threading
import time
from collections import deque
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("fortune_wheel_llm")

# --- safe parsers (defined first — used at module level) ---
def _safe_float(v, default=0.0):
    try:
        return float(v)
    except Exception:
        return default

def _safe_int(v, default=0):
    try:
        return int(v)
    except Exception:
        return default

# --- env config (no sensitive data) ---
_ENABLED = os.environ.get("HERMES_FALLBACK_ROTATE", "true").lower() not in ("0", "false", "no", "off", "")
_WEIGHTED = os.environ.get("HERMES_FALLBACK_WEIGHTED", "true").lower() not in ("0", "false", "no", "off", "")
_WEIGHT_POS = _safe_float(os.environ.get("HERMES_FALLBACK_WEIGHT_POS", "0.6"), 0.6)
_WEIGHT_LAT = _safe_float(os.environ.get("HERMES_FALLBACK_WEIGHT_LAT", "1.0"), 1.0)
_LAT_ALPHA = _safe_float(os.environ.get("HERMES_FALLBACK_LATENCY_ALPHA", "0.3"), 0.3)
_LAT_DEFAULT_MS = _safe_float(os.environ.get("HERMES_FALLBACK_LATENCY_DEFAULT_MS", "800"), 800)
_COOLDOWN_BASE_S = _safe_int(os.environ.get("HERMES_FALLBACK_COOLDOWN_BASE_S", "60"), 60)
_COOLDOWN_MAX_S = _safe_int(os.environ.get("HERMES_FALLBACK_COOLDOWN_MAX_S", "14400"), 14400)
_STATE = pathlib.Path(os.environ.get("HERMES_FALLBACK_ROTATE_STATE",
    str(pathlib.Path.home() / ".hermes" / "fortune-wheel-state.json")))
_LOCK = threading.Lock()

# --- llm-keypool vendor (read-only) ---
try:
    from llm_keypool.providers.headers import extract_cooldown as _lk_extract
except Exception:
    _lk_extract = None

try:
    _PROVIDER_CFG = json.load(open(os.path.expanduser("~/.config/llm-keypool/providers.json")))["providers"]
except Exception:
    _PROVIDER_CFG = {}

# --- fallback strategies ---
def _next_utc_midnight():
    now = datetime.now(timezone.utc)
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()

def _next_first_of_month():
    now = datetime.now(timezone.utc)
    m = now.month + 1
    y = now.year + (1 if m > 12 else 0)
    m = 1 if m > 12 else m
    return now.replace(year=y, month=m, day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()

def _rolling(s):
    return lambda: (datetime.now(timezone.utc) + timedelta(seconds=s)).isoformat()

_FALLBACK_STRATS = {
    "daily_utc_midnight": _next_utc_midnight,
    "first_of_calendar_month": _next_first_of_month,
    "rolling_60": _rolling(60),
    "rolling_65": _rolling(65),
    "rolling_120": _rolling(120),
}
_DEFAULT_FB = _rolling(60)

def _fallback_from_cfg(provider: str):
    cfg = _PROVIDER_CFG.get(provider, {})
    key = cfg.get("cooldown_fallback", {}).get("strategy", "rolling_60")
    return _FALLBACK_STRATS.get(key, _DEFAULT_FB)

def _quota_score(provider: str, req_today: int) -> float:
    cfg = _PROVIDER_CFG.get(provider, {})
    rpd = cfg.get("limits", {}).get("rpd")
    return float(rpd - req_today) if isinstance(rpd, (int, float)) else float(-req_today)

def _alias(p: str) -> str:
    p = (p or "").strip().lower()
    return "google" if p == "gemini" else p

# --- state I/O (atomic) ---
def _load() -> dict:
    if not _STATE.exists():
        # backward compat: try old path
        old = pathlib.Path.home() / ".hermes" / "ha-fallback-rotate-state.json"
        if old.exists():
            try:
                data = json.loads(old.read_text())
                if isinstance(data, dict):
                    _save(data)
                    return data
            except Exception:
                pass
        return {}
    try:
        data = json.loads(_STATE.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}

def _save(state: dict) -> None:
    try:
        _STATE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        tmp.replace(_STATE)
        try:
            os.chmod(_STATE, 0o600)
        except Exception:
            pass
    except Exception as e:
        logger.debug("fortune-wheel save failed: %s", e)

def _key() -> str:
    return f"{os.environ.get('HERMES_PROFILE','default')}/{os.environ.get('HERMES_SESSION_ID') or 'standalone'}"

def _now() -> float:
    return time.monotonic()

# --- cooldown & stats ---
def _cooldown_until(state: dict, provider: str):
    p = _alias(provider)
    cd = state.get("cooldown", {}).get(p)
    if cd is not None:
        return cd
    return state.get("stats", {}).get(p, {}).get("cooldown_until")

def _clear_expired(state: dict, provider: str) -> None:
    p = _alias(provider)
    cd = _cooldown_until(state, p)
    if cd is not None and cd <= _now():
        state.get("cooldown", {}).pop(p, None)
        if p in state.get("stats", {}):
            state["stats"][p].pop("cooldown_until", None)
            state["stats"][p].pop("cooldown_until_iso", None)

def _get_avg(state: dict, provider: str) -> float:
    p = _alias(provider)
    s = state.get("stats", {}).get(p, {})
    v = s.get("avg_ms")
    if v is None:
        v = s.get("ewma_ms")
    try:
        return float(v) if v is not None else _LAT_DEFAULT_MS
    except Exception:
        return _LAT_DEFAULT_MS

def _record_latency(provider: str, latency_s: float) -> None:
    p = _alias(provider)
    if not p:
        return
    try:
        ms = float(latency_s) * 1000.0
        if not (0 < ms < 600000):
            return
    except Exception:
        return
    with _LOCK:
        st = _load()
        s = st.setdefault("stats", {}).setdefault(p, {})
        prev = s.get("avg_ms")
        if prev is None:
            prev = s.get("ewma_ms", ms)
        try:
            prev_f = float(prev)
        except Exception:
            prev_f = ms
        ewma = prev_f * (1 - _LAT_ALPHA) + ms * _LAT_ALPHA
        s["avg_ms"] = ewma
        s["ewma_ms"] = ewma
        s["last_latency_ms"] = ms
        s["count"] = _safe_int(s.get("count", 0)) + 1
        _save(st)

def _set_cooldown(state: dict, provider: str, until: float, headers=None) -> None:
    p = _alias(provider)
    iso = ""
    if headers is not None and _lk_extract is not None:
        try:
            iso_try = _lk_extract(p, headers, was_429=True)
            if iso_try:
                iso = iso_try
                dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
                delta = (dt - datetime.now(dt.tzinfo)).total_seconds()
                until = _now() + max(delta, 5)
        except Exception:
            pass
    if headers is None:
        try:
            iso_try = _fallback_from_cfg(p)()
            iso = iso_try
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            delta = (dt - datetime.now(dt.tzinfo)).total_seconds()
            if delta > 120:
                until = _now() + delta
        except Exception:
            pass
    state.setdefault("cooldown", {})[p] = until
    s = state.setdefault("stats", {}).setdefault(p, {})
    s["cooldown_until"] = until
    if iso:
        s["cooldown_until_iso"] = iso
    s["consecutive_429"] = _safe_int(s.get("consecutive_429", 0)) + 1
    s["requests_today"] = _safe_int(s.get("requests_today", 0)) + 1
    _save(state)

def _score(provider: str, idx: int, state: dict) -> float:
    p = _alias(provider)
    pos = idx * _WEIGHT_POS
    req = _safe_int(state.get("stats", {}).get(p, {}).get("requests_today", 0))
    quota = _quota_score(p, req)
    quota_contrib = -quota * 0.0005
    lat = (_get_avg(state, p) / 1000.0) * _WEIGHT_LAT
    return pos + quota_contrib + lat

def _select_weighted(chain: list, state: dict) -> list:
    if len(chain) <= 1:
        return list(chain)
    cands = []
    cooled = []
    for idx, entry in enumerate(chain):
        prov = (entry.get("provider") or "").strip().lower()
        cd = _cooldown_until(state, prov)
        if cd is not None and cd > _now():
            cooled.append((idx, entry))
            continue
        cands.append((_score(prov, idx, state), idx, entry))
    cands.sort(key=lambda x: x[0])
    return [e for _, _, e in cands] + [e for _, e in cooled]

def _effective_index(state: dict, chain: list, start: int) -> int:
    n = len(chain)
    if n <= 1:
        return 0
    i = start % n
    for _ in range(n):
        prov = (chain[i].get("provider") or "").strip().lower()
        cd = _cooldown_until(state, prov)
        if cd is None or cd <= _now():
            return i
        i = (i + 1) % n
    return start

# --- latency polling (lightweight, 2s) ---
def _find_agents():
    import gc
    for obj in gc.get_objects():
        try:
            hist = getattr(obj, "_api_latency_history", None)
            prov = getattr(obj, "provider", None)
            if hist is None or prov is None:
                continue
            if not isinstance(hist, deque):
                continue
            if len(hist) == 0:
                continue
            if not isinstance(prov, str) or not prov:
                continue
            yield obj
        except Exception:
            continue

def _maybe_update_latencies():
    try:
        for ag in _find_agents():
            hist = getattr(ag, "_api_latency_history", None)
            last = float(list(hist)[-1])
            prov = _alias(getattr(ag, "provider", ""))
            with _LOCK:
                st = _load()
                k = f"_last_lat_{prov}"
                if st.get(k) == last:
                    continue
                st[k] = last
                _save(st)
            _record_latency(prov, last)
    except Exception:
        pass

def register():
    if not _ENABLED:
        logger.info("fortune-wheel-llm: disabled (HERMES_FALLBACK_ROTATE=%s)", os.environ.get("HERMES_FALLBACK_ROTATE"))
        return
    try:
        from hermes_cli import fallback_config as fc
    except Exception as e:
        logger.warning("fortune-wheel-llm: fallback_config import failed: %s", e)
        return
    orig_chain = fc.get_fallback_chain

    def get_fallback_chain(config=None):
        chain = orig_chain(config)
        if not chain or len(chain) <= 1:
            return chain
        chain = list(chain)
        k = _key()
        with _LOCK:
            st = _load()
            _maybe_update_latencies()
            for e in chain:
                _clear_expired(st, (e.get("provider") or ""))
            if _WEIGHTED:
                return _select_weighted(chain, st)
            idx = _safe_int(st.get(k, 0)) % len(chain)
            if idx:
                chain = chain[idx:] + chain[:idx]
            eff = _effective_index(st, chain, 0)
            if eff:
                chain = chain[eff:] + chain[:eff]
            return chain

    fc.get_fallback_chain = get_fallback_chain

    try:
        from agent.chat_completion_helpers import try_activate_fallback as _orig
        from agent.chat_completion_helpers import FailoverReason
    except Exception as e:
        logger.warning("fortune-wheel-llm: try_activate_fallback import failed: %s", e)
        return

    def _patched(agent, reason=None):
        try:
            chain = getattr(agent, "_fallback_chain", None) or []
            if len(chain) <= 1:
                return _orig(agent, reason)
            cur = _safe_int(getattr(agent, "_fallback_index", 0) or 0)
            if chain:
                with _LOCK:
                    st = _load()
                    if _WEIGHTED and cur < len(chain):
                        prov = (chain[cur].get("provider") or "").strip().lower()
                        cd = _cooldown_until(st, prov)
                        if cd is not None and cd > _now():
                            for off in range(1, len(chain)):
                                nxt = (cur + off) % len(chain)
                                p2 = (chain[nxt].get("provider") or "").strip().lower()
                                cd2 = _cooldown_until(st, p2)
                                if cd2 is None or cd2 <= _now():
                                    agent._fallback_index = nxt
                                    break
                    elif not _WEIGHTED:
                        k = _key()
                        sess = _safe_int(st.get(k, 0)) % len(chain)
                        eff = _effective_index(st, chain, sess)
                        if eff != sess:
                            agent._fallback_index = eff
        except Exception:
            pass

        activated = _orig(agent, reason)

        try:
            chain = getattr(agent, "_fallback_chain", None) or []
            cur = _safe_int(getattr(agent, "_fallback_index", 0) or 0)
            if chain and len(chain) > 1 and cur > 0 and not _WEIGHTED:
                with _LOCK:
                    st = _load()
                    st[_key()] = cur % len(chain)
                    _save(st)
            _HARD_ERRORS = {FailoverReason.billing, FailoverReason.model_not_found}
            _SOFT_ERRORS = {FailoverReason.rate_limit, FailoverReason.upstream_rate_limit}
            if reason in _HARD_ERRORS | _SOFT_ERRORS:
                try:
                    used = (cur - 1) % len(chain) if cur > 0 else 0
                    entry = chain[used] if chain else {}
                    prov = (entry.get("provider") or entry.get("base_url", "") or "").strip().lower()
                    if prov:
                        with _LOCK:
                            st = _load()
                            s = st.setdefault("stats", {}).setdefault(_alias(prov), {})
                            consec = _safe_int(s.get("consecutive_429", 0))
                            backoff = min(_COOLDOWN_BASE_S * (2 ** consec), _COOLDOWN_MAX_S)
                            until = _now() + backoff
                            _set_cooldown(st, prov, until)
                            if reason in _HARD_ERRORS:
                                logger.warning("fortune-wheel HARD ERROR %s %s cooldown %ds (#%d)", prov, reason.value if reason else '?', backoff, consec + 1)
                            else:
                                logger.info("fortune-wheel cooldown %s %ds (429 #%d)", prov, backoff, consec + 1)
                except Exception:
                    pass
            elif activated:
                try:
                    with _LOCK:
                        st = _load()
                        if chain and len(chain) > 1 and 0 <= cur - 1 < len(chain):
                            p2 = _alias((chain[cur - 1].get("provider") or "").strip().lower())
                            if p2 in st.get("stats", {}):
                                st["stats"][p2]["consecutive_429"] = 0
                                _save(st)
                except Exception:
                    pass
        except Exception:
            pass
        return activated

    import agent.chat_completion_helpers as ch
    ch.try_activate_fallback = _patched

    def _poll():
        import time as _t
        while True:
            try:
                _maybe_update_latencies()
            except Exception:
                pass
            _t.sleep(2)

    threading.Thread(target=_poll, daemon=True, name="fortune-wheel-poll").start()
    mode = "weighted pos+lat+quota" if _WEIGHTED else "pure round-robin"
    logger.info("fortune-wheel-llm: enabled — mode %s (Wpos=%.1f Wlat=%.1f) + cooldown exp", mode, _WEIGHT_POS, _WEIGHT_LAT)

register()
