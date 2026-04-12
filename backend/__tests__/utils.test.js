import { describe, it, expect } from 'vitest';
import {
  toMinutes, minutesToTimeStr, timeslotStr, getTodayDate,
  parseTimeRange, extractBookingTime, parseBookingDetails,
  extractRoomMetadata, normalizeStatus, calculateAvailabilitySummary,
  generateTimeslotsForRoom, mapTimeslotsToRooms,
} from '../utils.js';

describe('toMinutes', () => {
  it('converts 00:00 to 0', () => {
    expect(toMinutes('00:00')).toBe(0);
  });
  it('converts 01:30 to 90', () => {
    expect(toMinutes('01:30')).toBe(90);
  });
  it('converts 23:59 to 1439', () => {
    expect(toMinutes('23:59')).toBe(1439);
  });
  it('converts 12:00 to 720', () => {
    expect(toMinutes('12:00')).toBe(720);
  });
});

describe('minutesToTimeStr', () => {
  it('converts 0 to 00:00', () => {
    expect(minutesToTimeStr(0)).toBe('00:00');
  });
  it('converts 90 to 01:30', () => {
    expect(minutesToTimeStr(90)).toBe('01:30');
  });
  it('converts 1440 to 24:00', () => {
    expect(minutesToTimeStr(1440)).toBe('24:00');
  });
  it('pads single digit hours and minutes', () => {
    expect(minutesToTimeStr(65)).toBe('01:05');
  });
});

describe('timeslotStr', () => {
  it('formats start-end range', () => {
    expect(timeslotStr(0, 90)).toBe('00:00-01:30');
  });
  it('handles full day', () => {
    expect(timeslotStr(0, 1440)).toBe('00:00-24:00');
  });
});

describe('getTodayDate', () => {
  it('returns date in DD-Mon-YYYY format', () => {
    const result = getTodayDate();
    expect(result).toMatch(/^\d{2}-[A-Z][a-z]{2}-\d{4}$/);
  });
});

describe('parseTimeRange', () => {
  it('parses HH:MM-HH:MM into [startMin, endMin]', () => {
    expect(parseTimeRange('08:00-10:30')).toEqual([480, 630]);
  });
  it('parses midnight range', () => {
    expect(parseTimeRange('00:00-08:00')).toEqual([0, 480]);
  });
});

describe('extractBookingTime', () => {
  it('extracts time from booking string', () => {
    const raw = 'Booking Time: 09:00-10:00\nBooking Reference Number: ABC123';
    expect(extractBookingTime(raw)).toBe('09:00-10:00');
  });
  it('returns null for no match', () => {
    expect(extractBookingTime('no time here')).toBeNull();
  });
});

describe('parseBookingDetails', () => {
  it('returns null for empty string', () => {
    expect(parseBookingDetails('')).toBeNull();
  });
  it('returns null for null input', () => {
    expect(parseBookingDetails(null)).toBeNull();
  });
  it('parses multi-line booking details', () => {
    const details = [
      'Booking Time: 09:00-10:00',
      'Booking Reference Number: REF001',
      'Booking Status: Confirmed',
      'Booked for User Name: John Doe',
      'Booked for User Email Address: john@smu.edu.sg',
      'Booked for User Org Unit: SCIS',
      'Purpose of Booking: Study',
      'Use Type: Academic',
    ].join('\n');
    const result = parseBookingDetails(details);
    expect(result.reference).toBe('REF001');
    expect(result.status).toBe('Confirmed');
    expect(result.booker_name).toBe('John Doe');
    expect(result.booker_email).toBe('john@smu.edu.sg');
    expect(result.booker_org).toBe('SCIS');
    expect(result.purpose).toBe('Study');
    expect(result.use_type).toBe('Academic');
  });
  it('returns empty strings for missing fields', () => {
    const details = 'Booking Time: 09:00-10:00';
    const result = parseBookingDetails(details);
    expect(result.reference).toBe('');
    expect(result.status).toBe('');
  });
});

describe('extractRoomMetadata', () => {
  it('extracts building code and floor from room name', () => {
    const meta = extractRoomMetadata('SCIS-2.01-SR', [], [], ['Seminar Room'], ['Projector']);
    expect(meta.building_code).toBe('SCIS');
    expect(meta.building).toBe('School of Computing & Information Systems');
    expect(meta.floor).toBe('Level 2');
    expect(meta.facility_type).toBe('Seminar Room');
    expect(meta.equipment).toEqual(['Projector']);
  });
  it('handles basement floors', () => {
    const meta = extractRoomMetadata('LKCSB-B1.05-CR', [], [], [], []);
    expect(meta.floor).toBe('Basement 1');
  });
  it('falls back to filter when building code unknown', () => {
    const meta = extractRoomMetadata('XYZ-3.01-PR', ['Custom Building'], [], ['Lab'], []);
    expect(meta.building).toBe('Custom Building');
  });
  it('returns Unknown for unresolvable building', () => {
    const meta = extractRoomMetadata('XYZ-3.01-PR', [], [], [], []);
    expect(meta.building).toBe('Unknown');
  });
  it('returns Unknown floor when pattern does not match', () => {
    const meta = extractRoomMetadata('SCIS-LOBBY', [], [], [], []);
    expect(meta.floor).toBe('Unknown');
  });
});

