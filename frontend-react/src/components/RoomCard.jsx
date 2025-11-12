import React, { useState } from 'react';
import { getStatusBadgeClass, formatDuration } from '../utils/time';

const RoomCard = ({ room, isFavorite, toggleFavorite }) => {
  const [expanded, setExpanded] = useState(false);
  const [selectedBooking, setSelectedBooking] = useState(null);

  const handleBookingClick = (timeslot) => {
    if (timeslot.booking) {
      setSelectedBooking(timeslot.booking);
    }
  };

  const closeModal = () => {
    setSelectedBooking(null);
  };

  return (
    <>
      <div className="card hover:shadow-lg transition-shadow">
        <div className="flex items-start justify-between mb-3">
          <div className="flex-1">
            <div className="flex items-center gap-2 mb-1">
              <h3 className="font-bold text-lg text-spacemacs-light-accent dark:text-spacemacs-dark-accent">
                {room.name}
              </h3>
              {room.availability_summary.is_available_now && (
                <span className="badge-success">Available Now</span>
              )}
            </div>
            <div className="text-sm text-gray-600 dark:text-gray-400">
              {room.building} • {room.floor}
            </div>
            <div className="text-xs text-gray-500 dark:text-gray-500 mt-1">
              {room.facility_type}
            </div>
          </div>

          <button
            onClick={() => toggleFavorite(room.id)}
            className="p-2 rounded-md hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors"
            aria-label={isFavorite ? 'Remove from favorites' : 'Add to favorites'}
          >
            <svg
              className={`w-5 h-5 transition-colors ${
                isFavorite
                  ? 'fill-spacemacs-light-yellow text-spacemacs-light-yellow dark:fill-spacemacs-dark-yellow dark:text-spacemacs-dark-yellow'
                  : 'fill-none text-gray-400'
              }`}
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M11.049 2.927c.3-.921 1.603-.921 1.902 0l1.519 4.674a1 1 0 00.95.69h4.915c.969 0 1.371 1.24.588 1.81l-3.976 2.888a1 1 0 00-.363 1.118l1.518 4.674c.3.922-.755 1.688-1.538 1.118l-3.976-2.888a1 1 0 00-1.176 0l-3.976 2.888c-.783.57-1.838-.197-1.538-1.118l1.518-4.674a1 1 0 00-.363-1.118l-3.976-2.888c-.784-.57-.38-1.81.588-1.81h4.914a1 1 0 00.951-.69l1.519-4.674z"
              />
            </svg>
          </button>
        </div>

        <div className="space-y-2 mb-3">
          <div className="flex items-center justify-between text-sm">
            <span className="text-gray-600 dark:text-gray-400">Free Duration:</span>
            <span className="font-medium">
              {formatDuration(room.availability_summary.free_duration_minutes)}
            </span>
          </div>
          <div className="flex items-center justify-between text-sm">
            <span className="text-gray-600 dark:text-gray-400">Free Slots:</span>
            <span className="font-medium">
              {room.availability_summary.free_slots_count}
            </span>
          </div>
          {room.equipment.length > 0 && (
            <div className="flex items-start gap-2 text-sm">
              <span className="text-gray-600 dark:text-gray-400">Equipment:</span>
              <div className="flex flex-wrap gap-1">
                {room.equipment.map((eq, idx) => (
                  <span key={idx} className="badge-info text-xs">
                    {eq}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>

        <button
          onClick={() => setExpanded(!expanded)}
          className="w-full flex items-center justify-between text-sm text-spacemacs-light-accent dark:text-spacemacs-dark-accent hover:opacity-80 transition-opacity"
        >
          <span className="font-medium">
            {expanded ? 'Hide Timeslots' : 'Show Timeslots'}
          </span>
          <svg
            className={`w-4 h-4 transition-transform ${expanded ? 'rotate-180' : ''}`}
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
          >
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
          </svg>
        </button>

        {expanded && (
          <div className="mt-4 pt-4 border-t space-y-2">
            {room.timeslots.map((slot, idx) => (
              <div
                key={idx}
                className={`flex items-center justify-between p-2 rounded-md text-sm ${
                  slot.status === 'booked'
                    ? 'bg-red-50 dark:bg-red-900/20 cursor-pointer hover:bg-red-100 dark:hover:bg-red-900/30'
                    : 'bg-gray-50 dark:bg-gray-800/50'
                }`}
                onClick={() => slot.status === 'booked' && handleBookingClick(slot)}
              >
                <span className="font-medium">
                  {slot.start} - {slot.end}
                </span>
                <span className={`${getStatusBadgeClass(slot.status)}`}>
                  {slot.status}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>

      {selectedBooking && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm p-4"
          onClick={closeModal}
        >
          <div
            className="bg-white dark:bg-spacemacs-dark-bg-alt rounded-lg shadow-xl max-w-md w-full p-6"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between mb-4">
              <h3 className="text-xl font-bold text-spacemacs-light-accent dark:text-spacemacs-dark-accent">
                Booking Details
              </h3>
              <button
                onClick={closeModal}
                className="p-2 rounded-md hover:bg-gray-100 dark:hover:bg-gray-800 transition-colors"
              >
                <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" />
                </svg>
              </button>
            </div>

            <div className="space-y-3">
              <div>
                <div className="text-sm text-gray-600 dark:text-gray-400">Reference</div>
                <div className="font-medium">{selectedBooking.reference}</div>
              </div>
              <div>
                <div className="text-sm text-gray-600 dark:text-gray-400">Status</div>
                <div className="font-medium">{selectedBooking.status}</div>
              </div>
              <div>
                <div className="text-sm text-gray-600 dark:text-gray-400">Booked By</div>
                <div className="font-medium">{selectedBooking.booker_name}</div>
              </div>
              <div>
                <div className="text-sm text-gray-600 dark:text-gray-400">Email</div>
                <div className="font-medium text-sm">{selectedBooking.booker_email}</div>
              </div>
              {selectedBooking.purpose && (
                <div>
                  <div className="text-sm text-gray-600 dark:text-gray-400">Purpose</div>
                  <div className="font-medium">{selectedBooking.purpose}</div>
                </div>
              )}
              <div>
                <div className="text-sm text-gray-600 dark:text-gray-400">Type</div>
                <div className="font-medium">{selectedBooking.use_type}</div>
              </div>
            </div>
          </div>
        </div>
      )}
    </>
  );
};

export default RoomCard;
