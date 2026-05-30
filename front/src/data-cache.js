/**
 * Data Cache and Delta Fetching for AIS Visualizer
 * Implements viewport-based incremental data loading
 */

// Cache structure: { [timeRangeKey]: { [bboxKey]: data } }
class AISDataCache {
  constructor() {
    this.cache = new Map();
    this.lastViewport = null;
    this.lastTimeRange = null;
    this.bboxExpansionFactor = 0.1; // Expand bbox by 10% to avoid gaps
  }

  // Generate unique key for time range
  getTimeRangeKey(range) {
    if (!range) return null;
    return `${range.start}|${range.end}`;
  }

  // Generate unique key for bounding box
  getBboxKey(bounds) {
    if (!bounds) return null;
    // Round to 3 decimals to avoid too many cache entries
    const round = (v) => Math.round(v * 1000) / 1000;
    return `${round(bounds.west)}|${round(bounds.south)}|${round(bounds.east)}|${round(bounds.north)}`;
  }

  // Expand bbox to include a margin (prevents gaps when panning)
  expandBbox(bounds, factor = 0.1) {
    const latMargin = (bounds.north - bounds.south) * factor;
    const lonMargin = (bounds.east - bounds.west) * factor;
    return {
      west: bounds.west - lonMargin,
      south: bounds.south - latMargin,
      east: bounds.east + lonMargin,
      north: bounds.north + latMargin
    };
  }

  // Calculate delta between current viewport and last viewport
  // Returns: null if no delta, or the delta bbox to fetch
  calculateDeltaViewport(currentBounds, currentTimeRange) {
    if (!this.lastViewport || !this.lastTimeRange) {
      // First load - fetch everything
      this.lastViewport = currentBounds;
      this.lastTimeRange = currentTimeRange;
      return { 
        bounds: this.expandBbox(currentBounds), 
        timeRange: currentTimeRange,
        isFull: true 
      };
    }

    // If time range changed, invalidate cache for this range
    const currentTimeKey = this.getTimeRangeKey(currentTimeRange);
    const lastTimeKey = this.getTimeRangeKey(this.lastTimeRange);
    
    if (currentTimeKey !== lastTimeKey) {
      // Time changed - need to refetch everything for new time
      this.lastViewport = currentBounds;
      this.lastTimeRange = currentTimeRange;
      return { 
        bounds: this.expandBbox(currentBounds), 
        timeRange: currentTimeRange,
        isFull: true 
      };
    }

    // Check if viewport overlaps with last viewport
    const overlap = this.calculateBboxOverlap(this.lastViewport, currentBounds);
    
    if (overlap >= 0.8) {
      // Significant overlap - only fetch the new parts
      const deltaBbox = this.calculateBboxDelta(this.lastViewport, currentBounds);
      if (deltaBbox) {
        this.lastViewport = currentBounds;
        return { 
          bounds: this.expandBbox(deltaBbox), 
          timeRange: currentTimeRange,
          isFull: false 
        };
      }
      return null; // No significant change
    } else {
      // Mostly new area - fetch everything
      this.lastViewport = currentBounds;
      return { 
        bounds: this.expandBbox(currentBounds), 
        timeRange: currentTimeRange,
        isFull: true 
      };
    }
  }

  // Calculate overlap ratio between two bboxes (0-1)
  calculateBboxOverlap(bbox1, bbox2) {
    const intersection = this.getBboxIntersection(bbox1, bbox2);
    if (!intersection) return 0;
    
    const area1 = (bbox1.east - bbox1.west) * (bbox1.north - bbox1.south);
    const area2 = (bbox2.east - bbox2.west) * (bbox2.north - bbox2.south);
    const areaIntersection = (intersection.east - intersection.west) * (intersection.north - intersection.south);
    
    return areaIntersection / Math.min(area1, area2);
  }

  // Get intersection of two bboxes
  getBboxIntersection(bbox1, bbox2) {
    const west = Math.max(bbox1.west, bbox2.west);
    const east = Math.min(bbox1.east, bbox2.east);
    const south = Math.max(bbox1.south, bbox2.south);
    const north = Math.min(bbox1.north, bbox2.north);
    
    if (west >= east || south >= north) return null;
    return { west, east, south, north };
  }

