/**
 * Three.js WebGL Renderer for AIS Ship Visualization
 * Handles ship points, direction arrows, and camera controls
 */

import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

// Scene state
let scene, camera, renderer, controls;
let pointsMesh, arrowsMesh;
let raycaster, mouse;

// Data cache for infobox
let currentData = [];

// Camera state for bounds calculation
const cameraState = {
  zoom: 1
};

/**
 * Initialize the Three.js renderer
 * @param {HTMLElement} container - DOM element to contain the canvas
 * @returns {Object} - { scene, camera, renderer, controls }
 */
export function initRenderer(container) {
  // Scene
  scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0a0a1a);

  // Camera: Orthographic projection covering the whole world
  // X: -180 to 180 (longitude), Z: -90 to 90 (latitude)
  const aspect = container.clientWidth / container.clientHeight;
  const cameraWidth = 180 * 1.2;  // Slightly larger than world
  const cameraHeight = cameraWidth / aspect;
  
  camera = new THREE.OrthographicCamera(
    -cameraWidth, cameraWidth,
    cameraHeight, -cameraHeight,
    0.1, 1000
  );
  camera.position.set(0, 0, 100);
  camera.lookAt(0, 0, 0);

  // Renderer
  renderer = new THREE.WebGLRenderer({
    antialias: true,
    alpha: false
  });
  renderer.setSize(container.clientWidth, container.clientHeight);
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setClearColor(0x0a0a1a);
  container.appendChild(renderer.domElement);

  // Orbit controls
  controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.05;
  controls.screenSpacePanning = false;
  controls.maxPolarAngle = Math.PI / 2 + 0.1; // Slightly below horizon
  controls.minDistance = 50;
  controls.maxDistance = 500;
  controls.maxZoom = 10;
  controls.minZoom = 0.5;

  // Raycaster for mouse interaction
  raycaster = new THREE.Raycaster();
  mouse = new THREE.Vector2();

  // Add grid helper
  const gridHelper = new THREE.GridHelper(360, 36, 0x222244, 0x111122);
  gridHelper.rotation.x = Math.PI / 2; // Rotate to horizontal plane
  scene.add(gridHelper);

  // Add coordinate axes helper
  const axesHelper = new THREE.AxesHelper(100);
  axesHelper.visible = false; // Hidden by default
  scene.add(axesHelper);

  // Add Earth sphere for reference
  const earthGeometry = new THREE.SphereGeometry(90, 64, 64);
  const earthMaterial = new THREE.MeshBasicMaterial({
    color: 0x1a3a5a,
    wireframe: true,
    transparent: true,
    opacity: 0.2
  });
  const earth = new THREE.Mesh(earthGeometry, earthMaterial);
  scene.add(earth);

  // Add coastlines (simplified)
  createCoastlines();

  // Handle resize
  window.addEventListener('resize', () => {
    const width = container.clientWidth;
    const height = container.clientHeight;
    
    const cameraWidth = 180 * 1.2;
    const cameraHeight = cameraWidth / (width / height);
    
    camera.left = -cameraWidth;
    camera.right = cameraWidth;
    camera.top = cameraHeight;
    camera.bottom = -cameraHeight;
    camera.updateProjectionMatrix();
    
    renderer.setSize(width, height);
  });

  return { scene, camera, renderer, controls };
}

/**
 * Create simplified coastlines
 */
function createCoastlines() {
  // Simple continental outlines using lat/lon coordinates
  // These are very simplified representations
  const continents = [
    // Europe rough outline
    { name: 'europe', color: 0x335533 },
    // North America
    { name: 'north-america', color: 0x335533 },
    // Asia
    { name: 'asia', color: 0x335533 },
    // Africa
    { name: 'africa', color: 0x335533 },
    // South America
    { name: 'south-america', color: 0x335533 },
    // Australia
    { name: 'australia', color: 0x335533 }
  ];

  // For performance, we'll just add a simple grid for now
  // A full coastline implementation would require GeoJSON parsing
}

/**
 * Update the visualization with new ship data
 * @param {Array} data - Array of ship objects with lat, lon, cog, sog, mmsi, name, etc.
 */
