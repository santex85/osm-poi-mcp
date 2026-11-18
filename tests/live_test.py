"""Live smoke test against public OSM services (Nominatim, Overpass). Run: .venv/bin/python tests/live_test.py

Public Overpass instances are often overloaded, so find_places may need a retry later.
"""
import asyncio, json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import server  # noqa: E402


def show(name, r, ok):
    s = json.dumps(r, ensure_ascii=False, default=str)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {len(s)} bytes | {s[:500]}\n")
    return bool(ok)


async def main():
    res = []
    tools = await server.mcp.list_tools()
    res.append(show("list_tools", [t.name for t in tools], len(tools) == 4))
    r = await server.list_categories()
    res.append(show("list_categories", r, "cafes" in json.dumps(r)))
    r = await server.geocode("Chaweng Beach, Koh Samui", limit=2, country_codes="th")
    res.append(show("geocode", r, not r.get("error") and json.dumps(r).count("lat") > 0))
    r = await server.reverse_geocode(9.5357, 100.0627)
    res.append(show("reverse_geocode", r, not r.get("error")))
    r = await server.find_places(categories=["cafes"], lat=9.5357, lon=100.0627, radius_m=2000, limit=5)
    res.append(show("find_places", r, not r.get("error") and r.get("count", 0) > 0))
    print(f"{sum(res)}/{len(res)} passed")
    sys.exit(0 if all(res) else 1)


asyncio.run(main())
