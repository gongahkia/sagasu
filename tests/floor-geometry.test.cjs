const test = require('node:test');
const assert = require('node:assert/strict');
const {roomsFor, library} = require('../web/floor-geometry.js');
test('transcribed school floors have the expected source-plan room numbers', () => {
  assert.equal(roomsFor('LKCSB:2').length, 25);
  assert.equal(roomsFor('LKCSB:3').length, 35);
  assert.equal(roomsFor('SOA:2').length, 9);
  assert.equal(roomsFor('SCIS1:2').length, 7);
  assert.equal(roomsFor('SOE/SCIS2:3').length, 18);
  assert.equal(roomsFor('LKCSB:1').length, 0);
});
test('missing rooms are not fabricated', () => {
  assert.deepEqual(roomsFor('SOE/SCIS2:5').map(room=>room.name), ['SOE/SCIS2 GSR 5-1', 'SOE/SCIS2 GSR 5-3']);
});

test('library floors retain the rooms visible on the supplied plans', () => {
  assert.deepEqual(library['LKS:2'].rooms.map(room => room[0]), [0,1,2,3,4,5]);
  assert.deepEqual(library['LKS:3'].rooms.map(room => room[0]), [1,2,3,4,5]);
  assert.equal(library['LKS:4'].rooms.length, 16);
  assert.equal(library['LKS:5'].rooms.length, 10);
  assert.equal(library['LKS:4'].rooms.some(room => room[0] === 3), false);
});
