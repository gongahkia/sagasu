const container = document.getElementById('campus-canvas');
const fallback = document.getElementById('map-fallback');
const data = window.SagasuMapData;
try {
  const THREE = await import('https://cdn.jsdelivr.net/npm/three@0.170.0/build/three.module.js');
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0xe8efe4);
  const camera = new THREE.OrthographicCamera(-7, 7, 5, -5, 0.1, 100);
  camera.position.set(0, 14, 18); camera.lookAt(0, 0, 0);
  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  container.append(renderer.domElement);
  scene.add(new THREE.HemisphereLight(0xffffff, 0x8aab91, 2.3));
  const ground = new THREE.Mesh(new THREE.BoxGeometry(10, 0.25, 12), new THREE.MeshStandardMaterial({ color: 0xc9d9c3 }));
  ground.position.y = -0.2; scene.add(ground);
  const meshes = new Map(), labels = new Map();
  function labelFor(text) {
    const canvas = document.createElement('canvas'); canvas.width = 512; canvas.height = 100;
    const ctx = canvas.getContext('2d'); ctx.fillStyle = '#ffffff'; ctx.fillRect(0, 0, 512, 100);
    ctx.fillStyle = '#20352e'; ctx.font = 'bold 30px system-ui'; ctx.textAlign = 'center';
    ctx.fillText(text, 256, 64); return new THREE.CanvasTexture(canvas);
  }
  for (const building of data.buildings) {
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(1.8, 1.2, 1.8), new THREE.MeshStandardMaterial({ color: 0x849a8b }));
    const { x, z } = data.project(building.lat, building.lon);
    mesh.position.set(x, 0.6, z); scene.add(mesh); meshes.set(building.id, mesh);
    const label = new THREE.Sprite(new THREE.SpriteMaterial({map: labelFor(building.id), transparent: true}));
    label.scale.set(2.7, 0.53, 1); label.position.set(x, 1.9, z); scene.add(label); labels.set(building.id, label);
  }
  function resize() {
    const width = container.clientWidth, height = container.clientHeight;
    if (!width || !height) return;
    const aspect = width / height;
    camera.left = -6.8 * aspect; camera.right = 6.8 * aspect; camera.top = 6.8; camera.bottom = -6.8;
    camera.updateProjectionMatrix(); renderer.setSize(width, height); renderer.render(scene, camera);
  }
  function update(event) {
    const counts = data.counts(event.detail.rooms);
    for (const building of data.buildings) {
      const mesh = meshes.get(building.id), count = counts.buildings[building.id];
      mesh.material.color.setHex(count ? 0x255e47 : 0x9aaca0);
      const label = labels.get(building.id);
      label.material.map.dispose(); label.material.map = labelFor(`${building.id} · ${count} free`); label.material.needsUpdate = true;
    }
    renderer.render(scene, camera);
  }
  window.addEventListener('sagasu:results', update);
  new ResizeObserver(resize).observe(container); resize();
  fallback.hidden = true;
  window.dispatchEvent(new Event("sagasu:map-ready"));
} catch (_) {
  fallback.textContent = 'The 3D view is unavailable. Building counts and room results remain available below.';
}