  // Calculate the delta (new areas) between two bboxes
  calculateBboxDelta(lastBbox, currentBbox) {
    const union = {
      west: Math.min(lastBbox.west, currentBbox.west),
      south: Math.min(lastBbox.south, currentBbox.south),
      east: Math.max(lastBbox.east, currentBbox.east),
      north: Math.max(lastBbox.north, currentBbox.north)
    };
    
    // Check if current bbox extends beyond last bbox in any direction
    const extendsWest = currentBbox.west < lastBbox.west;
    const extendsEast = currentBbox.east > lastBbox.east;
    const extendsNorth = currentBbox.north > lastBbox.north;
    const extendsSouth = currentBbox.south < lastBbox.south;
    
    if (!extendsWest && !extendsEast && !extendsNorth && !extendsSouth) {
      // Current bbox is completely inside last bbox
      return null;
    }
    
    // Calculate delta regions (up to 4 new rectangles)
    const deltas = [];
    
    // West strip
    if (extendsWest) {
      deltas.push({
        west: currentBbox.west,
        south: Math.max(currentBbox.south, lastBbox.south),
        east: Math.min(currentBbox.east, lastBbox.west),
        north: Math.min(currentBbox.north, lastBbox.north)
      });
    }
    
    // East strip
    if (extendsEast) {
      deltas.push({
        west: Math.max(currentBbox.west, lastBbox.east),
        south: Math.max(currentBbox.south, lastBbox.south),
        east: currentBbox.east,
        north: Math.min(currentBbox.north, lastBbox.north)
      });
    }
    
    // North strip
    if (extendsNorth) {
      deltas.push({
        west: Math.max(currentBbox.west, lastBbox.west),
        south: Math.max(currentBbox.south, lastBbox.north),
        east: Math.min(currentBbox.east, lastBbox.east),
        north: currentBbox.north
      });
    }
    
    // South strip
    if (extendsSouth) {
      deltas.push({
        west: Math.max(currentBbox.west, lastBbox.west),
        south: currentBbox.south,
        east: Math.min(currentBbox.east, lastBbox.east),
        north: Math.min(currentBbox.north, lastBbox.south)
      });
    }
    
    // Combine all delta regions into one bbox
    if (deltas.length === 0) return null;
    
    return {
      west: Math.min(...deltas.map(d => d.west)),
      south: Math.min(...deltas.map(d => d.south)),
      east: Math.max(...deltas.map(d => d.east)),
      north: Math.max(...deltas.map(d => d.north))
    };
  }

  // Get cached data for a specific bbox and time range
  getCachedData(timeRange, bounds) {
    const timeKey = this.getTimeRangeKey(timeRange);
    const bboxKey = this.getBboxKey(bounds);
    
    if (!timeKey || !bboxKey) return null;
    
    const timeCache = this.cache.get(timeKey);
    if (!timeCache) return null;
    
    return timeCache.get(bboxKey) || null;
  }

  // Set cached data for a specific bbox and time range
  setCachedData(timeRange, bounds, data) {
    const timeKey = this.getTimeRangeKey(timeRange);
    const bboxKey = this.getBboxKey(bounds);
    
    if (!timeKey || !bboxKey) return;
    
    if (!this.cache.has(timeKey)) {
      this.cache.set(timeKey, new Map());
    }
    
    this.cache.get(timeKey).set(bboxKey, data);
  }

  // Merge new data with existing cache
  mergeData(timeRange, newData) {
    const timeKey = this.getTimeRangeKey(timeRange);
    if (!timeKey) return newData;
    
    const timeCache = this.cache.get(timeKey);
    if (!timeCache) {
      this.cache.set(timeKey, new Map());
      return newData;
    }
    
    // For now, just return new data (will be improved)
    return newData;
  }

  // Clear cache for a specific time range
  clearTimeRange(timeRange) {
    const timeKey = this.getTimeRangeKey(timeRange);
    if (timeKey) {
      this.cache.delete(timeKey);
    }
  }

