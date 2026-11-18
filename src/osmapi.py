"""Polite clients for Overpass (POI queries) and Nominatim (geocoding).

Usage policies respected:
- Overpass: one query at a time, identifying User-Agent, short server timeouts, small result caps,
  30 s back-off on 429/406, overpass-api.de (FOSSGIS) is not used by default because it asks
  commercial users to use other instances.
- Nominatim (https://operations.osmfoundation.org/policies/nominatim/): max 1 request/second for the
  whole process, identifying User-Agent, results cached in memory, no autocomplete / bulk use.
"""
import asyncio
import math
import os
import time
from collections import OrderedDict

import httpx

UA = os.environ.get("OSM_USER_AGENT") or "osm-poi-mcp/0.1 (MCP server for OpenStreetMap POI lookup; python-httpx)"
OVERPASS_ENDPOINTS = [e.strip() for e in (os.environ.get("OVERPASS_ENDPOINTS") or
                      "https://overpass.private.coffee/api/interpreter,https://maps.mail.ru/osm/tools/overpass/api/interpreter").split(",") if e.strip()]
NOMINATIM = (os.environ.get("NOMINATIM_URL") or "https://nominatim.openstreetmap.org").rstrip("/")
NOMINATIM_INTERVAL = 1.1      # seconds between Nominatim requests (policy: max 1/s)
OVERPASS_INTERVAL = 1.0       # pause between consecutive Overpass queries
OVERPASS_QL_TIMEOUT = 25      # [timeout:] sent to the server
OVERPASS_DEADLINE = 75        # total seconds per tool call spent on Overpass incl. retries
MAX_RESPONSE_BYTES = 8_000_000


class OverpassError(RuntimeError):
    pass


class _Throttle:
    def __init__(self, interval: float):
        self.interval = interval
        self.lock = asyncio.Lock()
        self.last = 0.0

    async def __aenter__(self):
        await self.lock.acquire()
        wait = self.last + self.interval - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)

    async def __aexit__(self, *exc):
        self.last = time.monotonic()
        self.lock.release()


_overpass_gate = _Throttle(OVERPASS_INTERVAL)
_nominatim_gate = _Throttle(NOMINATIM_INTERVAL)
_cache: "OrderedDict[str, object]" = OrderedDict()
_CACHE_MAX = 500


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers={"User-Agent": UA, "Accept-Language": "en"}, follow_redirects=True,
                             timeout=httpx.Timeout(OVERPASS_QL_TIMEOUT + 15, connect=10))


_bad_until: dict[str, float] = {}   # endpoint -> monotonic time until which it is demoted
DEMOTE_S = 300


def _ordered_endpoints() -> list[str]:
    now = time.monotonic()
    return sorted(OVERPASS_ENDPOINTS, key=lambda e: (_bad_until.get(e, 0) > now, _bad_until.get(e, 0)))


async def overpass(ql: str) -> dict:
    """Run one Overpass QL query (sequentially, process-wide). Endpoints that hang or rate-limit are demoted
    for 5 minutes; quick 5xx (load shedding) is retried after a short pause. Total time capped by OVERPASS_DEADLINE."""
    deadline = time.monotonic() + OVERPASS_DEADLINE
    last = ""
    async with _client() as client:
        for _ in range(4):
            ep = _ordered_endpoints()[0]
            host = ep.split("/")[2]
            left = deadline - time.monotonic()
            if left < 5:
                break
            async with _overpass_gate:
                try:
                    r = await client.post(ep, data={"data": ql}, timeout=httpx.Timeout(min(OVERPASS_QL_TIMEOUT + 10, left), connect=10))
                except (httpx.TimeoutException, httpx.TransportError) as e:
                    last = f"{host}: {type(e).__name__}"
                    _bad_until[ep] = time.monotonic() + DEMOTE_S
                    continue
            if len(r.content) > MAX_RESPONSE_BYTES:
                raise OverpassError("Overpass response too large; narrow the area or categories.")
            if r.status_code == 200:
                try:
                    d = r.json()
                except ValueError:
                    last = f"{host}: non-JSON response"
                    _bad_until[ep] = time.monotonic() + 60
                    continue
                rem = d.get("remark") or ""
                if "runtime error" in rem or "out of memory" in rem.lower():
                    raise OverpassError("Query too heavy for the Overpass server (" + rem[:200] + "). "
                                        "Use a smaller area/radius or fewer categories.")
                _bad_until.pop(ep, None)
                return d
            if r.status_code == 400:
                raise OverpassError("Overpass rejected the query (HTTP 400): " + r.text[:300])
            last = f"{host}: HTTP {r.status_code}"
            if r.status_code in (429, 406):
                _bad_until[ep] = time.monotonic() + DEMOTE_S
            await asyncio.sleep(3)
    raise OverpassError(f"Public Overpass servers did not answer in time ({last}). They are sometimes overloaded; "
                        "try again in a minute, use a smaller area, or set OVERPASS_ENDPOINTS to your own instance.")


async def nominatim(path: str, params: dict) -> object:
    """GET a Nominatim endpoint (search/reverse) as JSON, max 1 request/s, cached."""
    params = {**params, "format": "jsonv2"}
    key = path + "?" + "&".join(f"{k}={params[k]}" for k in sorted(params))
    if key in _cache:
        _cache.move_to_end(key)
        return _cache[key]
    async with _client() as client:
        for attempt in range(3):
            async with _nominatim_gate:
                try:
                    r = await client.get(f"{NOMINATIM}/{path}", params=params, timeout=httpx.Timeout(20, connect=10))
                except (httpx.TimeoutException, httpx.TransportError) as e:
                    err = f"Nominatim unreachable: {type(e).__name__}"
                    continue
            if r.status_code == 200:
                d = r.json()
                _cache[key] = d
                if len(_cache) > _CACHE_MAX:
                    _cache.popitem(last=False)
                return d
            err = f"Nominatim HTTP {r.status_code}: {r.text[:200]}"
            if r.status_code in (400, 403, 404):
                break
            await asyncio.sleep(3 * (attempt + 1))
    raise RuntimeError(err)


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6371008.8 * 2 * math.asin(math.sqrt(a))
