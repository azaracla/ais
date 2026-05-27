/**
 * AIS Ship Visualizer - Main Application
 * WebGL visualization of AIS ship data using DuckDB-WASM
 */

import { 
  initDuckDB, 
  queryLastPositions,
  getStats,
  getTimeRange 
} from './duckdb.js';
import {
  createTimeline,
  createSearch,
  createInfobox,
  createInfoPanel,
  createLoadingIndicator,
  createKeyboardShortcuts,
  fetchAvailableTimeRange
} from './ui.js';

// State
let currentData = [];
let isQuerying = false;
let map;
let markers = L.layerGroup();

// Main application
class AISVisualizer {
  constructor() {
    this.appContainer = document.getElementById('app');
    this.infoContainer = document.getElementById('info');
    
    // Initialize all components
    this.init();
  }

  getShipColor(shipType) {
    if (!shipType) return '#9e9e9e'; // Unknown (Grey)
    if (shipType >= 70 && shipType <= 79) return '#4caf50'; // Cargo (Green)
    if (shipType >= 80 && shipType <= 89) return '#f44336'; // Tanker (Red)
    if (shipType >= 60 && shipType <= 69) return '#2196f3'; // Passenger (Blue)
    if (shipType >= 40 && shipType <= 49) return '#ffeb3b'; // High Speed (Yellow)
    if (shipType === 30) return '#ff9800'; // Fishing (Orange)
    if (shipType === 36 || shipType === 37) return '#e91e63'; // Pleasure/Sailing (Pink)
    if (shipType === 31 || shipType === 32 || shipType === 52) return '#00bcd4'; // Tug/Pilot (Cyan)
    if (shipType === 35) return '#607d8b'; // Military (Blue Grey)
    return '#9e9e9e';
  }

  createShipIcon(ship) {
    const color = this.getShipColor(ship.ship_type);
    const rotation = ship.cog || 0;
    const size = 18;
    const isStationary = !ship.sog || ship.sog < 0.5;
    
    let svg;
    if (isStationary) {
      // Circle for stationary ships
      svg = `
        <svg width="${size}" height="${size}" viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg">
          <circle cx="50" cy="50" r="40" fill="${color}" stroke="#ffffff" stroke-width="10" />
          <circle cx="50" cy="50" r="10" fill="#ffffff" />
        </svg>
      `;
    } else {
      // Sharp triangle for moving ships
      svg = `
        <svg width="${size}" height="${size}" viewBox="0 0 100 100" xmlns="http://www.w3.org/2000/svg" style="transform: rotate(${rotation}deg);">
          <path d="M50 0 L90 100 L50 80 L10 100 Z" fill="${color}" stroke="#ffffff" stroke-width="8" stroke-linejoin="round" />
        </svg>
      `;
    }
    
    return L.divIcon({
      html: svg,
      className: 'ship-marker-icon',
      iconSize: [size, size],
      iconAnchor: [size/2, size/2]
    });
  }

  getShipTypeName(type) {
    const types = {
      30: 'Pêche',
      31: 'Remorqueur',
      32: 'Remorqueur',
      35: 'Militaire',
      36: 'Plaisance',
      37: 'Voilier',
      40: 'Grande vitesse',
      52: 'Pilotage',
      60: 'Passagers',
      70: 'Cargo',
      80: 'Pétrolier',
    };
    if (type >= 70 && type <= 79) return 'Cargo';
    if (type >= 80 && type <= 89) return 'Pétrolier';
    if (type >= 60 && type <= 69) return 'Passagers';
    if (type >= 40 && type <= 49) return 'Grande vitesse';
    return types[type] || `Autre (${type || 'inconnu'})`;
  }

