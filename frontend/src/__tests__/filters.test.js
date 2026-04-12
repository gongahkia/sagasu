import { describe, it, expect } from 'vitest';
import { filterRooms, getUniqueValues, sortRooms } from '../utils/filters';

const makeRoom = (overrides = {}) => ({
  id: 'SCIS-2.01',
  name: 'SCIS-2.01',
  building: 'School of Computing & Information Systems',
  floor: 'Level 2',
  facility_type: 'Seminar Room',
  availability_summary: {
    is_available_now: true,
    free_slots_count: 3,
    free_duration_minutes: 360,
  },
  ...overrides,
});
const defaultFilters = {
  search: '',
  availability: 'all',
  buildings: [],
  floors: [],
  facilityTypes: [],
  showFavoritesOnly: false,
  favoriteIds: [],
};

describe('filterRooms', () => {
  it('returns empty array for null rooms', () => {
    expect(filterRooms(null, defaultFilters)).toEqual([]);
  });
  it('returns all rooms with default filters', () => {
    const rooms = [makeRoom(), makeRoom({ id: 'SCIS-2.02', name: 'SCIS-2.02' })];
    expect(filterRooms(rooms, defaultFilters)).toHaveLength(2);
  });
  it('filters by search on name', () => {
    const rooms = [makeRoom({ name: 'SCIS-2.01' }), makeRoom({ id: 'SOA-3.01', name: 'SOA-3.01', building: 'School of Accountancy' })];
    const result = filterRooms(rooms, { ...defaultFilters, search: 'SCIS' });
    expect(result).toHaveLength(1);
    expect(result[0].name).toBe('SCIS-2.01');
  });
  it('filters by search on building (case-insensitive)', () => {
    const rooms = [makeRoom(), makeRoom({ id: 'SOA-3.01', name: 'SOA-3.01', building: 'School of Accountancy' })];
    const result = filterRooms(rooms, { ...defaultFilters, search: 'accountancy' });
    expect(result).toHaveLength(1);
  });
  it('filters by availability: available', () => {
    const rooms = [
      makeRoom({ availability_summary: { is_available_now: true, free_slots_count: 1, free_duration_minutes: 60 } }),
      makeRoom({ id: 'B', name: 'B', availability_summary: { is_available_now: false, free_slots_count: 0, free_duration_minutes: 0 } }),
    ];
    const result = filterRooms(rooms, { ...defaultFilters, availability: 'available' });
    expect(result).toHaveLength(1);
    expect(result[0].availability_summary.is_available_now).toBe(true);
  });
  it('filters by availability: booked', () => {
    const rooms = [
      makeRoom({ availability_summary: { is_available_now: true, free_slots_count: 3, free_duration_minutes: 180 } }),
      makeRoom({ id: 'B', name: 'B', availability_summary: { is_available_now: false, free_slots_count: 0, free_duration_minutes: 0 } }),
    ];
    const result = filterRooms(rooms, { ...defaultFilters, availability: 'booked' });
    expect(result).toHaveLength(1);
    expect(result[0].availability_summary.free_slots_count).toBe(0);
  });
  it('filters by buildings', () => {
    const rooms = [makeRoom(), makeRoom({ id: 'SOA', name: 'SOA', building: 'School of Accountancy' })];
    const result = filterRooms(rooms, { ...defaultFilters, buildings: ['School of Accountancy'] });
    expect(result).toHaveLength(1);
    expect(result[0].building).toBe('School of Accountancy');
  });
  it('filters by floors', () => {
    const rooms = [makeRoom({ floor: 'Level 2' }), makeRoom({ id: 'B', name: 'B', floor: 'Level 3' })];
    const result = filterRooms(rooms, { ...defaultFilters, floors: ['Level 2'] });
    expect(result).toHaveLength(1);
  });
  it('filters by facilityTypes', () => {
    const rooms = [makeRoom({ facility_type: 'Seminar Room' }), makeRoom({ id: 'B', name: 'B', facility_type: 'Lab' })];
    const result = filterRooms(rooms, { ...defaultFilters, facilityTypes: ['Lab'] });
    expect(result).toHaveLength(1);
    expect(result[0].facility_type).toBe('Lab');
  });
  it('filters by favorites only', () => {
    const rooms = [makeRoom({ id: 'A' }), makeRoom({ id: 'B', name: 'B' })];
    const result = filterRooms(rooms, { ...defaultFilters, showFavoritesOnly: true, favoriteIds: ['A'] });
    expect(result).toHaveLength(1);
    expect(result[0].id).toBe('A');
  });
  it('combines multiple filters', () => {
    const rooms = [
      makeRoom({ id: 'A', name: 'SCIS-2.01', building: 'SCIS', floor: 'Level 2' }),
      makeRoom({ id: 'B', name: 'SOA-3.01', building: 'SOA', floor: 'Level 3' }),
    ];
    const result = filterRooms(rooms, { ...defaultFilters, search: 'SCIS', floors: ['Level 2'] });
    expect(result).toHaveLength(1);
  });
});

describe('getUniqueValues', () => {
  it('returns empty for null rooms', () => {
    expect(getUniqueValues(null, 'building')).toEqual([]);
  });
  it('returns sorted unique values', () => {
    const rooms = [
      makeRoom({ building: 'B' }),
      makeRoom({ building: 'A' }),
      makeRoom({ building: 'B' }),
    ];
    expect(getUniqueValues(rooms, 'building')).toEqual(['A', 'B']);
  });
});

describe('sortRooms', () => {
  it('returns empty for null rooms', () => {
    expect(sortRooms(null, 'name')).toEqual([]);
  });
  it('sorts by name', () => {
    const rooms = [makeRoom({ name: 'Z' }), makeRoom({ name: 'A' })];
    const result = sortRooms(rooms, 'name');
    expect(result[0].name).toBe('A');
    expect(result[1].name).toBe('Z');
  });
  it('sorts by building', () => {
    const rooms = [makeRoom({ building: 'Z' }), makeRoom({ building: 'A' })];
    const result = sortRooms(rooms, 'building');
    expect(result[0].building).toBe('A');
  });
  it('sorts by availability (available first, then by free duration)', () => {
    const rooms = [
      makeRoom({ name: 'Booked', availability_summary: { is_available_now: false, free_slots_count: 0, free_duration_minutes: 0 } }),
      makeRoom({ name: 'Free', availability_summary: { is_available_now: true, free_slots_count: 2, free_duration_minutes: 120 } }),
      makeRoom({ name: 'FreeLong', availability_summary: { is_available_now: true, free_slots_count: 3, free_duration_minutes: 360 } }),
    ];
    const result = sortRooms(rooms, 'availability');
    expect(result[0].name).toBe('FreeLong');
    expect(result[1].name).toBe('Free');
    expect(result[2].name).toBe('Booked');
  });
  it('returns copy for unknown sort key', () => {
    const rooms = [makeRoom({ name: 'A' })];
    const result = sortRooms(rooms, 'unknown');
    expect(result).toHaveLength(1);
    expect(result).not.toBe(rooms); // new array
  });
});
