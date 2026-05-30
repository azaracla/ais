import { Deck, MapView } from '@deck.gl/core';
import { IconLayer } from '@deck.gl/layers';

const SHIP_COLORS = {
  default:    [158, 158, 158],
  fishing:    [255, 152,   0],
  tug:        [  0, 188, 212],
  military:   [ 96, 125, 139],
  pleasure:   [233,  30,  99],
  highspeed:  [255, 235,  59],
  passenger:  [ 33, 150, 243],
  cargo:      [ 76, 175,  80],
  tanker:     [244,  67,  54],
};

function getColorForType(shipType) {
  if (shipType == null) return SHIP_COLORS.default;
  if (shipType >= 70 && shipType <= 79) return SHIP_COLORS.cargo;
  if (shipType >= 80 && shipType <= 89) return SHIP_COLORS.tanker;
  if (shipType >= 60 && shipType <= 69) return SHIP_COLORS.passenger;
  if (shipType >= 40 && shipType <= 49) return SHIP_COLORS.highspeed;
  if (shipType === 31 || shipType === 32 || shipType === 52) return SHIP_COLORS.tug;
  if (shipType === 36 || shipType === 37) return SHIP_COLORS.pleasure;
  if (shipType === 35) return SHIP_COLORS.military;
  if (shipType === 30) return SHIP_COLORS.fishing;
  return SHIP_COLORS.default;
}

const iconCache = new Map();

function buildIcon(shipType, isMoving) {
  const key = `${shipType}_${isMoving}`;
  if (iconCache.has(key)) return iconCache.get(key);

  const [r, g, b] = getColorForType(shipType);
  const hex = [r, g, b].map(v => v.toString(16).padStart(2, '0')).join('');

  let icon;
  if (isMoving) {
    const svg = `<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'>` +
      `<polygon points='12,2 22,22 2,22' fill='%23${hex}' stroke='%23fff' stroke-width='1.5'/>` +
      `</svg>`;
    icon = {
      url: `data:image/svg+xml,${svg}`,
      width: 24,
      height: 24,
      anchorX: 12,
      anchorY: 12,
      mask: false,
    };
  } else {
    const svg = `<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'>` +
      `<circle cx='8' cy='8' r='6' fill='%23${hex}' stroke='%23fff' stroke-width='1.5'/>` +
      `</svg>`;
    icon = {
      url: `data:image/svg+xml,${svg}`,
      width: 16,
      height: 16,
      anchorX: 8,
      anchorY: 8,
      mask: false,
    };
  }

  iconCache.set(key, icon);
  return icon;
}

function getSizeForZoom(zoom) {
  if (zoom < 3)  return 10;
  if (zoom < 5)  return 14;
  if (zoom < 7)  return 20;
  if (zoom < 9)  return 28;
  if (zoom < 11) return 38;
  if (zoom < 13) return 52;
  return 64;
}

export class AISDeckRenderer {
  constructor(map, onHover, onClick) {
    this.map = map;
    this.onHover = onHover;
    this.onClick = onClick;
    this.data = [];
    this.deck = null;
    this._rafId = null;

    this._init();
  }

  _init() {
    const container = this.map.getContainer();
    container.style.position = 'relative';

    this.canvas = document.createElement('canvas');
    this.canvas.id = 'deckgl-canvas';
    Object.assign(this.canvas.style, {
      position:      'absolute',
      top:           '0',
      left:          '0',
      width:         '100%',
      height:        '100%',
      pointerEvents: 'none',
      zIndex:        '400',
      background:    'transparent',
    });

    const { clientWidth: w, clientHeight: h } = container;
    this.canvas.width  = w * devicePixelRatio;
    this.canvas.height = h * devicePixelRatio;
    container.appendChild(this.canvas);

    const center = this.map.getCenter();

    this.deck = new Deck({
      canvas: this.canvas,
      views:  new MapView({ repeat: true }),
      initialViewState: {
        longitude: center.lng,
        latitude:  center.lat,
        zoom:      this.map.getZoom(),
        pitch:     0,
        bearing:   0,
      },
      controller:      false,
      useDevicePixels: true,
      parameters: {
        clearColor: [0, 0, 0, 0],
      },
      layers:   [],
      onHover:  this.onHover,
      onClick:  this.onClick,
    });

    this._startRaf();
    this.map.on('resize', () => this._onResize());
  }

  _startRaf() {
    const tick = () => {
      this._syncView();
      this._rafId = requestAnimationFrame(tick);
    };
    this._rafId = requestAnimationFrame(tick);
  }

  _syncView() {
    if (!this.deck || !this.map) return;

    const center  = this.map.getCenter();
    const zoom    = this.map.getZoom();
    const bearing = this.map.getBearing?.() ?? 0;
    const pitch   = this.map.getPitch?.()   ?? 0;

    this.deck.setProps({
      viewState: {
        longitude:          center.lng,
        latitude:           center.lat,
        zoom,
        pitch,
        bearing,
        transitionDuration: 0,
      },
    });
  }

  _onResize() {
    if (!this.deck) return;
    const { clientWidth: w, clientHeight: h } = this.map.getContainer();
    this.canvas.width  = w * devicePixelRatio;
    this.canvas.height = h * devicePixelRatio;
    this.deck.setProps({ width: w, height: h });
    this._syncView();
  }

  _buildLayers() {
    if (!this.data.length) return [];

    const zoom = this.map.getZoom();
    const size = getSizeForZoom(zoom);

    return [
      new IconLayer({
        id:         'ais-ships',
        data:       this.data,
        pickable:   true,
        sizeScale:  1,
        sizeUnits:  'pixels',

        getPosition: d => [d.lon, d.lat, 0],

        getIcon: d => buildIcon(d.ship_type, d.sog != null && d.sog >= 0.5),

        getAngle: d => (d.sog != null && d.sog >= 0.5) ? (d.cog ?? 0) : 0,

        getSize: size,

        updateTriggers: {
          getIcon:  [],
          getAngle: [this.data],
          getSize:  [zoom],
        },
      }),
    ];
  }

  updateData(data) {
    this.data = data ?? [];
    if (this.deck) {
      this.deck.setProps({ layers: this._buildLayers() });
    }
  }

  updateZoom() {
    if (this.deck) {
      this.deck.setProps({ layers: this._buildLayers() });
    }
  }

  destroy() {
    if (this._rafId) {
      cancelAnimationFrame(this._rafId);
      this._rafId = null;
    }
    if (this.deck) {
      this.deck.finalize();
      this.deck = null;
    }
    if (this.canvas?.parentNode) {
      this.canvas.parentNode.removeChild(this.canvas);
      this.canvas = null;
    }
    this.data = [];
    iconCache.clear();
  }
}
