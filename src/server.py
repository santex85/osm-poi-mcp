"""OpenStreetMap POI MCP server: find places (restaurants, hotels, shops, ATMs, ...) via Overpass,
geocode / reverse-geocode via Nominatim.

Data © OpenStreetMap contributors, available under the Open Database License (ODbL) 1.0.
Ported from the osm-poi-extractor Apify actor (no Apify dependency).
"""
import math
import os
import sys

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, os.path.dirname(__file__))
from osm import ATTRIBUTION, CATEGORIES, bbox_km2, build_filters, poi_query, to_row  # noqa: E402
from osmapi import OVERPASS_QL_TIMEOUT, OverpassError, haversine_m, nominatim, overpass  # noqa: E402

mcp = FastMCP("osm-poi", host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8080")))

MAX_LIMIT = 200
MAX_RADIUS_M = 20000
MAX_AREA_KM2 = 25000
LICENSE = "ODbL 1.0 - https://opendatacommons.org/licenses/odbl/1-0/"


def _compact(d: dict) -> dict:
    return {k: v for k, v in d.items() if v not in (None, "", [], {})}


def _parse_bbox(s: str):
    try:
        p = [float(x) for x in s.replace(";", ",").split(",")]
    except ValueError:
        p = []
    if len(p) != 4 or not (-90 <= p[0] < p[2] <= 90 and -180 <= p[1] < p[3] <= 180):
        raise ValueError(f"bbox must be 'south,west,north,east' in degrees with south<north and west<east, e.g. "
                         f"'9.40,99.90,9.60,100.10'; got '{s}'")
    return tuple(p)


def _place(r: dict) -> dict:
    bb = r.get("boundingbox") or []
    return _compact({
        "name": r.get("name"),
        "displayName": r.get("display_name"),
        "lat": float(r["lat"]) if r.get("lat") else None,
        "lon": float(r["lon"]) if r.get("lon") else None,
        "category": f"{r.get('category')}={r.get('type')}" if r.get("category") else None,
        "addressType": r.get("addresstype"),
        "osmType": r.get("osm_type"),
        "osmId": r.get("osm_id"),
        "bbox": [float(bb[0]), float(bb[2]), float(bb[1]), float(bb[3])] if len(bb) == 4 else None,  # s,w,n,e
        "address": r.get("address"),
        "importance": round(r["importance"], 3) if isinstance(r.get("importance"), (int, float)) else None,
    })


async def _resolve_area(name: str):
    """Area name -> (overpass area id or None, bbox s,w,n,e, label) via Nominatim."""
    res = await nominatim("search", {"q": name, "limit": 5, "addressdetails": 0})
    if not res:
        raise ValueError(f"Area '{name}' was not found by Nominatim. Try another spelling, add the country "
                         "(e.g. 'Springfield, Illinois') or pass bbox / lat+lon instead.")
    # prefer real polygons (relations/ways of class boundary/place), else the top hit's bounding box
    best = next((r for r in res if r.get("osm_type") in ("relation", "way") and r.get("category") in ("boundary", "place")), None)
    best = best or res[0]
    bb = [float(x) for x in best["boundingbox"]]  # [s, n, w, e]
    bbox = (bb[0], bb[2], bb[1], bb[3])
    area_id = None
    if best.get("osm_type") == "relation":
        area_id = 3600000000 + int(best["osm_id"])
    elif best.get("osm_type") == "way":
        area_id = 2400000000 + int(best["osm_id"])
    return area_id, bbox, best.get("display_name")


@mcp.tool()
async def find_places(
    categories: list[str] | None = None,
    tags: list[str] | None = None,
    area: str | None = None,
    bbox: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    radius_m: int = 1000,
    name_contains: str | None = None,
    limit: int = 50,
    include_contacts: bool = True,
    only_named: bool = True,
) -> dict:
    """Find points of interest (businesses and public places) in OpenStreetMap.

    WHAT (at least one of categories/tags; default ["restaurants", "cafes"]):
    - categories: friendly names from list_categories(), e.g. ["hotels"], ["pharmacies", "hospitals"], ["atms"].
    - tags: raw OSM tag filters "key=value", "key=v1|v2" or "key" (any value), e.g. ["cuisine=thai"], ["shop=bicycle"].
      Several categories/tags are OR-ed together.
    - name_contains: optional case-insensitive substring filter on the name, e.g. "Starbucks".
    - only_named: skip POIs without name/brand/operator (default true).

    WHERE (exactly one; priority lat/lon > bbox > area):
    - lat + lon + radius_m (10..20000 m): results sorted by distance, each with distance_m. In dense areas the
      server samples up to 5*limit matches inside the circle and returns the nearest of those, so use a
      smaller radius when you need the strictly closest places.
    - bbox: "south,west,north,east" in degrees, e.g. "9.40,99.90,9.60,100.10".
    - area: place name geocoded with Nominatim, e.g. "Koh Samui", "Kreuzberg, Berlin". Uses the exact
      administrative polygon when OSM has one, otherwise its bounding box.
    Areas/bboxes larger than 25,000 km² are rejected (use a smaller region).

    limit: 1..200 (default 50). include_contacts: add phone, website, email, facebook, instagram when tagged.

    Returns {attribution, license, scope, count, truncated, places:[...]}. Each place has (only fields that are
    tagged in OSM): name, nameEn, category, primaryTag (e.g. "amenity=restaurant"), lat, lon, street,
    housenumber, city, district, province, postcode, country, fullAddress, openingHours (raw OSM syntax),
    phone, website, email, cuisine, brand, operator, wheelchair, stars, wikidata, osmType, osmId, osmUrl.
    OSM is community data: many POIs lack address/hours/contacts. Any use must credit
    "© OpenStreetMap contributors" (ODbL). One Overpass query per call, ~25 s server timeout.
    """
    categories = categories or []
    tags = tags or []
    if not categories and not tags:
        categories = ["restaurants", "cafes"]
    try:
        filters = build_filters(categories, tags)
        limit = max(1, min(int(limit), MAX_LIMIT))
        area_id, scope, desc, center = None, "", "", None
        if lat is not None or lon is not None:
            if lat is None or lon is None:
                raise ValueError("Give both lat and lon for a radius search.")
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise ValueError("lat must be -90..90 and lon -180..180.")
            radius_m = max(10, min(int(radius_m), MAX_RADIUS_M))
            # bbox queries are much cheaper for Overpass than (around:...); exact circle is applied afterwards
            dlat = radius_m / 111320
            dlon = radius_m / (111320 * max(math.cos(math.radians(lat)), 0.01))
            scope = f"({max(lat - dlat, -90):.6f},{max(lon - dlon, -180):.6f},{min(lat + dlat, 90):.6f},{min(lon + dlon, 180):.6f})"
            desc, center = f"{radius_m} m around {lat},{lon}", (lat, lon)
        else:
            if bbox:
                box = _parse_bbox(bbox)
                desc = f"bbox {box}"
            elif area:
                area_id, box, label = await _resolve_area(area.strip())
                desc = f"area '{label}'" + ("" if area_id else " (bounding box)")
            else:
                raise ValueError("Set one of: area (place name), bbox, or lat+lon (+radius_m).")
            km2 = bbox_km2(*box)
            if km2 > MAX_AREA_KM2:
                raise ValueError(f"Area too large (~{km2:,.0f} km² > {MAX_AREA_KM2:,} km²). Use a city/district, a smaller bbox or lat+lon+radius.")
            scope = f"({box[0]},{box[1]},{box[2]},{box[3]})"
        # radius search: fetch extra so we can return the nearest ones
        fetch = min(limit * 5, 1000) if center else limit + 1
        ql = poi_query(filters, scope, (name_contains or "").strip(), fetch, OVERPASS_QL_TIMEOUT, area_id, only_named)
        d = await overpass(ql)
    except (ValueError, OverpassError, RuntimeError) as e:
        return {"error": str(e), "attribution": ATTRIBUTION}

    places, seen = [], set()
    for el in d.get("elements", []):
        key = (el.get("type"), el.get("id"))
        if key in seen or not el.get("tags"):
            continue
        seen.add(key)
        row = to_row(el, filters, include_contacts, True, False)
        row.pop("attribution", None)
        if center:
            if row.get("lat") is None:
                continue
            row["distance_m"] = round(haversine_m(center[0], center[1], row["lat"], row["lon"]))
            if row["distance_m"] > radius_m:
                continue
        places.append(_compact(row))
    if center:
        places.sort(key=lambda r: r.get("distance_m", 1e12))
    truncated = len(places) > limit
    return {"attribution": ATTRIBUTION, "license": LICENSE, "scope": desc, "count": min(len(places), limit),
            "truncated": truncated, "places": places[:limit]}


@mcp.tool()
async def geocode(query: str, limit: int = 5, country_codes: str | None = None) -> dict:
    """Geocode a free-form address or place name with OSM Nominatim.

    query: e.g. "Chaweng Beach, Koh Samui", "10 Downing Street, London", "Eiffel Tower".
    limit: 1..10 (default 5). country_codes: optional ISO 3166-1 alpha-2 list, comma-separated, e.g. "th" or "de,at".
    Returns {attribution, results:[{name, displayName, lat, lon, category ("place=city"), addressType, osmType,
    osmId, bbox [south,west,north,east], address {road, city, postcode, country, country_code, ...}, importance}]}
    best match first. Rate-limited to 1 request/second process-wide (Nominatim policy) and cached; do not use
    for bulk geocoding or autocomplete.
    """
    q = (query or "").strip()
    if not q:
        return {"error": "query is empty", "attribution": ATTRIBUTION}
    params = {"q": q[:300], "limit": max(1, min(int(limit), 10)), "addressdetails": 1}
    if country_codes:
        params["countrycodes"] = country_codes.replace(" ", "").lower()
    try:
        res = await nominatim("search", params)
    except RuntimeError as e:
        return {"error": str(e), "attribution": ATTRIBUTION}
    return {"attribution": ATTRIBUTION, "license": LICENSE, "query": q, "results": [_place(r) for r in res]}


@mcp.tool()
async def reverse_geocode(lat: float, lon: float, zoom: int = 18) -> dict:
    """Reverse-geocode coordinates to the nearest OSM address/place with Nominatim.

    zoom controls detail: 18 = building/address (default), 16 = street, 14 = suburb, 10 = city, 3 = country.
    Returns {attribution, result:{name, displayName, lat, lon, category, addressType, osmType, osmId, bbox, address}}
    or an error if nothing is there (e.g. open sea). Rate-limited to 1 request/second (Nominatim policy).
    """
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return {"error": "lat must be -90..90 and lon -180..180", "attribution": ATTRIBUTION}
    try:
        r = await nominatim("reverse", {"lat": lat, "lon": lon, "zoom": max(3, min(int(zoom), 18)), "addressdetails": 1})
    except RuntimeError as e:
        return {"error": str(e), "attribution": ATTRIBUTION}
    if not isinstance(r, dict) or r.get("error"):
        return {"error": (r or {}).get("error", "nothing found") if isinstance(r, dict) else "nothing found", "attribution": ATTRIBUTION}
    return {"attribution": ATTRIBUTION, "license": LICENSE, "result": _place(r)}


@mcp.tool()
async def list_categories() -> dict:
    """List the friendly POI categories accepted by find_places(categories=...) and the OSM tags each maps to,
    e.g. "pharmacies" -> ["amenity=pharmacy", "healthcare=pharmacy"]. For anything not listed, pass raw OSM tags
    to find_places(tags=[...]) - see https://wiki.openstreetmap.org/wiki/Map_features."""
    return {"categories": {c: [f"{k}={'|'.join(v) if v != ['*'] else '*'}" for k, v in f] for c, f in CATEGORIES.items()},
            "attribution": ATTRIBUTION}


from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "healthy"})


if __name__ == "__main__":
    mcp.run(transport=os.environ.get("MCP_TRANSPORT", "streamable-http"))
