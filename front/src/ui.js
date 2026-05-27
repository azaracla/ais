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
 * @param {string} initialDateTime - Initial date time string
 * @param {function} onChange - Callback when date time changes
 * @returns {Object} - { updateDateTime: function }
 */
export function createTimeline(parent, initialDateTime, onChange) {
  onTimeChangeCallback = onChange;
  const currentDateTime = initialDateTime;

  const container = document.createElement('div');
  container.className = 'timeline-container';
  
  const label = document.createElement('span');
  label.textContent = 'Date et Heure :';
  label.style.fontSize = '12px';
  label.style.color = '#666';
  label.style.fontWeight = '600';

  // Single Date input
  const dateInput = document.createElement('input');
  dateInput.type = 'datetime-local';
  dateInput.id = 'timeline-datetime';
  dateInput.value = initialDateTime;
  dateInput.style.borderRadius = '20px';
  dateInput.style.padding = '8px 16px';
  dateInput.style.border = '1px solid #dcdfe6';
  dateInput.style.background = '#f8f9fa';
  dateInput.style.fontSize = '13px';
  
  dateInput.addEventListener('change', () => {
    if (onTimeChangeCallback) onTimeChangeCallback(dateInput.value);
  });
  
  container.appendChild(label);
  container.appendChild(dateInput);
  parent.appendChild(container);

  return {
    updateDateTime: (dateTime) => {
      dateInput.value = dateTime;
      if (onTimeChangeCallback) onTimeChangeCallback(dateTime);
    },
    getDateTime: () => dateInput.value,
    container
  };
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
    const type = getShipTypeName(ship.ship_type);
    const color = getShipColor(ship.ship_type);
    
    return `
      <div class="search-result" data-mmsi="${ship.mmsi}" data-imo="${ship.imo_number}">
        <div class="name">
          <span style="display:inline-block; width:10px; height:10px; border-radius:50%; background:${color}; margin-right:8px;"></span>
          ${escapeHtml(name)}
        </div>
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

function getShipColor(shipType) {
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

function getShipTypeName(type) {
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
