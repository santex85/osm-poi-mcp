"""OSM helpers: category -> tag mapping, Overpass QL builder, element -> output row (ported from the osm-poi-extractor actor)."""
import math
import re

ATTRIBUTION = "© OpenStreetMap contributors, ODbL 1.0 (https://www.openstreetmap.org/copyright)"

# friendly category -> list of (key, [values]) ; values == ["*"] means "key exists"
CATEGORIES: dict[str, list[tuple[str, list[str]]]] = {
    "restaurants": [("amenity", ["restaurant"])],
    "cafes": [("amenity", ["cafe"])],
    "fast_food": [("amenity", ["fast_food", "food_court"])],
    "bars_pubs": [("amenity", ["bar", "pub", "biergarten"])],
    "nightclubs": [("amenity", ["nightclub"])],
    "hotels": [("tourism", ["hotel", "motel"])],
    "hostels_guesthouses": [("tourism", ["hostel", "guest_house", "apartment", "chalet"])],
    "resorts": [("leisure", ["resort"])],
    "supermarkets": [("shop", ["supermarket", "convenience", "grocery"])],
    "shops": [("shop", ["*"])],
    "bakeries": [("shop", ["bakery", "pastry"])],
    "clothes_shops": [("shop", ["clothes", "shoes", "boutique"])],
    "electronics_shops": [("shop", ["electronics", "mobile_phone", "computer"])],
    "marketplaces": [("amenity", ["marketplace"])],
    "pharmacies": [("amenity", ["pharmacy"]), ("healthcare", ["pharmacy"])],
    "hospitals": [("amenity", ["hospital"])],
    "clinics_doctors": [("amenity", ["clinic", "doctors"])],
    "dentists": [("amenity", ["dentist"])],
    "veterinary": [("amenity", ["veterinary"])],
    "schools": [("amenity", ["school"])],
    "kindergartens": [("amenity", ["kindergarten", "childcare"])],
    "universities_colleges": [("amenity", ["university", "college"])],
    "banks": [("amenity", ["bank"])],
    "atms": [("amenity", ["atm"])],
    "currency_exchange": [("amenity", ["bureau_de_change"])],
    "fuel": [("amenity", ["fuel"])],
    "ev_charging": [("amenity", ["charging_station"])],
    "parking": [("amenity", ["parking"])],
    "car_rental": [("amenity", ["car_rental"])],
    "car_dealers": [("shop", ["car", "motorcycle"])],
    "car_repair": [("shop", ["car_repair", "tyres", "motorcycle_repair"])],
    "tourist_attractions": [("tourism", ["attraction", "viewpoint", "theme_park", "zoo", "aquarium"])],
    "museums_galleries": [("tourism", ["museum", "gallery"])],
    "beaches": [("natural", ["beach"])],
    "gyms_fitness": [("leisure", ["fitness_centre", "sports_centre"])],
    "beauty_spa": [("shop", ["beauty", "hairdresser", "massage", "cosmetics"]), ("amenity", ["spa"]), ("leisure", ["spa"])],
    "dive_centres": [("amenity", ["dive_centre"]), ("shop", ["scuba_diving"])],
    "coworking": [("amenity", ["coworking_space"]), ("office", ["coworking"])],
    "offices": [("office", ["*"])],
    "real_estate_agencies": [("office", ["estate_agent"])],
    "places_of_worship": [("amenity", ["place_of_worship"])],
    "post_offices": [("amenity", ["post_office"])],
    "police": [("amenity", ["police"])],
    "toilets": [("amenity", ["toilets"])],
    "cinemas_theatres": [("amenity", ["cinema", "theatre", "arts_centre"])],
}

CONTACT_KEY_RE = re.compile(r"(^|:)(phone|mobile|fax|email|website|url|facebook|instagram|twitter|tiktok|whatsapp|telegram|line|vk|linkedin|youtube)(:|$)|^contact:")

KEY_RE = re.compile(r"^[A-Za-z0-9_:.\-]+$")


def esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


def parse_custom_tag(spec: str) -> tuple[str, list[str]]:
    """'cuisine=thai' | 'cuisine=thai|seafood' | 'craft' | 'craft=*' -> (key, values)."""
    spec = spec.strip()
    if "=" in spec:
        k, v = spec.split("=", 1)
    else:
        k, v = spec, "*"
    k, v = k.strip(), v.strip()
    if not k or not KEY_RE.match(k):
        raise ValueError(f"Invalid custom tag key in '{spec}'. Use key=value, key=v1|v2 or key")
    values = [x.strip() for x in v.split("|") if x.strip()] or ["*"]
    return k, values


def build_filters(categories: list[str], custom_tags: list[str]) -> list[tuple[str, str, list[str]]]:
    """-> list of (label, key, values)."""
    out = []
    for c in categories:
        if c not in CATEGORIES:
            raise ValueError(f"Unknown category '{c}'. Known: {', '.join(CATEGORIES)}")
        for k, vals in CATEGORIES[c]:
            out.append((c, k, vals))
    for spec in custom_tags:
        if spec and spec.strip():
            k, vals = parse_custom_tag(spec)
            out.append((f"custom:{spec.strip()}", k, vals))
    return out


