const test = require('node:test');
const assert = require('node:assert/strict');
const { filterRooms, isFree, isStale, minutes } = require('../web/app.js');
const room = (name, slots) => ({name, timeslots: slots.map(([start, end, status]) => ({start, end, status}))});

test('only rooms free for the entire requested interval are shown', () => {
  const day = {rooms: [
    room('SCIS GSR A', [['08:00', '22:00', 'free']]),
    room('Library B', [['10:00', '11:00', 'free'], ['11:00', '12:00', 'booked']]),
    room('Library C', [['10:00', '12:00', 'unavailable']]),
  ]};
  assert.deepEqual(filterRooms(day, '10:00', '12:00', '').map(r => r.name), ['SCIS GSR A']);
  assert.equal(filterRooms(day, '10:00', '12:00', ' LIBRARY ').length, 0);
  assert.equal(filterRooms(day, '10:00', '12:00', ' scis ').length, 1);
});
test('bounds and invalid or reversed windows', () => {
  const r = room('A', [['10:00', '12:00', 'free']]);
  assert.equal(isFree(r, '10:00', '12:00'), true);
  assert.equal(isFree(r, '09:30', '12:00'), false);
  assert.equal(isFree(r, '12:00', '10:00'), false);
  assert.equal(isFree(r, '10:00', '10:00'), false);
  assert.ok(Number.isNaN(minutes('25:00')));
});
test('empty results remain empty', () => {
  assert.deepEqual(filterRooms({rooms: []}, '10:00', '12:00', ''), []);
});
test('staleness is based on last successful update, even without redeployment', () => {
  const now = Date.parse('2026-10-31T12:00:00Z');
  assert.equal(isStale({updated_at: '2026-10-31T10:00:00Z'}, now), false);
  assert.equal(isStale({updated_at: '2026-10-29T10:00:00Z'}, now), true);
  assert.equal(isStale({updated_at: 'invalid'}, now), true);
});
