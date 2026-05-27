/**
 * UI Components: Timeline, Search, Infobox
 */

import { getShipAtPosition, zoomToShip, zoomToFit, getCameraPosition, setCameraPosition } from './renderer.js';
import { searchShips, queryShipTrack, getTimeRange } from './duckdb.js';

// State
let currentTimeRange = {
  start: new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString().slice(0, 19),
  end: new Date().toISOString().slice(0, 19)
};

let onTimeChangeCallback = null;
let onSearchSelectCallback = null;
let dataForInfobox = [];

/**
 * Create timeline UI component
 * @param {HTMLElement} parent - Parent element
 * @param {Object} initialRange - Initial time range
 * @param {function} onChange - Callback when time range changes
 * @returns {Object} - { updateRange: function }
 */
export function createTimeline(parent, initialRange, onChange) {
  onTimeChangeCallback = onChange;
  currentTimeRange = { ...initialRange };

  const container = document.createElement('div');
  container.className = 'timeline-container';
  
  // Preset buttons
  const presetButtons = createPresetButtons();
  
  // Date inputs
  const dateInputs = createDateInputs();
  
  // Range slider
  const rangeSlider = createRangeSlider();
  
  container.appendChild(presetButtons);
  container.appendChild(dateInputs);
  // container.appendChild(rangeSlider);
  
  parent.appendChild(container);

  // Initialize with current range
  updateInputs();

  return {
    updateRange: (range) => {
      currentTimeRange = { ...range };
      updateInputs();
      if (onTimeChangeCallback) onTimeChangeCallback(currentTimeRange);
    },
    getRange: () => ({ ...currentTimeRange }),
    container
  };
}

function createPresetButtons() {
  const group = document.createElement('div');
  group.className = 'timeline-controls';
  
  const presets = [
    { label: '1h', hours: 1, active: false },
    { label: '6h', hours: 6, active: false },
    { label: '12h', hours: 12, active: false },
    { label: '24h', hours: 24, active: true },
    { label: '3j', hours: 72, active: false },
    { label: '7j', hours: 168, active: false }
  ];
  
  presets.forEach(preset => {
    const btn = document.createElement('button');
    btn.className = 'timeline-button';
    if (preset.active) btn.classList.add('active');
    btn.textContent = preset.label;
    btn.dataset.hours = preset.hours;
    
    btn.addEventListener('click', () => {
      // Update active state
      group.querySelectorAll('.timeline-button').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      
      // Calculate time range
      const end = new Date();
      const start = new Date(end.getTime() - preset.hours * 60 * 60 * 1000);
      
      currentTimeRange = {
        start: start.toISOString().slice(0, 19),
        end: end.toISOString().slice(0, 19)
      };
      
      updateInputs();
      if (onTimeChangeCallback) onTimeChangeCallback({ ...currentTimeRange });
    });
    
    group.appendChild(btn);
  });
  
  return group;
}

function createDateInputs() {
  const group = document.createElement('div');
  group.className = 'timeline-range';
  
  // Start date
  const startInput = document.createElement('input');
  startInput.type = 'datetime-local';
  startInput.id = 'timeline-start';
  startInput.addEventListener('change', () => {
    currentTimeRange.start = startInput.value;
    if (onTimeChangeCallback) onTimeChangeCallback({ ...currentTimeRange });
  });
  
  // End date
  const endInput = document.createElement('input');
  endInput.type = 'datetime-local';
  endInput.id = 'timeline-end';
  endInput.addEventListener('change', () => {
    currentTimeRange.end = endInput.value;
    if (onTimeChangeCallback) onTimeChangeCallback({ ...currentTimeRange });
  });
  
  group.appendChild(startInput);
  group.appendChild(endInput);
  
  return group;
}

function createRangeSlider() {
  const group = document.createElement('div');
  group.className = 'timeline-slider';
  
  const slider = document.createElement('input');
  slider.type = 'range';
  slider.min = '0';
  slider.max = '100';
  slider.value = '100';
  slider.style.width = '200px';
  
  group.appendChild(slider);
  
  return group;
}

