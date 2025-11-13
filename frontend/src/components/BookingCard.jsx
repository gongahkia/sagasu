import React, { useState } from 'react';
import { getBookingStatusColor } from '../utils/time';
import EnvOverlay from './EnvOverlay';

const BookingCard = ({ bookings, systemStatus }) => {
  const [expanded, setExpanded] = useState(false);

  if (!bookings || !bookings.bookings || bookings.bookings.length === 0) {
    return (
      <div className="card">
        <div className="flex items-center gap-3 mb-2">
          <div className="p-2 bg-spacemacs-light-blue/10 rounded-lg">
            <svg className="w-6 h-6 text-spacemacs-light-blue" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
            </svg>
          </div>
          <div>
            <h3 className="font-bold text-lg">Auto-Booking Status</h3>
            <p className="text-sm text-gray-600">
              No bookings yet
            </p>
          </div>
        </div>
        <p className="text-sm text-gray-500">
          Automatic booking feature coming soon. This dashboard will show your scheduled room bookings.
        </p>
      </div>
    );
  }

  const { metadata, bookings: bookingList } = bookings;
  const recentBookings = bookingList.slice(0, 5);

  return (
    <div className="card relative">
      <div className="flex items-center justify-between mb-4">
        <div className="flex items-center gap-3">
          <div className="p-2 bg-spacemacs-light-blue/10 rounded-lg">
            <svg className="w-6 h-6 text-spacemacs-light-blue" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
            </svg>
          </div>
          <div>
            <h3 className="font-bold text-lg">Auto-Booking Status</h3>
            <p className="text-sm text-gray-600">
              {metadata.total_bookings} total booking(s)
            </p>
          </div>
        </div>

        <button
          onClick={() => setExpanded(!expanded)}
          className="p-2 rounded-md hover:bg-gray-100 transition-colors"
        >
          <svg
            className={`w-5 h-5 transition-transform ${expanded ? 'rotate-180' : ''}`}
            fill="none"
            stroke="currentColor"
            viewBox="0 0 24 24"
          >
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
          </svg>
        </button>
      </div>

      <div className="grid grid-cols-3 gap-4 mb-4">
        <div className="text-center">
          <div className="text-2xl font-bold text-spacemacs-light-green">
            {metadata.successful_bookings || 0}
          </div>
          <div className="text-xs text-gray-600">Confirmed</div>
        </div>
        <div className="text-center">
          <div className="text-2xl font-bold text-spacemacs-light-yellow">
            {bookingList.filter(b => b.status === 'pending').length}
          </div>
          <div className="text-xs text-gray-600">Pending</div>
        </div>
        <div className="text-center">
          <div className="text-2xl font-bold text-spacemacs-light-red">
            {metadata.failed_bookings || 0}
          </div>
          <div className="text-xs text-gray-600">Failed</div>
        </div>
      </div>

      {expanded && recentBookings.length > 0 && (
        <div className="pt-4 border-t space-y-2">
          <div className="text-sm font-medium mb-2">Recent Bookings</div>
          {recentBookings.map((booking) => (
            <div
              key={booking.id}
              className="p-3 rounded-md bg-gray-50 space-y-1"
            >
              <div className="flex items-center justify-between">
                <span className="font-medium text-sm">{booking.room}</span>
                <span className={getBookingStatusColor(booking.status)}>
                  {booking.status}
                </span>
              </div>
              <div className="text-xs text-gray-600">
                {booking.date} • {booking.start_time} - {booking.end_time}
              </div>
              <div className="text-xs text-gray-500">
                {booking.building}
              </div>
              {booking.error && (
                <div className="text-xs text-spacemacs-light-red mt-1">
                  Error: {booking.error}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {/* Environment Variable Overlay */}
      {systemStatus && systemStatus.shouldShowOverlay && (
        <EnvOverlay
          envStatus={systemStatus.envStatus}
          dataStatus={systemStatus.dataStatus}
        />
      )}
    </div>
  );
};

export default BookingCard;