  async init() {
    console.log('Initializing AIS Visualizer...');
    
    // Create loading indicator
    this.loading = createLoadingIndicator();
    this.loading.show('Initialisation de DuckDB WASM...');
    
    try {
      // Initialize DuckDB with progress callback
      await initDuckDB((progress) => {
        this.loading.update(progress);
      });
      
      this.loading.update(70);
      
      // Initialize Leaflet map with Canvas renderer for performance
      this.appContainer.innerHTML = '<div id="map" style="width: 100%; height: 100%;"></div>';
      map = L.map('map', {
        renderer: L.canvas()
      }).setView([0, 0], 2);
      
      L.tileLayer('https://{s}.basemaps.cartocdn.com/rastertiles/voyager/{z}/{x}/{y}{r}.png', {
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
        subdomains: 'abcd',
        maxZoom: 20
      }).addTo(map);
      markers.addTo(map);
      
      this.loading.update(85);
      
      // Create UI components
      this.createUI();
      
      // Get available time range and initial data
      await this.loadInitialData();
      
      // Hide loading screen
      this.loading.hide();
      
      console.log('AIS Visualizer ready');
      
    } catch (error) {
      console.error('Initialization failed:', error);
      this.loading.update('Erreur de chargement');
      // Don't hide loading screen on error - show error message
      const loadingEl = document.getElementById('loading');
      if (loadingEl) {
        loadingEl.innerHTML = `
          <div class="spinner" style="border-color: #ff4444"></div>
          <p style="color: #ff4444">Erreur: ${error.message}</p>
        `;
      }
    }
  }

  createUI() {
    // Single Datetime Picker
    const now = new Date();
    const defaultTime = now.toISOString().slice(0, 16); // format for datetime-local
    
    this.timeline = createTimeline(document.body, defaultTime, (dateTime) => {
      this.onDateTimeChange(dateTime);
    });
    
    // Search
    this.search = createSearch(document.body, (ship) => {
      this.onShipSearch(ship);
    });
    
    // Infobox
    this.infobox = createInfobox(document.body);
    
    // Info Panel
    this.infoPanel = createInfoPanel(this.infoContainer);
    
    // Keyboard shortcuts
    createKeyboardShortcuts({
      onEscape: () => this.infobox.hide(),
      onReset: () => this.resetView(),
      onFit: () => this.fitToData()
    });
    
    // Click on map to show ship info
    map.on('click', (e) => {
      const ship = this.findShipAtPosition(e.latlng.lat, e.latlng.lng);
      if (ship) {
        this.infobox.show(ship, e.originalEvent.clientX, e.originalEvent.clientY);
      }
    });

    // Viewport-based querying: Re-query when map moves or zooms
    let moveTimeout;
    map.on('moveend', () => {
      clearTimeout(moveTimeout);
      moveTimeout = setTimeout(() => {
        this.queryAndUpdateData(null, true); // Silent update for map movements
      }, 500); // Debounce queries
    });
  }

  findShipAtPosition(lat, lng) {
    const threshold = 0.01; // ~1km at equator
    return currentData.find(ship => 
      Math.abs(ship.lat - lat) < threshold && 
      Math.abs(ship.lon - lng) < threshold
    );
  }

  async loadInitialData() {
    this.loading.update(90);
    
    // Use current time as default
    const now = new Date();
    const dateTime = now.toISOString().slice(0, 16);
    
    // Update timeline
    this.timeline.updateDateTime(dateTime);
  }

  async onDateTimeChange(dateTime) {
    console.log('Date time changed:', dateTime);
    
    // Calculate 1 hour look-back
    const end = new Date(dateTime);
    const start = new Date(end.getTime() - 60 * 60 * 1000);
    
    const range = {
      start: start.toISOString().slice(0, 19),
      end: end.toISOString().slice(0, 19)
    };
    
    // Update info panel
    this.infoPanel.updateTime(range);
    
    // Query new data
    await this.queryAndUpdateData(range);
  }

  async onShipSearch(ship) {
    console.log('Ship selected:', ship);
    
    if (ship.mmsi) {
      // Find the ship in current data
      const shipData = currentData.find(s => s.mmsi === ship.mmsi);
      
      if (shipData) {
        this.zoomToShip(shipData);
        // Show infobox at center of map
        const center = map.getCenter();
        this.infobox.show(shipData, window.innerWidth / 2, window.innerHeight / 2);
      }
    }
  }