function updateInputs() {
  const startInput = document.getElementById('timeline-start');
  const endInput = document.getElementById('timeline-end');
  
  if (startInput) startInput.value = currentTimeRange.start;
  if (endInput) endInput.value = currentTimeRange.end;
}

/**
 * Create search UI component
 * @param {HTMLElement} parent - Parent element
 * @param {function} onSelect - Callback when a ship is selected
 * @returns {Object} - { search: function, clear: function }
 */
export function createSearch(parent, onSelect) {
  onSearchSelectCallback = onSelect;
  
  const container = document.createElement('div');
  container.className = 'search-container';
  
  const title = document.createElement('h3');
  title.textContent = 'Rechercher un navire';
  
  const inputWrapper = document.createElement('div');
  inputWrapper.className = 'search-input-wrapper';
  
  const input = document.createElement('input');
  input.type = 'text';
  input.placeholder = 'MMSI ou IMO...';
  input.autocomplete = 'off';
  input.id = 'search-input';
  
  const results = document.createElement('div');
  results.className = 'search-results';
  results.id = 'search-results';
  
  inputWrapper.appendChild(input);
  container.appendChild(title);
  container.appendChild(inputWrapper);
  container.appendChild(results);
  parent.appendChild(container);
  
  // Search on input
  let searchTimeout;
  input.addEventListener('input', async (e) => {
    clearTimeout(searchTimeout);
    const query = e.target.value.trim();
    
    if (query.length < 2) {
      results.classList.remove('visible');
      results.innerHTML = '';
      return;
    }
    
    searchTimeout = setTimeout(async () => {
      try {
        const ships = await searchShips(query, 10);
        displayResults(results, ships, onSelect);
      } catch (error) {
        console.error('Search failed:', error);
        results.innerHTML = '<div class="search-result">Erreur de recherche</div>';
        results.classList.add('visible');
      }
    }, 300);
  });
  
  // Close results when clicking outside
  document.addEventListener('click', (e) => {
    if (!container.contains(e.target)) {
      results.classList.remove('visible');
    }
  });
  
  return {
    search: (query) => {
      input.value = query;
      input.dispatchEvent(new Event('input'));
    },
    clear: () => {
      input.value = '';
      results.classList.remove('visible');
      results.innerHTML = '';
    },
    container
  };
}

function displayResults(container, ships, onSelect) {
  if (ships.length === 0) {
    container.innerHTML = '<div class="search-result"><span class="meta">Aucun résultat</span></div>';
    container.classList.add('visible');
    return;
  }
  
  container.innerHTML = ships.map(ship => {
    const name = ship.name || `Navire #${ship.mmsi}`;
    const imo = ship.imo_number ? `IMO: ${ship.imo_number}` : '';
    const pos = ship.lat && ship.lon ? `(${ship.lat.toFixed(4)}, ${ship.lon.toFixed(4)})` : '';
    const type = getShipTypeName(ship.message_type);
    
    return `
      <div class="search-result" data-mmsi="${ship.mmsi}" data-imo="${ship.imo_number}">
        <div class="name">${escapeHtml(name)}</div>
        <div class="details">${imo} ${pos}</div>
        <div class="meta">${type} | MMSI: ${ship.mmsi}</div>
      </div>
    `;
  }).join('');
  
  container.classList.add('visible');
  
  // Add click handlers
  container.querySelectorAll('.search-result').forEach(el => {
    el.addEventListener('click', () => {
      const mmsi = parseInt(el.dataset.mmsi);
      if (onSelect && mmsi) {
        onSelect({ mmsi, imo: parseInt(el.dataset.imo) || null });
      }
      container.classList.remove('visible');
    });
  });
}

function getShipTypeName(type) {
  const types = {
    1: 'Classe A',
    5: 'Classe A (Type 5)',
    18: 'Classe B',
    19: 'Équipement Classe B',
    24: 'Aide à la navigation',
    4: 'Station de base',
    27: 'Position au sol'
  };
  return types[type] || `Type ${type}`;
}