export function updatePoints(data) {
  currentData = data;

  // Clean up previous meshes
  if (pointsMesh) {
    scene.remove(pointsMesh);
    pointsMesh.geometry.dispose();
    pointsMesh.material.dispose();
  }
  if (arrowsMesh) {
    scene.remove(arrowsMesh);
    arrowsMesh.geometry.dispose();
    arrowsMesh.material.dispose();
  }

  if (data.length === 0) {
    pointsMesh = null;
    arrowsMesh = null;
    return;
  }

  // Prepare attributes
  const positions = [];
  const colors = [];
  const sizes = [];
  const arrowMatrices = [];

  // Create dummy object for arrow matrix calculations
  const arrowDummy = new THREE.Object3D();

  // Pre-calculate time-based pulsing for recently updated ships
  const now = Date.now();

  data.forEach((ship, index) => {
    if (ship.lat == null || ship.lon == null) return;

    // Convert lat/lon to x/z coordinates
    // THREE.js: X = longitude (-180 to 180), Z = -latitude (-90 to 90)
    const x = ship.lon;
    const z = -ship.lat;
    const y = 0;

    positions.push(x, y, z);

    // Size based on SOG (Speed Over Ground) - scale for visibility
    const speed = ship.sog || 0;
    const size = Math.max(0.5, Math.min(8, speed / 3));
    sizes.push(size);

    // Color based on ship type
    const color = getShipColor(ship);
    colors.push(color.r, color.g, color.b);

    // Create arrow matrix for direction
    if (ship.cog != null && !isNaN(ship.cog)) {
      // COG is degrees clockwise from North (0 = North, 90 = East, 180 = South, 270 = West)
      // In THREE.js, rotation around Y axis: 0 = East, 90 = North, 180 = West, 270 = South
      // So we need to convert: cog -> -cog + 90 degrees, then to radians
      const rotationY = THREE.MathUtils.degToRad(-ship.cog + 90);
      
      arrowDummy.position.set(x, y, z);
      arrowDummy.rotation.set(0, rotationY, 0);
      arrowDummy.updateMatrix();
      arrowMatrices.push(arrowDummy.matrix.clone());
    } else {
      // No direction info - create identity matrix
      const matrix = new THREE.Matrix4();
      matrix.makeTranslation(x, y, z);
      arrowMatrices.push(matrix);
    }
  });

  // Create points geometry
  const pointsGeometry = new THREE.BufferGeometry();
  pointsGeometry.setAttribute('position', new THREE.Float32BufferAttribute(positions, 3));
  pointsGeometry.setAttribute('size', new THREE.Float32BufferAttribute(sizes, 1));
  pointsGeometry.setAttribute('color', new THREE.Float32BufferAttribute(colors, 3));

  pointsMesh = new THREE.Points(pointsGeometry, new THREE.PointsMaterial({
    size: 1,
    vertexColors: true,
    transparent: true,
    opacity: 0.85,
    sizeAttenuation: false,
    blending: THREE.AdditiveBlending
  }));

  scene.add(pointsMesh);

  // Create direction arrows using InstancedMesh
  const arrowGeometry = new THREE.ConeGeometry(0.4, 1.5, 6);
  arrowGeometry.rotateX(Math.PI / 2); // Point cone along +Z

  // Shift cone to point at origin
  const coneOffset = new THREE.Matrix4().makeTranslation(0, 0, -0.75);
  arrowGeometry.applyMatrix4(coneOffset);

  const arrowMaterial = new THREE.MeshBasicMaterial({
    color: 0xffffff,
    transparent: true,
    opacity: 0.7
  });

  arrowsMesh = new THREE.InstancedMesh(arrowGeometry, arrowMaterial, arrowMatrices.length);

  for (let i = 0; i < arrowMatrices.length; i++) {
    arrowsMesh.setMatrixAt(i, arrowMatrices[i]);
  }

  scene.add(arrowsMesh);
}

/**
 * Get color for a ship based on its properties
 * @param {Object} ship - Ship data
 * @returns {THREE.Color}
 */
function getShipColor(ship) {
  // Color scheme based on ship type and status
  const type = ship.message_type;
  const sog = ship.sog || 0;

  // Message type mapping:
  // 1-3: Class A vessel
  // 4-10: Base station, etc.
  // 18: Class B vessel
  // 19: Class B equipment
  // 24: Static (ATON)
  // 25-27: Various

  switch (type) {
    case 1: case 2: case 3: // Class A
      return new THREE.Color(0x4a90e2); // Blue
    case 5: // Class A (type 5)
      return new THREE.Color(0xff6b6b); // Red
    case 18: // Class B
      return new THREE.Color(0x50e3c2); // Teal
    case 19: // Class B equipment
      return new THREE.Color(0xffd43b); // Yellow
    case 24: // Static/ATON
      return new THREE.Color(0xf783ac); // Pink
    default:
      // Color based on speed
      if (sog < 1) {
        return new THREE.Color(0x666688); // Grayish for stopped
      } else if (sog < 10) {
        return new THREE.Color(0x50e3c2); // Greenish for slow
      } else if (sog < 20) {
        return new THREE.Color(0xffd43b); // Yellow for medium
      } else {
        return new THREE.Color(0xff6b6b); // Red for fast
      }
  }
}

/**
 * Start animation loop
 * @param {function} onFrame - Optional callback for FPS calculation
 */
export function animate(onFrame) {
  requestAnimationFrame(() => animate(onFrame));
  
  controls.update();
  
  if (onFrame) {
    onFrame();
  }
  
  renderer.render(scene, camera);
}

/**
 * Get the current camera view bounds
 * @returns {Object} - { minLon, maxLon, minLat, maxLat }
 */
