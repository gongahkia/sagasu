import { z } from 'zod';
const MetadataSchema = z.object({
  version: z.string().optional(),
  scraped_at: z.string().optional(),
  scrape_duration_ms: z.number().optional(),
  success: z.boolean(),
  error: z.string().nullable().optional(),
  scraper_version: z.string().optional(),
}).passthrough();
const TimeslotSchema = z.object({
  start: z.string(),
  end: z.string(),
  status: z.string(),
  reason: z.string().optional(),
}).passthrough();
const AvailabilitySummarySchema = z.object({
  is_available_now: z.boolean(),
  next_available_at: z.string().nullable().optional(),
  free_slots_count: z.number(),
  free_duration_minutes: z.number(),
}).passthrough();
const RoomSchema = z.object({
  id: z.string(),
  name: z.string(),
  building: z.string(),
  building_code: z.string().optional(),
  floor: z.string().optional(),
  facility_type: z.string().optional(),
  equipment: z.array(z.string()).optional(),
  timeslots: z.array(TimeslotSchema),
  availability_summary: AvailabilitySummarySchema.optional(),
}).passthrough();
export const RoomDataSchema = z.object({
  metadata: MetadataSchema,
  config: z.object({}).passthrough().optional(),
  statistics: z.object({
    total_rooms: z.number(),
    available_rooms: z.number(),
    booked_rooms: z.number(),
    partially_available_rooms: z.number(),
  }).passthrough(),
  rooms: z.array(RoomSchema),
}).passthrough();
const BookingSchema = z.object({}).passthrough();
export const BookingsDataSchema = z.object({
  metadata: MetadataSchema,
  statistics: z.object({
    total_bookings: z.number(),
    confirmed_bookings: z.number(),
    pending_bookings: z.number(),
    total_price: z.number(),
  }).passthrough(),
  bookings: z.array(BookingSchema),
}).passthrough();
const TaskSchema = z.object({}).passthrough();
export const TasksDataSchema = z.object({
  metadata: MetadataSchema,
  statistics: z.object({
    total_tasks: z.number(),
    pending_tasks: z.number(),
    approved_tasks: z.number(),
    rejected_tasks: z.number(),
  }).passthrough(),
  tasks: z.array(TaskSchema),
}).passthrough();
