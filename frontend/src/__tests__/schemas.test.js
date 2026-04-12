import { describe, it, expect } from 'vitest';
import { readFileSync } from 'fs';
import { resolve, dirname } from 'path';
import { fileURLToPath } from 'url';
import { RoomDataSchema, BookingsDataSchema, TasksDataSchema } from '../schemas';
const __dirname = dirname(fileURLToPath(import.meta.url));
const logDir = resolve(__dirname, '../../../backend/log');
const loadJson = (name) => JSON.parse(readFileSync(resolve(logDir, name), 'utf8'));

describe('RoomDataSchema', () => {
  it('accepts valid scraped_log.json fixture', () => {
    const data = loadJson('scraped_log.json');
    const res = RoomDataSchema.safeParse(data);
    expect(res.success).toBe(true);
  });
  it('rejects missing required field (metadata)', () => {
    const data = loadJson('scraped_log.json');
    delete data.metadata;
    const res = RoomDataSchema.safeParse(data);
    expect(res.success).toBe(false);
  });
  it('rejects wrong type (rooms as string)', () => {
    const data = loadJson('scraped_log.json');
    data.rooms = 'not an array';
    const res = RoomDataSchema.safeParse(data);
    expect(res.success).toBe(false);
  });
  it('rejects invalid statistics type', () => {
    const data = loadJson('scraped_log.json');
    data.statistics.total_rooms = 'ten';
    const res = RoomDataSchema.safeParse(data);
    expect(res.success).toBe(false);
  });
});

describe('BookingsDataSchema', () => {
  it('accepts valid scraped_bookings.json fixture', () => {
    const data = loadJson('scraped_bookings.json');
    const res = BookingsDataSchema.safeParse(data);
    expect(res.success).toBe(true);
  });
  it('rejects missing bookings field', () => {
    const data = loadJson('scraped_bookings.json');
    delete data.bookings;
    const res = BookingsDataSchema.safeParse(data);
    expect(res.success).toBe(false);
  });
  it('rejects wrong type for total_bookings', () => {
    const data = loadJson('scraped_bookings.json');
    data.statistics.total_bookings = 'zero';
    const res = BookingsDataSchema.safeParse(data);
    expect(res.success).toBe(false);
  });
});

describe('TasksDataSchema', () => {
  it('accepts valid scraped_tasks.json fixture', () => {
    const data = loadJson('scraped_tasks.json');
    const res = TasksDataSchema.safeParse(data);
    expect(res.success).toBe(true);
  });
  it('rejects missing tasks field', () => {
    const data = loadJson('scraped_tasks.json');
    delete data.tasks;
    const res = TasksDataSchema.safeParse(data);
    expect(res.success).toBe(false);
  });
  it('rejects wrong type for pending_tasks', () => {
    const data = loadJson('scraped_tasks.json');
    data.statistics.pending_tasks = null;
    const res = TasksDataSchema.safeParse(data);
    expect(res.success).toBe(false);
  });
});
