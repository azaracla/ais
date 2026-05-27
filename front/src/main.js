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
  initRenderer, 
  updatePoints, 
  animate,
  zoomToShip,
  zoomToFit,
  getViewBounds,
  getRendererObjects,
  getShipAtPosition 
} from './renderer.js';
import {
  createTimeline,
  createSearch,
  createInfobox,
  createFpsCounter,
  createInfoPanel,
  createLoadingIndicator,
  createMouseHandler,
  createKeyboardShortcuts,
  fetchAvailableTimeRange
} from './ui.js';

// State
let currentData = [];
let isQuerying = false;

// Main application
class AISVisualizer {
  constructor() {
    this.appContainer = document.getElementById('app');
    this.infoContainer = document.getElementById('info');
    
    // Initialize all components
    this.init();
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
      
      // Initialize renderer
      const rendererObj = initRenderer(this.appContainer);
      this.renderer = rendererObj.renderer;
      this.camera = rendererObj.camera;
      this.controls = rendererObj.controls;
      
      this.loading.update(85);
      
      // Create UI components
      this.createUI();
      
      // Get available time range and initial data
      await this.loadInitialData();
      
      // Hide loading screen
      this.loading.hide();
      
      // Start animation loop
      this.startAnimation();
      
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
    // Timeline
    const now = new Date();
    const defaultRange = {
      start: new Date(now.getTime() - 24 * 60 * 60 * 1000).toISOString().slice(0, 19),
      end: now.toISOString().slice(0, 19)
    };
    
    this.timeline = createTimeline(document.body, defaultRange, (range) => {
      this.onTimeRangeChange(range);
    });
    
    // Search
    this.search = createSearch(document.body, (ship) => {
      this.onShipSearch(ship);
    });
    
    // Infobox
    this.infobox = createInfobox(document.body);
    
    // FPS Counter
    this.fpsCounter = createFpsCounter(this.infoContainer);
    
    // Info Panel
    this.infoPanel = createInfoPanel(this.infoContainer);
    
    // Keyboard shortcuts
    createKeyboardShortcuts({
      onEscape: () => this.infobox.hide(),
      onReset: () => this.resetView(),
      onFit: () => this.fitToData()
    });
    
    // Mouse handler for infobox
    const canvas = this.renderer.domElement;
    this.mouseHandler = createMouseHandler(
      canvas, 
      this.infobox, 
      currentData,
      this.camera
    );
    
    // Double click to zoom to ship
    canvas.addEventListener('dblclick', (event) => {
      const rect = canvas.getBoundingClientRect();
      const mouseX = ((event.clientX - rect.left) / rect.width) * 2 - 1;
      const mouseY = -((event.clientY - rect.top) / rect.height) * 2 + 1;
      
      const ship = getShipAtPosition(mouseX, mouseY);
      if (ship) {
        this.zoomToShip(ship);
      }
    });
  }

  async loadInitialData() {
    this.loading.update(90);
    
    // Get available time range
    const availableRange = await fetchAvailableTimeRange();
    console.log('Available time range:', availableRange);
    
    // Use last 24 hours as default
    const now = new Date();
    const start = new Date(now.getTime() - 24 * 60 * 60 * 1000);
    const end = new Date(now);
    
    // Clamp to available range
    const timeRange = {
      start: start.toISOString().slice(0, 19),
      end: end.toISOString().slice(0, 19)
    };
    
    // Update timeline
    this.timeline.updateRange(timeRange);
    
    // Query data
    await this.queryAndUpdateData(timeRange);
    
    // Update info panel
    this.infoPanel.updateTime(timeRange);
  }

  async onTimeRangeChange(range) {
    console.log('Time range changed:', range);
    
    // Update info panel
    this.infoPanel.updateTime(range);
    
    // Query new data
    await this.queryAndUpdateData(range);
  }

  async onShipSearch(ship) {
    console.log('Ship selected:', ship);
    
    if (ship.mmsi) {
      // Query track for this ship
      try {
        const track = await queryLastPositions(
          this.timeline.getRange(),
          {
            limit: 100,
            // Filter by MMSI - we'll filter client side
          }
        );
        
        // Find the ship in current data or track
        const shipData = track.find(s => s.mmsi === ship.mmsi) || 
                        currentData.find(s => s.mmsi === ship.mmsi);
        
        if (shipData) {
          this.zoomToShip(shipData);
          
          // Highlight the ship by showing infobox
          const canvas = this.renderer.domElement;
          const rect = canvas.getBoundingClientRect();
          const centerX = rect.width / 2;
          const centerY = rect.height / 2;
          this.infobox.show(shipData, centerX, centerY);
        }
      } catch (error) {
        console.error('Failed to load ship track:', error);
      }
    }
  }

  async queryAndUpdateData(timeRange) {
    if (isQuerying) {
      console.log('Query already in progress, skipping...');
      return;
    }
    
    isQuerying = true;
    this.loading.show('Chargement des données...');
    
    try {
      const startTime = performance.now();
      
      // Get view bounds if zoomed in
      const bounds = getViewBounds();
      
      // Query last positions
      const data = await queryLastPositions(timeRange, {
        limit: 500000,
        minLat: bounds.minLat,
        maxLat: bounds.maxLat,
        minLon: bounds.minLon,
        maxLon: bounds.maxLon
      });
      
      const duration = performance.now() - startTime;
      console.log(`Loaded ${data.length} ships in ${duration.toFixed(0)}ms`);
      
      // Update visualization
      updatePoints(data);
      currentData = data;
      
      // Update mouse handler with new data
      this.mouseHandler.updateData(currentData);
      
      // Update info panel
      this.infoPanel.updateCount(data.length);
      
    } catch (error) {
      console.error('Query failed:', error);
    } finally {
      this.loading.hide();
      isQuerying = false;
    }
  }

  zoomToShip(ship) {
    if (zoomToShip) {
      zoomToShip(ship);
    }
    // Also center camera on ship
    this.controls.target.set(ship.lon, 0, -ship.lat);
    this.controls.update();
  }

  resetView() {
    this.controls.reset();
    this.camera.position.set(0, 0, 100);
    this.camera.lookAt(0, 0, 0);
    this.controls.update();
    this.infobox.hide();
  }

  fitToData() {
    zoomToFit();
  }

  startAnimation() {
    // Update FPS counter each frame
    animate(() => {
      this.fpsCounter.update();
    });
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
