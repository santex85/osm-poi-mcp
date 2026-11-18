# OSM POI MCP server

MCP server that lets an AI agent find businesses and public places from **OpenStreetMap**: restaurants, cafes, hotels, shops, pharmacies, ATMs, EV chargers and 40+ other categories, or any raw OSM tag. It can search by place name, bounding box or point plus radius, and it also geocodes and reverse-geocodes addresses. No API key is needed.

It is ported from the `osm-poi-extractor` Apify actor. The category map, query builder and row format are the same, but there is no Apify dependency.

Data © OpenStreetMap contributors, ODbL 1.0.

## Tools

| Tool | What it does |
|---|---|
| `find_places` | POIs for `categories` and/or raw `tags`. Search where: `area` name, `bbox`, or `lat`+`lon`+`radius_m`. Optional `name_contains`. Returns name, category, coordinates, address, opening hours, phone/website/email (when tagged), brand, cuisine and an OSM link. |
| `geocode` | Turns an address or place name into coordinates, a bounding box and a structured address (Nominatim). |
| `reverse_geocode` | Turns coordinates into the nearest address or place (Nominatim). |
| `list_categories` | Lists the friendly category names and the OSM tags each one maps to. |

Example calls (arguments as JSON):

```jsonc
// find_places: cafes within 2 km of Chaweng, nearest first
{"categories": ["cafes"], "lat": 9.5357, "lon": 100.0627, "radius_m": 2000, "limit": 5}
// find_places: Starbucks in Lisbon (area name resolved through Nominatim)
{"categories": ["cafes"], "area": "Lisbon", "name_contains": "Starbucks", "limit": 5}
// find_places: Thai restaurants in a bbox (south,west,north,east)
{"tags": ["cuisine=thai"], "bbox": "52.49,13.38,52.52,13.43", "limit": 10}
// geocode
{"query": "Chaweng Beach, Koh Samui", "limit": 3, "country_codes": "th"}
// reverse_geocode
{"lat": 9.5357, "lon": 100.0627, "zoom": 18}
```

A `find_places` result looks like this (fields are only present when tagged in OSM):

```json
{"attribution": "© OpenStreetMap contributors, ODbL 1.0 (https://www.openstreetmap.org/copyright)",
 "license": "ODbL 1.0 - https://opendatacommons.org/licenses/odbl/1-0/",
 "scope": "area 'Lisbon, Portugal'", "count": 5, "truncated": true,
 "places": [{"name": "Starbucks", "category": "cafes", "primaryTag": "amenity=cafe", "lat": 38.6975196, "lon": -9.2036756,
             "fullAddress": "110 Rua de Belém, Lisboa, 1300-085", "brand": "Starbucks", "openingHours": "...",
             "osmUrl": "https://www.openstreetmap.org/node/477853299"}]}
```

## Run

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
PORT=8093 .venv/bin/python src/server.py                     # streamable-http on 0.0.0.0:8093/mcp
MCP_TRANSPORT=stdio .venv/bin/python src/server.py           # stdio
```

Environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `HOST`, `PORT` | `0.0.0.0`, `8080` | Address the HTTP server listens on |
| `MCP_TRANSPORT` | `streamable-http` | Transport (`stdio`, `sse` or `streamable-http`) |
| `OVERPASS_ENDPOINTS` | Private.coffee, then VK Maps | Comma-separated Overpass interpreter URLs |
| `NOMINATIM_URL` | `https://nominatim.openstreetmap.org` | Use your own Nominatim instance |
| `OSM_USER_AGENT` | `osm-poi-mcp/0.1 (...)` | Identifying User-Agent. Set one with your app's contact URL for production. |

## Limits

- `find_places` returns at most 200 results (default 50) and reports `truncated` when more matched.
- Radius search is capped at 20 km. In dense places it samples up to 5×limit matches inside the circle and returns the nearest of those.
- Areas and bboxes larger than 25,000 km² are rejected.
- Each call makes one Overpass query with a 25 s server timeout. Retries and endpoint switching are capped at about 75 s in total. Responses larger than 8 MB are refused.
- Geocoding takes the first hit. Add the country or region to the name, or pass a bbox, when a name is ambiguous.

## Data sources, licences and usage policies

- **OpenStreetMap data** is © OpenStreetMap contributors and licensed under the [Open Database License 1.0](https://opendatacommons.org/licenses/odbl/1-0/). Anywhere you show or use the data, credit "© OpenStreetMap contributors" with a link to https://www.openstreetmap.org/copyright. If you publicly distribute a derivative *database* built from it, that database must be ODbL too (share-alike). Every tool response carries an `attribution` field.
- **Overpass API.** The server uses public instances that allow general use: [Private.coffee](https://overpass.private.coffee/) by default, with [VK Maps](https://wiki.openstreetmap.org/wiki/Overpass_API#Public_Overpass_API_instances) as fallback. Under Private.coffee's terms the service must not be used for military or warfare projects. `overpass-api.de` is not used by default because it asks commercial and heavy users to go elsewhere. Queries run one at a time per process with a pause between them. Endpoints that hang or rate-limit (429/406) are set aside for 5 minutes. For heavy use, point `OVERPASS_ENDPOINTS` at your own instance.
- **Nominatim** ([usage policy](https://operations.osmfoundation.org/policies/nominatim/)). The server sends at most 1 request per second per process, uses an identifying User-Agent and caches results in memory (500 entries). Bulk geocoding and autocomplete are not allowed on the public instance. For volume, set `NOMINATIM_URL` to your own instance.
- OSM is community data. Coverage of addresses, hours and contacts varies by region. Contact fields describe businesses, but some list a personal phone or email, so use them lawfully (GDPR, PDPA, anti-spam rules).

## Known limitations

- Public Overpass servers are often overloaded and can return 504 or time out for a minute or more. The tool then returns `{"error": ...}` and the agent can retry later. During testing, Private.coffee and overpass-api.de did not answer from this host, and VK Maps answered after a few 504 retries.
- Area resolution uses the administrative polygon when Nominatim returns one (an OSM relation or way). Otherwise it falls back to the place's bounding box.

## Use with Claude Desktop or another MCP client

Local (stdio). Clone the repo, create the venv as above, then add this to `claude_desktop_config.json`
(or your client's MCP config), with the paths changed to where you cloned it:

```json
{
  "mcpServers": {
    "osm-poi": {
      "command": "/path/to/osm-poi-mcp/.venv/bin/python",
      "args": ["/path/to/osm-poi-mcp/src/server.py"],
      "env": {"MCP_TRANSPORT": "stdio"}
    }
  }
}
```

Remote (streamable HTTP). Run `.venv/bin/python src/server.py` on a server and point any client that supports
remote MCP servers at `http://<host>:8080/mcp`. `GET /health` returns `{"status": "healthy"}`.

## License

MIT, see [LICENSE](LICENSE). The data returned by the tools stays under the terms of its sources (see above).
