import ee
import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from datetime import datetime, timedelta
from cachetools import TTLCache
from threading import Lock

ee.Initialize(project="aal-sentinel")

app = FastAPI(title="Sentinel Satellite Tile Proxy")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_map_cache  = TTLCache(maxsize=50,   ttl=3600 * 3)
_tile_cache = TTLCache(maxsize=5000, ttl=3600 * 3)
_map_lock   = Lock()
_tile_lock  = Lock()


def get_map_id(start: str, end: str, bbox: str | None, sensor: str) -> str:
    key = (start, end, bbox, sensor)
    with _map_lock:
        if key in _map_cache:
            return _map_cache[key]

    geometry = None
    if bbox:
        west, south, east, north = map(float, bbox.split(","))
        geometry = ee.Geometry.Rectangle([west, south, east, north])

    if sensor == "S1":
        col = (ee.ImageCollection("COPERNICUS/S1_GRD")
            .filterDate(start, end)
            .filter(ee.Filter.eq("instrumentMode", "IW"))
            .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH")))

        if geometry:
            col = col.filterBounds(geometry)

        if col.size().getInfo() == 0:
            return ""

        image = col.select("VH").mosaic()

        if geometry:
            image = image.clip(geometry)

        vis = {"min": -25, "max": -5, "bands": ["VH"], "palette": ["000000", "ffffff"]}
    else:
        col = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
            .filterDate(start, end)
            .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 20))
            .sort("system:time_start"))  # ascending → mosaic puts newest on top

        if geometry:
            col = col.filterBounds(geometry)

        if col.size().getInfo() == 0:
            return ""

        image = col.mosaic().divide(10000)
        if geometry:
            image = image.clip(geometry)
        vis = {"min": 0, "max": 0.3, "bands": ["B4", "B3", "B2"]}

    tile_url = image.getMapId(vis)["tile_fetcher"].url_format

    with _map_lock:
        _map_cache[key] = tile_url
    return tile_url


async def fetch_tile(z: int, x: int, y: int, start: str, end: str,
                     bbox: str | None, sensor: str) -> bytes:
    key = (z, x, y, start, end, sensor)
    with _tile_lock:
        if key in _tile_cache:
            return _tile_cache[key]

    tile_url = get_map_id(start, end, bbox, sensor)
    if not tile_url:
        return b""
    url = tile_url.format(x=x, y=y, z=z)
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url)
    content = resp.content

    with _tile_lock:
        _tile_cache[key] = content
    return content


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/map")
def get_map(start: str = "2024-06-01", end: str = "2024-09-01",
            bbox: str | None = None, sensor: str = "S2"):
    geometry = None
    if bbox:
        west, south, east, north = map(float, bbox.split(","))
        geometry = ee.Geometry.Rectangle([west, south, east, north])

    if sensor == "S1":
        col = (ee.ImageCollection("COPERNICUS/S1_GRD")
               .filterDate(start, end)
               .filter(ee.Filter.eq("instrumentMode", "IW"))
               .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV")))
    else:
        col = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
               .filterDate(start, end)
               .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 20)))

    if geometry:
        col = col.filterBounds(geometry)

    timestamps = col.aggregate_array("system:time_start").getInfo()
    dates = sorted(set([
        datetime.utcfromtimestamp(t / 1000).strftime("%Y-%m-%d") for t in timestamps
    ]))
    return {"dates": dates, "count": len(dates)}


@app.get("/tiles/{z}/{x}/{y}")
async def proxy_tile(z: int, x: int, y: int,
                     start: str = "2024-06-01", end: str = "2024-09-01",
                     bbox: str | None = None, sensor: str = "S2"):
    content = await fetch_tile(z, x, y, start, end, bbox, sensor)
    if not content:
        return Response(content=b"", media_type="image/png", status_code=404)
    return Response(content=content, media_type="image/png")


