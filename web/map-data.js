/* Building names follow SMU's published campus map; coordinates are schematic. */
(function (root) {
  const buildings = [
    { id: 'LKCSB', name: 'Lee Kong Chian School of Business', prefix: 'LKCSB GSR', x: -4, z: -2 },
    { id: 'SOA', name: 'School of Accountancy', prefix: 'SOA GSR', x: -1.4, z: 1.8 },
    { id: 'SCIS1', name: 'School of Computing and Information Systems 1', prefix: 'SCIS1 GSR', x: 1.6, z: 1.8 },
    { id: 'SOE/SCIS2', name: 'School of Economics / SCIS 2', prefix: 'SOE/SCIS2 GSR', x: 4, z: -1.8 },
  ];
  function buildingFor(name) {
    const normalized = name.trim().toUpperCase();
    return buildings.find(building => normalized.startsWith(building.prefix + ' ') || normalized === building.prefix) || null;
  }
  function counts(rooms) {
    const result = Object.fromEntries(buildings.map(building => [building.id, 0]));
    let unmapped = 0;
    for (const room of rooms) {
      const building = buildingFor(room.name);
      if (building) result[building.id]++; else unmapped++;
    }
    return { buildings: result, unmapped };
  }
  const api = { buildings, buildingFor, counts };
  root.SagasuMapData = api;
  if (typeof module !== 'undefined') module.exports = api;
})(typeof globalThis === 'undefined' ? this : globalThis);
