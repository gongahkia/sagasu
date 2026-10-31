const test = require('node:test');
const assert = require('node:assert/strict');
const {buildingFor, counts, buildings, project, floorPlans, floorFor} = require('../web/map-data.js');
test('maps known room prefixes without confusing SCIS 1 and SCIS 2', () => {
  assert.equal(buildingFor('SCIS1 GSR 2-1').id, 'SCIS1');
  assert.equal(buildingFor('SOE/SCIS2 GSR 2-1').id, 'SOE/SCIS2');
  assert.equal(buildingFor('SCIS10 GSR 2-1'), null);
});
test('counts matching available rooms and retains unknown buildings', () => {
  const result = counts([{name:'LKCSB GSR 1-1'}, {name:'LKCSB GSR 1-2'}, {name:'Unknown GSR'}]);
  assert.equal(result.buildings.LKCSB, 2);
  assert.equal(result.unmapped, 1);
});

test('campus coordinates preserve the north-south arrangement', () => {
  const byId = Object.fromEntries(buildings.map(b => [b.id, project(b.lat, b.lon)]));
  assert.ok(byId['SOE/SCIS2'].z < byId.SCIS1.z);
  assert.ok(byId.SCIS1.z < byId.SOA.z);
  assert.ok(byId.SOA.z < byId.LKCSB.z);
});

test('floor plans match room building and floor without inventing missing plans', () => {
  assert.equal(floorFor('LKCSB GSR 2-10'), 'LKCSB:2');
  assert.equal(floorFor('SOE/SCIS2 GSR 3-1'), 'SOE/SCIS2:3');
  assert.ok(floorPlans['SCIS1:2']);
  assert.equal(floorPlans['LKCSB:1'], undefined);
});