  async queryAndUpdateData(providedRange = null, silent = false) {
    if (isQuerying) return;
    
    // Get current map zoom and bounds
    const zoom = map.getZoom();
    const mapBounds = map.getBounds();
    const bounds = {
      north: mapBounds.getNorth(),
      south: mapBounds.getSouth(),
      east: mapBounds.getEast(),
      west: mapBounds.getWest()
    };

    // Adapt limit based on zoom
    let limit = 1000;
    if (zoom >= 5) limit = 3000;
    if (zoom >= 7) limit = 10000;
    if (zoom >= 10) limit = 50000;

    // Get time range from timeline if not provided
    let range = providedRange;
    if (!range) {
      const dateTime = this.timeline.getDateTime();
      const end = new Date(dateTime);
      const start = new Date(end.getTime() - 60 * 60 * 1000);
      range = {
        start: start.toISOString().slice(0, 19),
        end: end.toISOString().slice(0, 19)
      };
    }

    isQuerying = true;
    if (!silent) {
      this.loading.show('Mise à jour de la zone...');
    }
    
    try {
      const startTime = performance.now();
      
      // Query with spatial bounds
      const data = await queryLastPositions(range, {
        limit: limit, 
        bounds: bounds
      });
      
      const duration = performance.now() - startTime;
      console.log(`Loaded ${data.length} ships for zoom ${zoom} in ${duration.toFixed(0)}ms (${silent ? 'background' : 'foreground'})`);
      
      // OPTIMIZATION: Detach layer before bulk update to avoid expensive re-paints
      markers.remove();
      markers.clearLayers();
      
      // Adaptive rendering: Circles for low zoom, Icons for high zoom
      const useIcons = zoom >= 7;

      data.forEach(ship => {
        if (ship.lat && ship.lon) {
          let marker;
          
          if (useIcons) {
            const icon = this.createShipIcon(ship);
            marker = L.marker([ship.lat, ship.lon], { icon });
          } else {
            // Simple dot for global view
            marker = L.circleMarker([ship.lat, ship.lon], {
              radius: zoom < 4 ? 1.5 : 2.5,
              fillColor: this.getShipColor(ship.ship_type),
              color: '#ffffff',
              weight: 0.5,
              fillOpacity: 0.8
            });
          }

          marker.bindPopup(`
              <div style="font-family: sans-serif; min-width: 150px;">
                <b style="color: #1a73e8; font-size: 14px;">${ship.name || 'Nom inconnu'}</b><br>
                <span style="color: #888; font-size: 11px;">MMSI: ${ship.mmsi}</span><br>
                <div style="margin-top: 5px; font-size: 12px;">
                  <b>Vitesse:</b> ${ship.sog ? ship.sog.toFixed(1) : 0} nœuds<br>
                  <b>Cap:</b> ${ship.cog ? ship.cog.toFixed(0) : 0}°<br>
                  <b>Type:</b> ${this.getShipTypeName(ship.ship_type)}
                </div>
              </div>
            `, { maxWidth: 200 });
          
          markers.addLayer(marker);
        }
      });

      // Re-attach optimized layer
      markers.addTo(map);
      currentData = data;
      
      // Update info panel
      this.infoPanel.updateCount(data.length);
      
    } catch (error) {
      console.error('Query failed:', error);
    } finally {
      if (!silent) {
        this.loading.hide();
      }
      isQuerying = false;
    }
  }

  zoomToShip(ship) {
    if (ship?.lat && ship?.lon) {
      map.setView([ship.lat, ship.lon], 10);
    }
  }

  resetView() {
    map.setView([0, 0], 2);
    this.infobox.hide();
  }

  fitToData() {
    if (currentData.length > 0) {
      const bounds = L.latLngBounds(currentData.map(s => [s.lat, s.lon]));
      map.fitBounds(bounds, { padding: [50, 50] });
    }
  }
}

// Initialize application when DOM is ready
document.addEventListener('DOMContentLoaded', () => {
  // Add a small delay to allow Vite to load everything
  setTimeout(() => {
    window.app = new AISVisualizer();
  }, 100);
});

// Export for debugging
export { currentData };
