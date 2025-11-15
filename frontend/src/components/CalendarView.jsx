import { useState, useMemo, useEffect } from 'react';

export default function CalendarView({ rooms, systemStatus }) {
  const [currentTime, setCurrentTime] = useState(new Date());

  // Get current time on mount
  useEffect(() => {
    setCurrentTime(new Date());
  }, []);

  // Generate hourly time slots from 00:00 to 24:00
  const timeSlots = useMemo(() => {
    const slots = [];
    for (let hour = 0; hour <= 24; hour++) {
      slots.push(`${hour.toString().padStart(2, '0')}:00`);
    }
    return slots;
  }, []);

  // Convert time string (HH:MM) to decimal hours for positioning
  const timeToDecimal = (timeStr) => {
    const [hours, minutes] = timeStr.split(':').map(Number);
    return hours + minutes / 60;
  };

  // Calculate position of current time indicator (0-100%)
  const currentTimePosition = useMemo(() => {
    const hours = currentTime.getHours();
    const minutes = currentTime.getMinutes();
    const decimal = hours + minutes / 60;
    return (decimal / 24) * 100;
  }, [currentTime]);

  // Get formatted current time
  const currentTimeString = useMemo(() => {
    return currentTime.toLocaleTimeString('en-SG', {
      hour: '2-digit',
      minute: '2-digit',
      hour12: false
    });
  }, [currentTime]);

  // Calculate timeslot blocks for visual display
  const getRoomBlocks = (room) => {
    return room.timeslots.map((slot, idx) => {
      const start = timeToDecimal(slot.start);
      const end = timeToDecimal(slot.end);
      const left = (start / 24) * 100;
      const width = ((end - start) / 24) * 100;

      let bgColor, borderColor, textColor;
      if (slot.status === 'free') {
        bgColor = 'bg-green-100';
        borderColor = 'border-green-300';
        textColor = 'text-green-800';
      } else if (slot.status === 'booked') {
        bgColor = 'bg-red-100';
        borderColor = 'border-red-300';
        textColor = 'text-red-800';
      } else {
        bgColor = 'bg-gray-100';
        borderColor = 'border-gray-300';
        textColor = 'text-gray-600';
      }

      return {
        ...slot,
        left,
        width,
        bgColor,
        borderColor,
        textColor,
        key: `${room.id}-${idx}`
      };
    });
  };

  if (rooms.length === 0) {
    return (
      <div className="card text-center py-12">
        <svg className="w-16 h-16 text-gray-400 mx-auto mb-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
        </svg>
        <p className="text-gray-600">No rooms to display in calendar view</p>
      </div>
    );
  }

  return (
    <div className="hidden md:block card overflow-x-auto">
      <div className="min-w-[1200px] relative">
        {/* Header with time markers */}
        <div className="flex border-b border-gray-200 bg-gray-50 sticky top-0 z-10">
          <div className="w-48 flex-shrink-0 p-3 font-semibold border-r border-gray-200">
            Room
          </div>
          <div className="flex-1 relative h-12">
            {/* Time markers */}
            <div className="absolute inset-0 flex">
              {timeSlots.slice(0, -1).map((time, idx) => (
                <div
                  key={time}
                  className="flex-1 border-r border-gray-200 text-xs text-gray-600 p-1 text-center"
                  style={{ minWidth: `${100 / 24}%` }}
                >
                  {idx % 2 === 0 ? time : ''}
                </div>
              ))}
            </div>

            {/* Current time indicator - only in header */}
            <div
              className="absolute top-0 bottom-0 w-0.5 bg-blue-500 z-20 pointer-events-none"
              style={{ left: `${currentTimePosition}%` }}
            >
              <div className="absolute -top-1 left-1/2 -translate-x-1/2 w-3 h-3 bg-blue-500 rounded-full" />
              <div className="absolute top-0 left-2 bg-blue-500 text-white text-xs px-1 rounded whitespace-nowrap">
                {currentTimeString}
              </div>
            </div>
          </div>
        </div>

        {/* Room rows */}
        <div className="divide-y divide-gray-200 relative">
          {rooms.map((room) => {
            const blocks = getRoomBlocks(room);

            return (
              <div key={room.id} className="flex hover:bg-gray-50 transition-colors">
                {/* Room name column */}
                <div className="w-48 flex-shrink-0 p-3 border-r border-gray-200">
                  <div className="font-medium text-sm">{room.name}</div>
                  <div className="text-xs text-gray-500 mt-1">{room.building_code}</div>
                  <div className="text-xs text-gray-500">{room.floor}</div>
                </div>

                {/* Timeline column */}
                <div className="flex-1 relative h-20">
                  {/* Grid lines */}
                  <div className="absolute inset-0 flex">
                    {timeSlots.slice(0, -1).map((time) => (
                      <div
                        key={time}
                        className="flex-1 border-r border-gray-100"
                        style={{ minWidth: `${100 / 24}%` }}
                      />
                    ))}
                  </div>

                  {/* Timeslot blocks */}
                  {blocks.map((block) => (
                    <div
                      key={block.key}
                      className={`absolute top-2 bottom-2 ${block.bgColor} ${block.borderColor} border rounded group cursor-pointer overflow-hidden`}
                      style={{
                        left: `${block.left}%`,
                        width: `${block.width}%`
                      }}
                      title={`${block.start} - ${block.end} (${block.status})`}
                    >
                      {/* Tooltip on hover */}
                      <div className="absolute inset-0 flex items-center justify-center text-xs font-medium opacity-0 group-hover:opacity-100 transition-opacity">
                        <div className={`${block.textColor} px-1 truncate`}>
                          {block.start}-{block.end}
                        </div>
                      </div>

                      {/* Show booking info on hover if booked */}
                      {block.status === 'booked' && block.booking && (
                        <div className="absolute left-0 top-full mt-1 bg-white border border-gray-300 rounded shadow-lg p-2 text-xs z-20 hidden group-hover:block min-w-[200px]">
                          <div className="font-semibold mb-1">{block.booking.reference}</div>
                          <div className="text-gray-600">{block.booking.booker_name}</div>
                          <div className="text-gray-500">{block.booking.purpose}</div>
                        </div>
                      )}
                    </div>
                  ))}
                </div>
              </div>
            );
          })}

          {/* Current time line across all rooms */}
          <div
            className="absolute top-0 bottom-0 w-0.5 bg-blue-500 z-10 pointer-events-none"
            style={{ left: `calc(12rem + ${currentTimePosition}%)` }}
          />
        </div>

        {/* Legend */}
        <div className="border-t border-gray-200 p-4 bg-gray-50">
          <div className="flex items-center gap-6 text-sm">
            <div className="font-semibold">Legend:</div>
            <div className="flex items-center gap-2">
              <div className="w-4 h-4 bg-green-100 border border-green-300 rounded"></div>
              <span>Available</span>
            </div>
            <div className="flex items-center gap-2">
              <div className="w-4 h-4 bg-red-100 border border-red-300 rounded"></div>
              <span>Booked</span>
            </div>
            <div className="flex items-center gap-2">
              <div className="w-4 h-4 bg-gray-100 border border-gray-300 rounded"></div>
              <span>Unavailable</span>
            </div>
            <div className="flex items-center gap-2">
              <div className="w-0.5 h-4 bg-blue-500"></div>
              <span>Current Time</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
