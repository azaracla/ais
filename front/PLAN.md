# Plan — Clone MarineTraffic

## Phase 1 : Sidebar + Vessel Search ✅
- [x] Sidebar panel (380px, left overlay, collapsible)
- [x] Vessel list with sort (speed/name/type)
- [x] Category filters (chips with counts)
- [x] Search bar with autocomplete (local vessel cache from DuckDB)
- [x] Vessel detail panel (IMO, Call Sign, dimensions, status, track)
- [x] Hover tooltip on vessel icons
- [x] Map layer filtering by category

## Phase 2 : Filters & Layer Controls (P0)
- [ ] Speed range slider
- [ ] Navigation status filter (underway / anchored / moored)
- [ ] Vessel density heatmap at low zoom levels
- [ ] Toggle vessel name labels on map

## Phase 3 : Timeline & Historical Replay (P1)
- [ ] Timeline scrubber bar (play/pause/speed)
- [ ] Animated vessel positions over time
- [ ] Persistent wake/trail for recent positions
- [ ] Multi-vessel selection (shift+click)

## Phase 4 : Port Info & ETA (P2)
- [ ] Port overlay (static GeoJSON)
- [ ] Port call detection from navigation_status
- [ ] ETA display from AIS data
- [ ] Nearby vessels panel

## Phase 5 : Polish & Advanced (P3)
- [ ] Weather overlay (wind/waves via Open-Meteo)
- [ ] Export (CSV/GeoJSON/GPX)
- [ ] Distance measurement tool
- [ ] Favorites / My Fleet (localStorage)
- [ ] Share link
- [ ] Mobile responsive improvements
