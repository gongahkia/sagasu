import React, { useState, useMemo } from 'react';
import Header from './components/Header';
import Statistics, { ScrapeConfig } from './components/Statistics';
import Filters from './components/Filters';
import RoomCard from './components/RoomCard';
import BookingCard from './components/BookingCard';
import { useRoomData, useBookingData } from './hooks/useRoomData';
import { useFavorites } from './hooks/useFavorites';
import { filterRooms, getUniqueValues, sortRooms } from './utils/filters';
import { checkSystemStatus } from './utils/envCheck';
import { EnvStatusCard } from './components/EnvOverlay';

function App() {
  const { data, loading, error, refetch } = useRoomData(false);
  const { bookings } = useBookingData();
  const { favorites, toggleFavorite, isFavorite } = useFavorites();

  const [filters, setFilters] = useState({
    search: '',
    availability: 'all',
    buildings: [],
    floors: [],
    facilityTypes: [],
    showFavoritesOnly: false,
    favoriteIds: favorites,
  });

  const [viewMode, setViewMode] = useState('grid');
  const [sortBy, setSortBy] = useState('name');

  // Check system status (env variables and data validity)
  const systemStatus = useMemo(() => checkSystemStatus(data), [data]);

  // Update favorites in filters when they change
  React.useEffect(() => {
    setFilters(prev => ({ ...prev, favoriteIds: favorites }));
  }, [favorites]);

  // Extract unique values for filters
  const buildings = useMemo(() =>
    data?.rooms ? getUniqueValues(data.rooms, 'building') : []
  , [data]);

  const floors = useMemo(() =>
    data?.rooms ? getUniqueValues(data.rooms, 'floor') : []
  , [data]);

  const facilityTypes = useMemo(() =>
    data?.rooms ? getUniqueValues(data.rooms, 'facility_type') : []
  , [data]);

  // Filter and sort rooms
  const filteredRooms = useMemo(() => {
    if (!data?.rooms) return [];
    const filtered = filterRooms(data.rooms, filters);
    return sortRooms(filtered, sortBy);
  }, [data, filters, sortBy]);

  const toggleFavoritesFilter = () => {
    setFilters(prev => ({
      ...prev,
      showFavoritesOnly: !prev.showFavoritesOnly,
    }));
  };

  if (loading && !data) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="text-center">
          <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-spacemacs-light-accent mx-auto mb-4"></div>
          <p className="text-gray-600">Loading room data...</p>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="card max-w-md mx-4">
          <div className="text-center">
            <svg className="w-12 h-12 text-spacemacs-light-red mx-auto mb-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            <h2 className="text-xl font-bold mb-2">Failed to Load Data</h2>
            <p className="text-gray-600 mb-4">{error}</p>
            <button onClick={refetch} className="btn-primary">
              Retry
            </button>
          </div>
        </div>
      </div>
    );
  }

  if (!data || !data.rooms) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="card max-w-md mx-4">
          <div className="text-center">
            <p className="text-gray-600">No room data available</p>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen">
      <Header
        lastUpdated={data.metadata.scraped_at}
      />

      <main className="container mx-auto px-4 py-8 space-y-6">
        {/* Booking Status Card */}
        <BookingCard bookings={bookings} systemStatus={systemStatus} />

        {/* Statistics - 4 Cards */}
        <Statistics statistics={data.statistics} />

        {/* Scrape Config and Environment Status - Side by Side */}
        <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
          <ScrapeConfig config={data.config} />
          <EnvStatusCard
            envStatus={systemStatus.envStatus}
            dataStatus={systemStatus.dataStatus}
          />
        </div>

        {/* Filters */}
        <Filters
          filters={filters}
          setFilters={setFilters}
          buildings={buildings}
          floors={floors}
          facilityTypes={facilityTypes}
          showFavorites={filters.showFavoritesOnly}
          toggleFavorites={toggleFavoritesFilter}
        />

        {/* View Controls */}
        <div className="flex items-center justify-between">
          <div className="text-sm text-gray-600">
            Showing {filteredRooms.length} of {data.rooms.length} rooms
          </div>

          <div className="flex items-center gap-4">
            <select
              value={sortBy}
              onChange={(e) => setSortBy(e.target.value)}
              className="input py-2 text-sm"
            >
              <option value="name">Sort by Name</option>
              <option value="building">Sort by Building</option>
              <option value="availability">Sort by Availability</option>
            </select>

            <div className="flex gap-2">
              <button
                onClick={() => setViewMode('grid')}
                className={`p-2 rounded-md transition-colors ${
                  viewMode === 'grid'
                    ? 'bg-spacemacs-light-accent text-white'
                    : 'bg-gray-100 text-gray-700'
                }`}
              >
                <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2V6zM14 6a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2V6zM4 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2H6a2 2 0 01-2-2v-2zM14 16a2 2 0 012-2h2a2 2 0 012 2v2a2 2 0 01-2 2h-2a2 2 0 01-2-2v-2z" />
                </svg>
              </button>
              <button
                onClick={() => setViewMode('list')}
                className={`p-2 rounded-md transition-colors ${
                  viewMode === 'list'
                    ? 'bg-spacemacs-light-accent text-white'
                    : 'bg-gray-100 text-gray-700'
                }`}
              >
                <svg className="w-5 h-5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 6h16M4 12h16M4 18h16" />
                </svg>
              </button>
            </div>
          </div>
        </div>

        {/* Rooms Grid/List */}
        {filteredRooms.length === 0 ? (
          <div className="card text-center py-12">
            <svg className="w-16 h-16 text-gray-400 mx-auto mb-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9.172 16.172a4 4 0 015.656 0M9 10h.01M15 10h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
            </svg>
            <p className="text-gray-600">
              No rooms match your filters
            </p>
          </div>
        ) : (
          <div className={
            viewMode === 'grid'
              ? 'grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4'
              : 'space-y-4'
          }>
            {filteredRooms.map(room => (
              <RoomCard
                key={room.id}
                room={room}
                isFavorite={isFavorite(room.id)}
                toggleFavorite={toggleFavorite}
                systemStatus={systemStatus}
              />
            ))}
          </div>
        )}

        {/* Footer */}
        <footer className="pt-8 pb-4 border-t text-center text-sm text-gray-600">
          <p>
            Made with ❤️ by {' '}
              <a
                href="https://gabrielongzm.com"
                target="_blank"
                rel="noopener noreferrer"
                className="text-spacemacs-light-accent hover:opacity-80">
                  Gabriel Ong
              </a>
          </p>
          <p className="mt-2 text-xs">
            Disclaimer: Sagasu 4 is not affiliated with SMU or SMU FBS
          </p>
        </footer>
      </main>
    </div>
  );
}

export default App;