export function getViewBounds() {
  // Calculate visible area based on camera frustum
  const frustum = new THREE.Frustum();
  const projScreenMatrix = new THREE.Matrix4().multiplyMatrices(
    camera.projectionMatrix,
    camera.matrixWorldInverse
  );
  frustum.setFromProjectionMatrix(projScreenMatrix);

  // Create a bounding box for the visible area
  const bounds = {
    minLon: -180,
    maxLon: 180,
    minLat: -90,
    maxLat: 90
  };

  // Test corners of the frustum at z=0
  const testPoints = [
    new THREE.Vector3(-180, 0, -90), // SW
    new THREE.Vector3(180, 0, -90),  // SE
    new THREE.Vector3(-180, 0, 90),  // NW
    new THREE.Vector3(180, 0, 90),   // NE
    new THREE.Vector3(0, 0, 0)      // Center
  ];

  let visibleMinLon = 180, visibleMaxLon = -180;
  let visibleMinLat = 90, visibleMaxLat = -90;

  testPoints.forEach(p => {
    if (frustum.containsPoint(p)) {
      visibleMinLon = Math.min(visibleMinLon, p.x);
      visibleMaxLon = Math.max(visibleMaxLon, p.x);
      // Convert z back to lat (z = -lat)
      visibleMinLat = Math.min(visibleMinLat, -p.z);
      visibleMaxLat = Math.max(visibleMaxLat, -p.z);
    }
  });

  // If no corners visible, use full world
  if (visibleMinLon > visibleMaxLon) {
    return bounds;
  }

  // Add some margin
  const margin = 5;
  return {
    minLon: Math.max(-180, visibleMinLon - margin),
    maxLon: Math.min(180, visibleMaxLon + margin),
    minLat: Math.max(-90, visibleMinLat - margin),
    maxLat: Math.min(90, visibleMaxLat + margin)
  };
}

/**
 * Get ship at mouse position
 * @param {number} mouseX - Normalized mouse X (-1 to 1)
 * @param {number} mouseY - Normalized mouse Y (-1 to 1)
 * @returns {Object|null} - Closest ship or null
 */
export function getShipAtPosition(mouseX, mouseY) {
  if (currentData.length === 0) return null;

  mouse.set(mouseX, mouseY);
  raycaster.setFromCamera(mouse, camera);

  // Intersect with points
  if (!pointsMesh) return null;

  const intersects = raycaster.intersectObject(pointsMesh);
  
  if (intersects.length === 0) return null;

  // Find closest ship to intersection point
  const point = intersects[0].point;
  let closestShip = null;
  let minDistance = Infinity;

  currentData.forEach(ship => {
    if (ship.lat == null || ship.lon == null) return;

    const x = ship.lon;
    const z = -ship.lat;
    const y = 0;

    const dx = point.x - x;
    const dy = point.y - y;
    const dz = point.z - z;
    const distance = dx * dx + dy * dy + dz * dz;

    if (distance < minDistance) {
      minDistance = distance;
      closestShip = ship;
    }
  });

  // Only return if reasonably close
  if (minDistance > 1) return null;
  
  return closestShip;
}

/**
 * Zoom to a specific ship
 * @param {Object} ship - Ship object with lat, lon
 */
export function zoomToShip(ship) {
  if (!ship || ship.lat == null || ship.lon == null) return;

  const targetX = ship.lon;
  const targetZ = -ship.lat;

  // Calculate new camera position
  controls.target.set(targetX, 0, targetZ);
  controls.update();

  // Adjust zoom
  camera.zoom = 10;
  camera.updateProjectionMatrix();
}

/**
 * Zoom to fit all data
 */
export function zoomToFit() {
  controls.reset();
  camera.zoom = 1;
  camera.updateProjectionMatrix();
}

/**
 * Get current camera position
 * @returns {Object} - { lon, lat, zoom }
 */
export function getCameraPosition() {
  const target = controls.target;
  return {
    lon: target.x,
    lat: -target.z,
    zoom: camera.zoom
  };
}

/**
 * Set camera position
 * @param {Object} pos - { lon, lat, zoom }
 */
export function setCameraPosition(pos) {
  controls.target.set(pos.lon, 0, -pos.lat);
  camera.zoom = pos.zoom || 1;
  camera.updateProjectionMatrix();
  controls.update();
}

/**
 * Clean up resources
 */
export function dispose() {
  if (pointsMesh) {
    scene.remove(pointsMesh);
    pointsMesh.geometry.dispose();
    pointsMesh.material.dispose();
  }
  if (arrowsMesh) {
    scene.remove(arrowsMesh);
    arrowsMesh.geometry.dispose();
    arrowsMesh.material.dispose();
  }
  
  renderer.dispose();
}

/**
 * Get Three.js objects for external access
 */
export function getRendererObjects() {
  return { scene, camera, renderer, controls };
}

export { scene, camera, renderer, controls, currentData };
