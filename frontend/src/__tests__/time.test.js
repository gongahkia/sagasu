import { describe, it, expect } from 'vitest';
import {
  formatTime, formatDuration, formatDateTime, formatDateShort,
  getStatusColor, getStatusBadgeClass, getBookingStatusColor,
} from '../utils/time';

describe('formatTime', () => {
  it('returns input unchanged', () => {
    expect(formatTime('08:00')).toBe('08:00');
    expect(formatTime('23:59')).toBe('23:59');
  });
});

describe('formatDuration', () => {
  it('formats minutes only', () => {
    expect(formatDuration(30)).toBe('30m');
    expect(formatDuration(1)).toBe('1m');
  });
  it('formats hours only', () => {
    expect(formatDuration(60)).toBe('1h');
    expect(formatDuration(120)).toBe('2h');
  });
  it('formats hours and minutes', () => {
    expect(formatDuration(90)).toBe('1h 30m');
    expect(formatDuration(150)).toBe('2h 30m');
  });
  it('handles zero', () => {
    expect(formatDuration(0)).toBe('0m');
  });
});

describe('formatDateTime', () => {
  it('formats ISO string to locale string', () => {
    const result = formatDateTime('2025-06-15T10:30:00.000Z');
    expect(typeof result).toBe('string');
    expect(result.length).toBeGreaterThan(0);
  });
});

describe('formatDateShort', () => {
  it('returns input unchanged', () => {
    expect(formatDateShort('15-Nov-2025')).toBe('15-Nov-2025');
  });
});

describe('getStatusColor', () => {
  it('returns correct classes', () => {
    expect(getStatusColor('free')).toBe('text-spacemacs-light-green');
    expect(getStatusColor('booked')).toBe('text-spacemacs-light-red');
    expect(getStatusColor('unavailable')).toBe('text-gray-400');
    expect(getStatusColor('unknown')).toBe('text-gray-600');
  });
});

describe('getStatusBadgeClass', () => {
  it('returns correct classes', () => {
    expect(getStatusBadgeClass('free')).toBe('badge-success');
    expect(getStatusBadgeClass('booked')).toBe('badge-danger');
    expect(getStatusBadgeClass('unavailable')).toContain('bg-gray-100');
    expect(getStatusBadgeClass('other')).toBe('badge-info');
  });
});

describe('getBookingStatusColor', () => {
  it('returns correct classes', () => {
    expect(getBookingStatusColor('confirmed')).toBe('badge-success');
    expect(getBookingStatusColor('pending')).toBe('badge-warning');
    expect(getBookingStatusColor('failed')).toBe('badge-danger');
    expect(getBookingStatusColor('cancelled')).toContain('bg-gray-100');
    expect(getBookingStatusColor('other')).toBe('badge-info');
  });
});