describe('normalizeStatus', () => {
  it('maps statuses correctly', () => {
    expect(normalizeStatus('not available due to timeslot')).toBe('unavailable');
    expect(normalizeStatus('free')).toBe('free');
    expect(normalizeStatus('booked')).toBe('booked');
    expect(normalizeStatus('something else')).toBe('unknown');
  });
});

describe('calculateAvailabilitySummary', () => {
  it('counts free slots and duration', () => {
    const timeslots = [
      { start: '00:00', end: '08:00', status: 'free' },
      { start: '08:00', end: '10:00', status: 'unavailable' },
      { start: '10:00', end: '12:00', status: 'booked' },
      { start: '12:00', end: '24:00', status: 'free' },
    ];
    const summary = calculateAvailabilitySummary(timeslots);
    expect(summary.free_slots_count).toBe(2);
    expect(summary.free_duration_minutes).toBe(8 * 60 + 12 * 60); // 480 + 720 = 1200
  });
  it('returns zero for fully booked', () => {
    const timeslots = [
      { start: '00:00', end: '24:00', status: 'booked' },
    ];
    const summary = calculateAvailabilitySummary(timeslots);
    expect(summary.free_slots_count).toBe(0);
    expect(summary.free_duration_minutes).toBe(0);
    expect(summary.is_available_now).toBe(false);
    expect(summary.next_available_at).toBeNull();
  });
});

describe('generateTimeslotsForRoom', () => {
  it('generates free gaps between unavailable/booked slots', () => {
    const raw = [
      '(00:00-08:00) (not available)',
      '(22:00-24:00) (not available)',
    ];
    const result = generateTimeslotsForRoom(raw);
    expect(result.length).toBe(3); // unavailable, free gap, unavailable
    expect(result[0].status).toBe('unavailable');
    expect(result[0].start).toBe('00:00');
    expect(result[0].end).toBe('08:00');
    expect(result[1].status).toBe('free');
    expect(result[1].start).toBe('08:00');
    expect(result[1].end).toBe('22:00');
    expect(result[2].status).toBe('unavailable');
  });
  it('handles booked slot with details', () => {
    const raw = [
      '(00:00-08:00) (not available)',
      'Booking Time: 10:00-12:00\nBooking Reference Number: REF1\nBooking Status: Confirmed',
      '(22:00-24:00) (not available)',
    ];
    const result = generateTimeslotsForRoom(raw);
    const bookedSlot = result.find(s => s.status === 'booked');
    expect(bookedSlot).toBeDefined();
    expect(bookedSlot.start).toBe('10:00');
    expect(bookedSlot.end).toBe('12:00');
    expect(bookedSlot.booking.reference).toBe('REF1');
  });
  it('adds free slot at end of day', () => {
    const raw = ['(00:00-08:00) (not available)'];
    const result = generateTimeslotsForRoom(raw);
    const last = result[result.length - 1];
    expect(last.status).toBe('free');
    expect(last.end).toBe('24:00');
  });
  it('throws on unexpected format', () => {
    expect(() => generateTimeslotsForRoom(['garbage'])).toThrow('Unexpected raw_timeslot format');
  });
});

describe('mapTimeslotsToRooms', () => {
  it('maps single room with timeslots', () => {
    const rooms = ['RoomA'];
    const timeslots = [
      '(00:00-08:00) (not available)',
      '(22:00-24:00) (not available)',
    ];
    const result = mapTimeslotsToRooms(rooms, timeslots);
    expect(result['RoomA']).toBeDefined();
    expect(result['RoomA'].length).toBeGreaterThan(0);
  });
  it('maps multiple rooms by detecting new room start pattern', () => {
    const rooms = ['RoomA', 'RoomB'];
    const timeslots = [
      '(00:00-08:00) (not available)',
      '(22:00-24:00) (not available)',
      '(00:00-08:00) (not available)', // new room start
      '(20:00-24:00) (not available)',
    ];
    const result = mapTimeslotsToRooms(rooms, timeslots);
    expect(result['RoomA']).toBeDefined();
    expect(result['RoomB']).toBeDefined();
  });
  it('assigns free day to extra rooms with no timeslots', () => {
    const rooms = ['RoomA', 'RoomB'];
    const timeslots = [
      '(00:00-08:00) (not available)',
    ];
    const result = mapTimeslotsToRooms(rooms, timeslots);
    expect(result['RoomA']).toBeDefined();
    expect(result['RoomB']).toBeDefined();
    expect(result['RoomB'][0].status).toBe('free');
  });
});
