import React, { useState, useMemo } from 'react';

const MyBookings = ({ bookingsData }) => {
  const [sortBy, setSortBy] = useState('date'); // 'date', 'status', 'price'
  const [filterStatus, setFilterStatus] = useState('all'); // 'all', 'confirmed', 'pending'

  if (!bookingsData?.bookings) {
    return null;
  }

  const { statistics, bookings } = bookingsData;

  // Filter and sort bookings
  const filteredAndSortedBookings = useMemo(() => {
    let filtered = [...bookings];

    // Filter by status
    if (filterStatus === 'confirmed') {
      filtered = filtered.filter(b => b.status === 'Confirmed');
    } else if (filterStatus === 'pending') {
      filtered = filtered.filter(b => b.status === 'Pending Confirmation');
    }

    // Sort bookings
    filtered.sort((a, b) => {
      if (sortBy === 'date') {
        // Parse dates and compare
        const dateA = new Date(a.date.split('-').reverse().join('-'));
        const dateB = new Date(b.date.split('-').reverse().join('-'));
        return dateA - dateB;
      } else if (sortBy === 'status') {
        return a.status.localeCompare(b.status);
      } else if (sortBy === 'price') {
        return b.price - a.price;
      }
      return 0;
    });

    return filtered;
  }, [bookings, sortBy, filterStatus]);

  const getStatusBadgeClass = (status) => {
    if (status === 'Confirmed') {
      return 'badge-success';
    } else if (status === 'Pending Confirmation') {
      return 'badge-warning';
    }
    return 'badge-info';
  };

  const formatDate = (dateStr) => {
    // Convert "15-Nov-2025" to a more readable format
    const date = new Date(dateStr.split('-').reverse().join('-'));
    const today = new Date();
    const tomorrow = new Date(today);
    tomorrow.setDate(tomorrow.getDate() + 1);

    // Reset time for comparison
    today.setHours(0, 0, 0, 0);
    tomorrow.setHours(0, 0, 0, 0);
    date.setHours(0, 0, 0, 0);

    if (date.getTime() === today.getTime()) {
      return 'Today';
    } else if (date.getTime() === tomorrow.getTime()) {
      return 'Tomorrow';
    }
    return dateStr;
  };

  if (bookings.length === 0) {
    return (
      <div className="card text-center py-8">
        <div className="mb-4">
          <svg className="w-16 h-16 mx-auto text-gray-300" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
          </svg>
        </div>
        <h3 className="text-lg font-semibold text-gray-700 mb-2">No Bookings Found</h3>
        <p className="text-sm text-gray-500">You don't have any bookings yet.</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Statistics Cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <div className="card">
          <div className="text-sm text-gray-600 mb-1">
            Total Bookings
          </div>
          <div className="text-3xl font-bold text-spacemacs-light-accent">
            {statistics.total_bookings}
          </div>
        </div>

        <div className="card">
          <div className="text-sm text-gray-600 mb-1">
            Confirmed
          </div>
          <div className="text-3xl font-bold text-spacemacs-light-green">
            {statistics.confirmed_bookings}
          </div>
        </div>

        <div className="card">
          <div className="text-sm text-gray-600 mb-1">
            Pending
          </div>
          <div className="text-3xl font-bold text-spacemacs-light-yellow">
            {statistics.pending_bookings}
          </div>
        </div>

        <div className="card">
          <div className="text-sm text-gray-600 mb-1">
            Total Cost
          </div>
          <div className="text-3xl font-bold text-spacemacs-light-blue">
            ${statistics.total_price.toFixed(2)}
          </div>
        </div>
      </div>

      {/* Controls */}
      <div className="card">
        <div className="flex flex-col sm:flex-row gap-4 items-start sm:items-center justify-between">
          {/* Status Filter */}
          <div className="flex gap-2 flex-wrap">
            <button
              onClick={() => setFilterStatus('all')}
              className={`px-3 py-1.5 rounded-md text-sm font-medium transition-colors ${
                filterStatus === 'all'
                  ? 'bg-spacemacs-light-accent text-white'
                  : 'bg-gray-100 text-gray-700 hover:bg-gray-200'
              }`}
            >
              All ({bookings.length})
            </button>
            <button
              onClick={() => setFilterStatus('confirmed')}
              className={`px-3 py-1.5 rounded-md text-sm font-medium transition-colors ${
                filterStatus === 'confirmed'
                  ? 'bg-spacemacs-light-green text-white'
                  : 'bg-gray-100 text-gray-700 hover:bg-gray-200'
              }`}
            >
              Confirmed ({statistics.confirmed_bookings})
            </button>
            <button
              onClick={() => setFilterStatus('pending')}
              className={`px-3 py-1.5 rounded-md text-sm font-medium transition-colors ${
                filterStatus === 'pending'
                  ? 'bg-spacemacs-light-yellow text-white'
                  : 'bg-gray-100 text-gray-700 hover:bg-gray-200'
              }`}
            >
              Pending ({statistics.pending_bookings})
            </button>
          </div>

          {/* Sort Dropdown */}
          <div className="flex items-center gap-2">
            <label className="text-sm text-gray-600">Sort by:</label>
            <select
              value={sortBy}
              onChange={(e) => setSortBy(e.target.value)}
              className="px-3 py-1.5 border border-gray-300 rounded-md text-sm focus:ring-2 focus:ring-spacemacs-light-accent focus:border-transparent"
            >
              <option value="date">Date</option>
              <option value="status">Status</option>
              <option value="price">Price</option>
            </select>
          </div>
        </div>
      </div>

      {/* Bookings List */}
      <div className="space-y-3">
        {filteredAndSortedBookings.length === 0 ? (
          <div className="card text-center py-8">
            <p className="text-gray-500">No bookings match the selected filter.</p>
          </div>
        ) : (
          filteredAndSortedBookings.map((booking, index) => (
            <BookingCard key={booking.reference_number || index} booking={booking} formatDate={formatDate} getStatusBadgeClass={getStatusBadgeClass} />
          ))
        )}
      </div>
    </div>
  );
};

const BookingCard = ({ booking, formatDate, getStatusBadgeClass }) => {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="card hover:shadow-lg transition-shadow">
      <div className="flex items-start justify-between mb-3">
        <div className="flex-1">
          <div className="flex items-center gap-2 mb-2">
            <h3 className="font-bold text-lg text-spacemacs-light-accent">
              {booking.room_name}
            </h3>
            <span className={getStatusBadgeClass(booking.status)}>
              {booking.status}
            </span>
          </div>
          <div className="text-sm text-gray-600 space-y-1">
            <div className="flex items-center gap-2">
              <svg className="w-4 h-4 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
              </svg>
              <span>
                {formatDate(booking.date)} ({booking.day_of_week}) • {booking.start_time} - {booking.end_time}
              </span>
            </div>
            <div className="flex items-center gap-2">
              <svg className="w-4 h-4 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4" />
              </svg>
              <span>{booking.building}</span>
            </div>
          </div>
        </div>

        <div className="text-right">
          <div className="text-2xl font-bold text-spacemacs-light-blue">
            ${booking.price.toFixed(2)}
          </div>
          <div className="text-xs text-gray-500">
            {booking.duration_hours}hrs
          </div>
        </div>
      </div>

      {/* Expandable Details */}
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center justify-between px-3 py-2 bg-gray-50 hover:bg-gray-100 rounded-md transition-colors text-sm"
      >
        <span className="font-medium text-gray-700">
          {expanded ? 'Hide Details' : 'Show Details'}
        </span>
        <svg
          className={`w-4 h-4 text-gray-500 transition-transform ${expanded ? 'rotate-180' : ''}`}
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
        </svg>
      </button>

      {expanded && (
        <div className="mt-3 pt-3 border-t border-gray-200 space-y-2 text-sm">
          <div className="flex justify-between">
            <span className="text-gray-600">Reference Number:</span>
            <span className="font-mono font-medium">{booking.reference_number}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-gray-600">Booked By:</span>
            <span className="font-medium">{booking.booked_by}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-gray-600">Booking Type:</span>
            <span className="font-medium">{booking.booking_type}</span>
          </div>
        </div>
      )}
    </div>
  );
};

export default MyBookings;
