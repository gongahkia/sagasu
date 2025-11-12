# Enhanced JSON Structure for Sagasu 4

## Overview
This document defines the optimized JSON structure for room availability data and future booking data.

---

## Room Availability JSON (`backend/log/scraped_log.json`)

### Structure

```json
{
  "metadata": {
    "version": "4.0.0",
    "scraped_at": "2025-08-20T04:05:44.614Z",
    "scrape_duration_ms": 45230,
    "success": true,
    "error": null,
    "scraper_version": "prod-v1.0.0"
  },
  "config": {
    "date": "20-Aug-2025",
    "start_time": "10:00",
    "end_time": "17:00",
    "filters": {
      "buildings": ["Yong Pung How School of Law/Kwa Geok Choo Law Library"],
      "floors": ["Level 4", "Level 5"],
      "facility_types": ["Project Room"],
      "equipment": ["TV Panel"],
      "capacity": "From6To10Pax"
    }
  },
  "statistics": {
    "total_rooms": 12,
    "available_rooms": 5,
    "booked_rooms": 7,
    "partially_available_rooms": 10
  },
  "rooms": [
    {
      "id": "KGC-4.02-PR",
      "name": "KGC-4.02-PR",
      "building": "Kwa Geok Choo Law Library",
      "building_code": "KGC",
      "floor": "Level 4",
      "facility_type": "Project Room",
      "capacity": "6-10 pax",
      "equipment": ["TV Panel"],
      "timeslots": [
        {
          "start": "00:00",
          "end": "08:00",
          "status": "unavailable",
          "reason": "Outside scrape window"
        },
        {
          "start": "08:00",
          "end": "13:00",
          "status": "free"
        },
        {
          "start": "13:00",
          "end": "16:00",
          "status": "booked",
          "booking": {
            "reference": "BK-20250813-000390",
            "status": "Confirmed",
            "booker_name": "SHERMAN WAN SI EN",
            "booker_email": "sherman.wan.2022@scis.smu.edu.sg",
            "booker_org": "",
            "purpose": "fly away",
            "use_type": "AdHoc"
          }
        },
        {
          "start": "16:00",
          "end": "24:00",
          "status": "free"
        }
      ],
      "availability_summary": {
        "is_available_now": true,
        "next_available_at": null,
        "free_slots_count": 2,
        "free_duration_minutes": 540
      }
    }
  ]
}
```

### Key Improvements

1. **Metadata Section**
   - `version`: Sagasu version
   - `scraped_at`: ISO timestamp
   - `scrape_duration_ms`: Performance metric
   - `success`: Boolean flag
   - `error`: Error message if scrape failed
   - `scraper_version`: Tracks which scraper was used

2. **Config Section**
   - Preserves all filters used for scraping
   - Useful for debugging and displaying to users
   - Grouped under `filters` object

3. **Statistics Section**
   - Pre-calculated stats for quick display
   - `total_rooms`: Total rooms found
   - `available_rooms`: Rooms with at least one free slot
   - `booked_rooms`: Rooms completely booked
   - `partially_available_rooms`: Rooms with some free slots

4. **Rooms Array**
   - Array instead of object for easier filtering/sorting
   - Each room is enriched with metadata:
     - `building`: Full building name
     - `building_code`: Extracted from room name (e.g., "KGC")
     - `floor`: Floor level
     - `facility_type`: Type of room
     - `capacity`: Human-readable capacity
     - `equipment`: Array of equipment available

5. **Timeslots**
   - `status`: Normalized to "unavailable" | "free" | "booked"
   - `reason`: Optional reason for unavailability
   - `booking`: Structured object instead of string
   - Separated time into `start` and `end` for easier parsing

6. **Booking Details**
   - Parsed from string into structured object
   - All fields are searchable/filterable

7. **Availability Summary** (per room)
   - `is_available_now`: Boolean (based on current time)
   - `next_available_at`: ISO timestamp or null
   - `free_slots_count`: Number of free timeslots
   - `free_duration_minutes`: Total free minutes

8. **Removed**
   - `raw_rooms`: Redundant (can derive from rooms array)
   - `raw_timeslots`: Not useful for frontend
   - `room_mappings`: Changed to `rooms` array

---

## Booking Status JSON (`backend/log/bookings_log.json`)

For future booking feature.

### Structure

```json
{
  "metadata": {
    "version": "4.0.0",
    "last_updated": "2025-08-20T12:00:00.000Z",
    "total_bookings": 5,
    "successful_bookings": 4,
    "failed_bookings": 1
  },
  "bookings": [
    {
      "id": "auto-book-001",
      "room": "KGC-4.02-PR",
      "building": "Kwa Geok Choo Law Library",
      "date": "21-Aug-2025",
      "start_time": "14:00",
      "end_time": "16:00",
      "status": "confirmed",
      "booking_reference": "BK-20250820-001234",
      "requested_at": "2025-08-20T08:00:00.000Z",
      "confirmed_at": "2025-08-20T08:00:15.000Z",
      "purpose": "Study session",
      "error": null
    },
    {
      "id": "auto-book-002",
      "room": "KGC-4.03-PR",
      "building": "Kwa Geok Choo Law Library",
      "date": "21-Aug-2025",
      "start_time": "10:00",
      "end_time": "12:00",
      "status": "failed",
      "booking_reference": null,
      "requested_at": "2025-08-20T08:00:00.000Z",
      "confirmed_at": null,
      "purpose": "Study session",
      "error": "Room already booked"
    },
    {
      "id": "auto-book-003",
      "room": "KGC-4.04-PR",
      "building": "Kwa Geok Choo Law Library",
      "date": "21-Aug-2025",
      "start_time": "16:00",
      "end_time": "18:00",
      "status": "pending",
      "booking_reference": null,
      "requested_at": "2025-08-20T08:00:00.000Z",
      "confirmed_at": null,
      "purpose": "Group meeting",
      "error": null
    }
  ]
}
```

### Booking Statuses

- `pending`: Booking requested but not yet confirmed
- `confirmed`: Successfully booked
- `failed`: Booking failed (see error field)
- `cancelled`: Booking was cancelled

---

## Implementation Notes

### Frontend Benefits

1. **Easy Filtering**
   - Filter by building: `rooms.filter(r => r.building === 'Law Library')`
   - Filter by floor: `rooms.filter(r => r.floor === 'Level 4')`
   - Filter by availability: `rooms.filter(r => r.availability_summary.is_available_now)`

2. **No String Parsing**
   - Booking details are already structured
   - Time parsing is simplified (HH:MM format)

3. **Rich Metadata**
   - Room attributes available without additional lookups
   - Statistics pre-calculated

4. **Performance**
   - Array iteration is fast
   - Can use array methods (map, filter, reduce)

### Backend Changes Required

1. **Parse booking details** from string format to object
2. **Extract room metadata** from room name and filters
3. **Calculate statistics** during scraping
4. **Add timing/error tracking**
5. **Normalize status values**

### Migration Strategy

1. Update scraper to output new format
2. Keep old format temporarily for backward compatibility
3. Update frontend to use new format
4. Remove old format support after testing