@app.get("/tiles-by-date/{z}/{x}/{y}")
async def proxy_tile_by_date(z: int, x: int, y: int,
                              date: str,
                              bbox: str | None = None,
                              sensor: str = "S2"):
    end = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
    content = await fetch_tile(z, x, y, date, end, bbox, sensor)
    if not content:
        return Response(content=b"", media_type="image/png", status_code=404)
    return Response(content=content, media_type="image/png")


@app.get("/available-dates")
def available_dates(lat: float, lon: float, sensor: str = "S2",
                    start: str = "2015-01-01", end: str = "2030-01-01"):
    point = ee.Geometry.Point([lon, lat])

    if sensor == "S1":
        col = (ee.ImageCollection("COPERNICUS/S1_GRD")
               .filterBounds(point)
               .filterDate(start, end)
               .filter(ee.Filter.eq("instrumentMode", "IW"))
               .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV")))
    else:
        col = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
               .filterBounds(point)
               .filterDate(start, end)
               .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 20)))

    timestamps = col.aggregate_array("system:time_start").getInfo()
    dates = sorted(set([
        datetime.utcfromtimestamp(t / 1000).strftime("%Y-%m-%d") for t in timestamps
    ]))
    return {"dates": dates, "count": len(dates)}


@app.get("/acquisition-time")
def acquisition_time(date: str, bbox: str | None = None, sensor: str = "S2"):
    geometry = None
    if bbox:
        west, south, east, north = map(float, bbox.split(","))
        geometry = ee.Geometry.Rectangle([west, south, east, north])

    start = date
    end = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

    if sensor == "S1":
        col = (ee.ImageCollection("COPERNICUS/S1_GRD")
            .filterDate(start, end)
            .filter(ee.Filter.eq("instrumentMode", "IW"))
            .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))
            .sort("system:time_start", False))
    else:
        col = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
            .filterDate(start, end)
            .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 20))
            .sort("system:time_start", False))

    if geometry:
        col = col.filterBounds(geometry)

    try:
        if col.size().getInfo() == 0:
            return {"acquisition_time": None}
        image = col.first()
        ts = image.get("system:time_start").getInfo()
        if ts is None:
            return {"acquisition_time": None}
        dt = datetime.utcfromtimestamp(ts / 1000)
        return {"acquisition_time": dt.strftime("%Y-%m-%d %H:%M:%S UTC")}
    except Exception:
        return {"acquisition_time": None}


@app.get("/scenes")
def scenes(date: str, bbox: str | None = None, sensor: str = "S2"):
    geometry = None
    if bbox:
        west, south, east, north = map(float, bbox.split(","))
        geometry = ee.Geometry.Rectangle([west, south, east, north])

    start = date
    end = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")

    if sensor == "S1":
        col = (ee.ImageCollection("COPERNICUS/S1_GRD")
            .filterDate(start, end)
            .filter(ee.Filter.eq("instrumentMode", "IW")))
    else:
        col = (ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
            .filterDate(start, end)
            .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 20)))

    if geometry:
        col = col.filterBounds(geometry)

    try:
        fc = ee.FeatureCollection(
            col.limit(1000).map(lambda img: ee.Feature(
                img.geometry(),
                {"acquisition_time": img.get("system:time_start")},
            ))
        )
        raw = fc.getInfo()
        if not raw or "features" not in raw:
            return {"type": "FeatureCollection", "features": []}

        features = []
        for f in raw["features"]:
            ts = f.get("properties", {}).get("acquisition_time")
            if not ts:
                continue
            dt = datetime.utcfromtimestamp(ts / 1000)
            features.append({
                "type": "Feature",
                "geometry": f.get("geometry"),
                "properties": {
                    "acquisition_time": dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
                    "sensor": sensor,
                },
            })
        return {"type": "FeatureCollection", "features": features}
    except Exception as e:
        return {"type": "FeatureCollection", "features": [], "error": str(e)}
