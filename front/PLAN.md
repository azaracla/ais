# Plan — Clone MarineTraffic

## Phase 1 : Sidebar + Vessel Search ✅
- [x] Sidebar panel (380px, left overlay, collapsible)
- [x] Vessel list with sort (speed/name/type)
- [x] Category filters (chips with counts)
- [x] Search bar with autocomplete (local vessel cache from DuckDB)
- [x] Vessel detail panel (IMO, Call Sign, dimensions, status, track)
- [x] Hover tooltip on vessel icons
- [x] Map layer filtering by category

## Phase 2 : Filters & Layer Controls ✅
- [x] Speed range slider (dual range, client-side filtering)
- [x] Vessel name labels toggle on map
- [x] Navigation status filter (underway / anchored / moored)
- [ ] Vessel density heatmap at low zoom levels
- [x] Layout fixes: top bar, legend, badges shift when sidebar open

## Phase 3 : Timeline & Historical Replay (P1) ✅
- [x] Timeline scrubber bar (play/pause/speed)
- [x] Animated vessel positions over time
- [x] Persistent wake/trail for recent positions
- [x] Multi-vessel selection (shift+click)

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
