const test = require('node:test');
const assert = require('node:assert/strict');
const {buildingFor, counts} = require('../web/map-data.js');
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