def tag_selector(key: str, values: list[str]) -> str:
    if values == ["*"]:
        return f'["{esc(key)}"]'
    if len(values) == 1:
        return f'["{esc(key)}"="{esc(values[0])}"]'
    alt = "|".join(re.escape(v) for v in values)
    return f'["{esc(key)}"~"^({esc(alt)})$"]'


def poi_query(filters, scope: str, name_contains: str, limit: int, timeout: int, area_id: int | None, only_named: bool = True) -> str:
    """scope: '(s,w,n,e)' bbox or '(around:r,lat,lon)' or '' ; area_id adds (area.a)."""
    # only_named: keep POIs identified by name, brand or operator (ATMs/chargers often only have operator/brand)
    if name_contains:
        name_sels = [f'["name"~"{esc(re.escape(name_contains))}",i]']
    elif only_named:
        name_sels = ['["name"]', '["brand"]', '["operator"]']
    else:
        name_sels = [""]
    area_sel = "(area.a)" if area_id else ""
    # merge identical keys into one selector to keep the query short
    merged: dict[str, set[str]] = {}
    for _, k, vals in filters:
        if vals == ["*"] or merged.get(k) == {"*"}:
            merged[k] = {"*"}
        else:
            merged.setdefault(k, set()).update(vals)
    lines = [f"  nwr{tag_selector(k, sorted(v))}{ns}{area_sel}{scope};" for k, v in merged.items() for ns in name_sels]
    head = f"[out:json][timeout:{timeout}][maxsize:134217728];\n"
    if area_id:
        head += f"area({area_id})->.a;\n"
    return head + "(\n" + "\n".join(lines) + "\n);\nout tags center " + str(int(limit)) + ";"


def bbox_km2(s, w, n, e) -> float:
    lat = math.radians((s + n) / 2)
    return abs(n - s) * 111.32 * abs(e - w) * 111.32 * max(math.cos(lat), 0.01)


def match_category(tags: dict, filters) -> str:
    for label, k, vals in filters:
        if k in tags and (vals == ["*"] or tags[k] in vals):
            return label
    return ""


def primary_tag(tags: dict) -> str:
    for k in ("amenity", "shop", "tourism", "leisure", "office", "healthcare", "craft", "natural", "historic", "man_made", "sport"):
        if k in tags:
            return f"{k}={tags[k]}"
    return ""


def to_row(el: dict, filters, include_contacts: bool, include_hours: bool, include_all_tags: bool) -> dict:
    t = el.get("tags") or {}
    if el["type"] == "node":
        lat, lon = el.get("lat"), el.get("lon")
    else:
        c = el.get("center") or {}
        lat, lon = c.get("lat"), c.get("lon")
    addr = {
        "housenumber": t.get("addr:housenumber"),
        "street": t.get("addr:street"),
        "city": t.get("addr:city") or t.get("addr:town") or t.get("addr:village"),
        "district": t.get("addr:district") or t.get("addr:subdistrict") or t.get("addr:suburb"),
        "province": t.get("addr:province") or t.get("addr:state"),
        "postcode": t.get("addr:postcode"),
        "country": t.get("addr:country"),
    }
    full = ", ".join(x for x in [
        " ".join(x for x in [addr["housenumber"], addr["street"]] if x) or None,
        addr["district"], addr["city"], addr["province"], addr["postcode"], addr["country"],
    ] if x)
    row = {
        "osmType": el["type"],
        "osmId": el["id"],
        "name": t.get("name"),
        "nameEn": t.get("name:en"),
        "category": match_category(t, filters),
        "primaryTag": primary_tag(t),
        "lat": lat,
        "lon": lon,
        "street": addr["street"],
        "housenumber": addr["housenumber"],
        "city": addr["city"],
        "district": addr["district"],
        "province": addr["province"],
        "postcode": addr["postcode"],
        "country": addr["country"],
        "fullAddress": full or None,
        "cuisine": t.get("cuisine"),
        "brand": t.get("brand"),
        "brandWikidata": t.get("brand:wikidata"),
        "operator": t.get("operator"),
        "wheelchair": t.get("wheelchair"),
        "outdoorSeating": t.get("outdoor_seating"),
        "takeaway": t.get("takeaway"),
        "delivery": t.get("delivery"),
        "internetAccess": t.get("internet_access"),
        "stars": t.get("stars"),
        "wikidata": t.get("wikidata"),
    }
    if include_hours:
        row["openingHours"] = t.get("opening_hours")
    if include_contacts:
        row["website"] = t.get("website") or t.get("contact:website") or t.get("url")
        row["phone"] = t.get("phone") or t.get("contact:phone") or t.get("contact:mobile")
        row["email"] = t.get("email") or t.get("contact:email")
        row["facebook"] = t.get("contact:facebook") or t.get("facebook")
        row["instagram"] = t.get("contact:instagram") or t.get("instagram")
    if include_all_tags:
        row["tags"] = {k: v for k, v in t.items() if include_contacts or not CONTACT_KEY_RE.search(k)}
    row["osmUrl"] = f"https://www.openstreetmap.org/{el['type']}/{el['id']}"
    row["attribution"] = ATTRIBUTION
    return row
