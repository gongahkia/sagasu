import React, { useState } from 'react';

const Filters = ({ filters, setFilters, buildings, floors, facilityTypes, showFavorites, toggleFavorites }) => {
  const [expanded, setExpanded] = useState(false);

  const handleSearchChange = (e) => {
    setFilters(prev => ({ ...prev, search: e.target.value }));
  };

  const handleAvailabilityChange = (value) => {
    setFilters(prev => ({ ...prev, availability: value }));
  };

  const handleBuildingToggle = (building) => {
    setFilters(prev => ({
      ...prev,
      buildings: prev.buildings.includes(building)
        ? prev.buildings.filter(b => b !== building)
        : [...prev.buildings, building]
    }));
  };

  const handleFloorToggle = (floor) => {
    setFilters(prev => ({
      ...prev,
      floors: prev.floors.includes(floor)
        ? prev.floors.filter(f => f !== floor)
        : [...prev.floors, floor]
    }));
  };

  const handleFacilityTypeToggle = (type) => {
    setFilters(prev => ({
      ...prev,
      facilityTypes: prev.facilityTypes.includes(type)
        ? prev.facilityTypes.filter(t => t !== type)
        : [...prev.facilityTypes, type]
    }));
  };

  const clearFilters = () => {
    setFilters({
      search: '',
      availability: 'all',
      buildings: [],
      floors: [],
      facilityTypes: [],
      showFavoritesOnly: false,
      favoriteIds: filters.favoriteIds,
    });
  };

  const hasActiveFilters = filters.search || filters.availability !== 'all' ||
    filters.buildings.length > 0 || filters.floors.length > 0 ||
    filters.facilityTypes.length > 0 || filters.showFavoritesOnly;

  return (
    <div className="card">
      <div className="flex flex-col sm:flex-row gap-4 mb-4">
        <div className="flex-1">
          <input
            type="text"
            placeholder="Search rooms or buildings..."
            value={filters.search}
            onChange={handleSearchChange}
            className="input"
          />
        </div>

        <div className="flex gap-2">
          <button
            onClick={() => handleAvailabilityChange('all')}
            className={`px-4 py-2 rounded-md font-medium transition-colors ${
              filters.availability === 'all'
                ? 'bg-spacemacs-light-accent text-white dark:bg-spacemacs-dark-accent'
                : 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300'
            }`}
          >
            All
          </button>
          <button
            onClick={() => handleAvailabilityChange('available')}
            className={`px-4 py-2 rounded-md font-medium transition-colors ${
              filters.availability === 'available'
                ? 'bg-spacemacs-light-green text-white dark:bg-spacemacs-dark-green'
                : 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300'
            }`}
          >
            Available
          </button>
          <button
            onClick={() => handleAvailabilityChange('booked')}
            className={`px-4 py-2 rounded-md font-medium transition-colors ${
              filters.availability === 'booked'
                ? 'bg-spacemacs-light-red text-white dark:bg-spacemacs-dark-red'
                : 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300'
            }`}
          >
            Booked
          </button>
        </div>

        <button
          onClick={toggleFavorites}
          className={`px-4 py-2 rounded-md font-medium transition-colors flex items-center gap-2 ${
            showFavorites
              ? 'bg-spacemacs-light-yellow text-white dark:bg-spacemacs-dark-yellow'
              : 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300'
          }`}
        >
          <svg className="w-5 h-5" fill={showFavorites ? 'currentColor' : 'none'} stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M11.049 2.927c.3-.921 1.603-.921 1.902 0l1.519 4.674a1 1 0 00.95.69h4.915c.969 0 1.371 1.24.588 1.81l-3.976 2.888a1 1 0 00-.363 1.118l1.518 4.674c.3.922-.755 1.688-1.538 1.118l-3.976-2.888a1 1 0 00-1.176 0l-3.976 2.888c-.783.57-1.838-.197-1.538-1.118l1.518-4.674a1 1 0 00-.363-1.118l-3.976-2.888c-.784-.57-.38-1.81.588-1.81h4.914a1 1 0 00.951-.69l1.519-4.674z" />
          </svg>
          Favorites
        </button>
      </div>

      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full flex items-center justify-between text-sm text-gray-600 dark:text-gray-400 hover:text-gray-900 dark:hover:text-gray-200 transition-colors"
      >
        <span>Advanced Filters</span>
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
        <div className="mt-4 pt-4 border-t space-y-4">
          {buildings.length > 0 && (
            <div>
              <div className="text-sm font-medium mb-2">Buildings</div>
              <div className="flex flex-wrap gap-2">
                {buildings.map(building => (
                  <button
                    key={building}
                    onClick={() => handleBuildingToggle(building)}
                    className={`px-3 py-1 rounded-full text-xs font-medium transition-colors ${
                      filters.buildings.includes(building)
                        ? 'bg-spacemacs-light-accent text-white dark:bg-spacemacs-dark-accent'
                        : 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300'
                    }`}
                  >
                    {building}
                  </button>
                ))}
              </div>
            </div>
          )}

          {floors.length > 0 && (
            <div>
              <div className="text-sm font-medium mb-2">Floors</div>
              <div className="flex flex-wrap gap-2">
                {floors.map(floor => (
                  <button
                    key={floor}
                    onClick={() => handleFloorToggle(floor)}
                    className={`px-3 py-1 rounded-full text-xs font-medium transition-colors ${
                      filters.floors.includes(floor)
                        ? 'bg-spacemacs-light-accent text-white dark:bg-spacemacs-dark-accent'
                        : 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300'
                    }`}
                  >
                    {floor}
                  </button>
                ))}
              </div>
            </div>
          )}

          {facilityTypes.length > 0 && (
            <div>
              <div className="text-sm font-medium mb-2">Facility Types</div>
              <div className="flex flex-wrap gap-2">
                {facilityTypes.map(type => (
                  <button
                    key={type}
                    onClick={() => handleFacilityTypeToggle(type)}
                    className={`px-3 py-1 rounded-full text-xs font-medium transition-colors ${
                      filters.facilityTypes.includes(type)
                        ? 'bg-spacemacs-light-accent text-white dark:bg-spacemacs-dark-accent'
                        : 'bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300'
                    }`}
                  >
                    {type}
                  </button>
                ))}
              </div>
            </div>
          )}

          {hasActiveFilters && (
            <button
              onClick={clearFilters}
              className="w-full sm:w-auto px-4 py-2 text-sm bg-gray-100 text-gray-700 dark:bg-gray-800 dark:text-gray-300 rounded-md hover:bg-gray-200 dark:hover:bg-gray-700 transition-colors"
            >
              Clear All Filters
            </button>
          )}
        </div>
      )}
    </div>
  );
};

export default Filters;