/**
 * Create infobox (tooltip) for ship details
 * @param {HTMLElement} parent - Parent element
 * @returns {Object} - { show: function, hide: function, update: function }
 */
export function createInfobox(parent) {
  const container = document.createElement('div');
  container.className = 'infobox';
  parent.appendChild(container);
  
  let isVisible = false;
  
  return {
    show: (ship, x, y) => {
      if (!ship) {
        container.classList.remove('visible');
        isVisible = false;
        return;
      }
      
      container.innerHTML = createInfoboxContent(ship);
      container.style.left = `${x + 10}px`;
      container.style.top = `${y + 10}px`;
      container.classList.add('visible');
      isVisible = true;
    },
    hide: () => {
      container.classList.remove('visible');
      isVisible = false;
    },
    update: (ship) => {
      if (!isVisible) return;
      container.innerHTML = createInfoboxContent(ship);
    },
    isVisible: () => isVisible,
    container
  };
}

function createInfoboxContent(ship) {
  const name = ship.name || `MMSI: ${ship.mmsi}`;
  const imo = ship.imo_number ? ship.imo_number : 'N/A';
  const lat = ship.lat != null ? ship.lat.toFixed(5) : 'N/A';
  const lon = ship.lon != null ? ship.lon.toFixed(5) : 'N/A';
  const cog = ship.cog != null ? `${ship.cog.toFixed(1)}°` : 'N/A';
  const sog = ship.sog != null ? `${ship.sog.toFixed(1)} nœuds` : 'N/A';
  const type = getShipTypeName(ship.message_type);
  const ts = ship.ts ? formatTimestamp(ship.ts) : 'N/A';
  
  return `
    <h4>${escapeHtml(name)}</h4>
    <div class="infobox-row">
      <span class="infobox-label">MMSI:</span>
      <span class="infobox-value">${ship.mmsi}</span>
    </div>
    <div class="infobox-row">
      <span class="infobox-label">IMO:</span>
      <span class="infobox-value">${imo}</span>
    </div>
    <div class="infobox-divider"></div>
    <div class="infobox-row">
      <span class="infobox-label">Position:</span>
      <span class="infobox-value">${lat}, ${lon}</span>
    </div>
    <div class="infobox-row">
      <span class="infobox-label">Cap:</span>
      <span class="infobox-value">${cog}</span>
    </div>
    <div class="infobox-row">
      <span class="infobox-label">Vitesse:</span>
      <span class="infobox-value">${sog}</span>
    </div>
    <div class="infobox-row">
      <span class="infobox-label">Type:</span>
      <span class="infobox-value">${type}</span>
    </div>
    <div class="infobox-divider"></div>
    <div class="infobox-row">
      <span class="infobox-label">Dernier message:</span>
      <span class="infobox-value">${ts}</span>
    </div>
  `;
}

function formatTimestamp(ts) {
  if (typeof ts === 'string') {
    ts = new Date(ts);
  }
  return ts.toLocaleString('fr-FR', {
    year: 'numeric',
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit'
  });
}

/**
 * Create FPS counter
 * @param {HTMLElement} parent - Parent element
 * @returns {Object} - { update: function }
 */
export function createFpsCounter(parent) {
  const fpsSpan = parent.querySelector('#fps');
  
  let lastTime = performance.now();
  let frameCount = 0;
  let fps = 60;
  
  return {
    update: () => {
      frameCount++;
      const now = performance.now();
      const elapsed = now - lastTime;
      
      if (elapsed >= 1000) {
        fps = Math.round((frameCount * 1000) / elapsed);
        if (fpsSpan) fpsSpan.textContent = `${fps} fps`;
        frameCount = 0;
        lastTime = now;
      }
    },
    getFps: () => fps
  };
}

/**
 * Create info panel
 * @param {HTMLElement} parent - Parent element
 * @returns {Object} - { updateCount: function, updateTime: function }
 */