  // Clear entire cache
  clearAll() {
    this.cache.clear();
    this.lastViewport = null;
    this.lastTimeRange = null;
  }

  // Get all cached data for a time range
  getAllCachedForTimeRange(timeRange) {
    const timeKey = this.getTimeRangeKey(timeRange);
    if (!timeKey) return [];
    
    const timeCache = this.cache.get(timeKey);
    if (!timeCache) return [];
    
    let allData = [];
    for (const [, data] of timeCache) {
      allData = [...allData, ...data];
    }
    return allData;
  }
}

// Separate cache for Arrow tables (to avoid serialization)
class ArrowCache {
  constructor() {
    this.arrowCache = new Map(); // { [timeRangeKey]: { [bboxKey]: arrowResult } }
  }

  setArrowData(timeRange, bounds, arrowResult) {
    const timeKey = this.getTimeRangeKey(timeRange);
    const bboxKey = this.getBboxKey(bounds);
    
    if (!timeKey || !bboxKey) return;
    
    if (!this.arrowCache.has(timeKey)) {
      this.arrowCache.set(timeKey, new Map());
    }
    
    this.arrowCache.get(timeKey).set(bboxKey, arrowResult);
  }

  getArrowData(timeRange, bounds) {
    const timeKey = this.getTimeRangeKey(timeRange);
    const bboxKey = this.getBboxKey(bounds);
    
    if (!timeKey || !bboxKey) return null;
    
    const timeCache = this.arrowCache.get(timeKey);
    if (!timeCache) return null;
    
    return timeCache.get(bboxKey) || null;
  }

  getTimeRangeKey(range) {
    if (!range) return null;
    return `${range.start}|${range.end}`;
  }

  getBboxKey(bounds) {
    if (!bounds) return null;
    const round = (v) => Math.round(v * 1000) / 1000;
    return `${round(bounds.west)}|${round(bounds.south)}|${round(bounds.east)}|${round(bounds.north)}`;
  }

  clearTimeRange(timeRange) {
    const timeKey = this.getTimeRangeKey(timeRange);
    if (timeKey) {
      this.arrowCache.delete(timeKey);
    }
  }

  clearAll() {
    this.arrowCache.clear();
  }

  getAllForTimeRange(timeRange) {
    const timeKey = this.getTimeRangeKey(timeRange);
    if (!timeKey) return [];
    
    const timeCache = this.arrowCache.get(timeKey);
    if (!timeCache) return [];
    
    return Array.from(timeCache.values());
  }
}

const arrowCache = new ArrowCache();

// Singleton instance
export const dataCache = new AISDataCache();

// Arrow cache methods
export function setCachedArrowData(timeRange, bounds, arrowResult) {
  arrowCache.setArrowData(timeRange, bounds, arrowResult);
}

export function getCachedArrowData(timeRange, bounds) {
  return arrowCache.getArrowData(timeRange, bounds);
}

export function getAllCachedArrowForTimeRange(timeRange) {
  return arrowCache.getAllForTimeRange(timeRange);
}

export function clearArrowCacheTimeRange(timeRange) {
  arrowCache.clearTimeRange(timeRange);
}

export function clearArrowCacheAll() {
  arrowCache.clearAll();
}

// Utility: Calculate bbox from Leaflet bounds
// Clamp to valid geographic ranges
 export function boundsToBbox(bounds) {
  return {
    west: Math.max(-180, Math.min(180, bounds.getWest())),
    south: Math.max(-90, Math.min(90, bounds.getSouth())),
    east: Math.max(-180, Math.min(180, bounds.getEast())),
    north: Math.max(-90, Math.min(90, bounds.getNorth()))
  };
}

// Utility: Check if bbox is valid
 export function isValidBbox(bbox) {
  return bbox && 
    bbox.west !== undefined && 
    bbox.south !== undefined && 
    bbox.east !== undefined && 
    bbox.north !== undefined &&
    bbox.west < bbox.east &&
    bbox.south < bbox.north;
}
