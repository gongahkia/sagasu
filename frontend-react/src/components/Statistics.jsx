import React from 'react';

const Statistics = ({ statistics, config }) => {
  if (!statistics) return null;

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
      <div className="card">
        <div className="text-sm text-gray-600 dark:text-gray-400 mb-1">
          Available Now
        </div>
        <div className="text-3xl font-bold text-spacemacs-light-green dark:text-spacemacs-dark-green">
          {statistics.available_rooms}
        </div>
      </div>

      <div className="card">
        <div className="text-sm text-gray-600 dark:text-gray-400 mb-1">
          Total Rooms
        </div>
        <div className="text-3xl font-bold text-spacemacs-light-accent dark:text-spacemacs-dark-accent">
          {statistics.total_rooms}
        </div>
      </div>

      <div className="card">
        <div className="text-sm text-gray-600 dark:text-gray-400 mb-1">
          Fully Booked
        </div>
        <div className="text-3xl font-bold text-spacemacs-light-red dark:text-spacemacs-dark-red">
          {statistics.booked_rooms}
        </div>
      </div>

      <div className="card">
        <div className="text-sm text-gray-600 dark:text-gray-400 mb-1">
          Partially Available
        </div>
        <div className="text-3xl font-bold text-spacemacs-light-yellow dark:text-spacemacs-dark-yellow">
          {statistics.partially_available_rooms}
        </div>
      </div>

      {config && (
        <div className="card col-span-1 sm:col-span-2 lg:col-span-4">
          <div className="text-sm text-gray-600 dark:text-gray-400 mb-2">
            Scrape Configuration
          </div>
          <div className="flex flex-wrap gap-2 text-xs">
            <span className="badge-info">
              {config.date}
            </span>
            <span className="badge-info">
              {config.start_time} - {config.end_time}
            </span>
            {config.filters.buildings.length > 0 && (
              <span className="badge-info">
                {config.filters.buildings.length} building(s)
              </span>
            )}
            {config.filters.floors.length > 0 && (
              <span className="badge-info">
                {config.filters.floors.length} floor(s)
              </span>
            )}
            {config.filters.facility_types.length > 0 && (
              <span className="badge-info">
                {config.filters.facility_types.join(', ')}
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  );
};

export default Statistics;