export function createInfoPanel(parent) {
  const countSpan = parent.querySelector('#count');
  const timeSpan = parent.querySelector('#time');
  
  return {
    updateCount: (count) => {
      if (countSpan) {
        countSpan.textContent = `${count.toLocaleString()} navires`;
      }
    },
    updateTime: (range) => {
      if (timeSpan) {
        const start = new Date(range.start);
        const end = new Date(range.end);
        timeSpan.textContent = `${start.toLocaleDateString('fr-FR')} - ${end.toLocaleDateString('fr-FR')}`;
      }
    }
  };
}

/**
 * Create loading indicator
 * @returns {Object} - { show: function, hide: function, update: function }
 */
export function createLoadingIndicator() {
  const loadingEl = document.getElementById('loading');
  const progressEl = loadingEl?.querySelector('p');
  
  return {
    show: (message = 'Chargement...') => {
      if (loadingEl) {
        loadingEl.classList.remove('hidden');
        if (progressEl) progressEl.textContent = message;
      }
    },
    hide: () => {
      if (loadingEl) {
        loadingEl.classList.add('hidden');
      }
    },
    update: (progress) => {
      if (progressEl) {
        progressEl.textContent = `Chargement: ${Math.round(progress)}%`;
      }
    }
  };
}

/**
 * Create mouse interaction handler for infobox
 * @param {HTMLElement} canvas - Renderer canvas element
 * @param {Object} infobox - Infobox instance from createInfobox
 * @param {Array} data - Current ship data
 * @param {Object} camera - Three.js camera
 * @returns {Object} - { updateData: function }
 */
export function createMouseHandler(canvas, infobox, dataRef, camera) {
  let data = dataRef;
  
  canvas.addEventListener('mousemove', (event) => {
    // Only show infobox if we have data
    if (!data || data.length === 0) {
      infobox.hide();
      return;
    }
    
    // Calculate normalized mouse coordinates
    const rect = canvas.getBoundingClientRect();
    const mouseX = ((event.clientX - rect.left) / rect.width) * 2 - 1;
    const mouseY = -((event.clientY - rect.top) / rect.height) * 2 + 1;
    
    // Get ship at this position
    const ship = getShipAtPosition(mouseX, mouseY);
    
    if (ship) {
      infobox.show(ship, event.clientX, event.clientY);
    } else {
      infobox.hide();
    }
  });
  
  // Hide infobox on mouse leave
  canvas.addEventListener('mouseleave', () => {
    infobox.hide();
  });
  
  return {
    updateData: (newData) => {
      data = newData;
    }
  };
}

/**
 * Create keyboard shortcuts
 * @param {Object} actions - Action handlers
 */
export function createKeyboardShortcuts(actions) {
  document.addEventListener('keydown', (e) => {
    // Don't trigger if typing in an input
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
    
    switch (e.key) {
      case 'Escape':
        if (actions.onEscape) actions.onEscape();
        break;
      case 'r':
      case 'R':
        if (actions.onReset) actions.onReset();
        break;
      case 'f':
      case 'F':
        if (actions.onFit) actions.onFit();
        break;
    }
  });
}

/**
 * Format ship count with appropriate units
 * @param {number} count - Number of ships
 * @returns {string}
 */
export function formatShipCount(count) {
  if (count >= 1000000) {
    return `${(count / 1000000).toFixed(1)}M navires`;
  } else if (count >= 1000) {
    return `${(count / 1000).toFixed(1)}K navires`;
  }
  return `${count.toLocaleString()} navires`;
}

/**
 * Escape HTML special characters
 * @param {string} text - Text to escape
 * @returns {string}
 */
function escapeHtml(text) {
  if (!text) return '';
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

/**
 * Async function to get available time range from database
 * @returns {Promise<Object>}
 */
export async function fetchAvailableTimeRange() {
  try {
    return await getTimeRange();
  } catch (error) {
    console.error('Failed to fetch time range:', error);
    // Return reasonable defaults
    return {
      min: new Date(Date.now() - 7 * 24 * 60 * 60 * 1000).toISOString().slice(0, 19),
      max: new Date().toISOString().slice(0, 19)
    };
  }
}
