import React from 'react';

const Statistics = ({ statistics }) => {
  if (!statistics) return null;

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
      <div className="card">
        <div className="text-sm text-gray-600 mb-1">
          Available Now
        </div>
        <div className="text-3xl font-bold text-spacemacs-light-green">
          {statistics.available_rooms}
        </div>
      </div>

      <div className="card">
        <div className="text-sm text-gray-600 mb-1">
          Total Rooms
        </div>
        <div className="text-3xl font-bold text-spacemacs-light-accent">
          {statistics.total_rooms}
        </div>
      </div>

      <div className="card">
        <div className="text-sm text-gray-600 mb-1">
          Fully Booked
        </div>
        <div className="text-3xl font-bold text-spacemacs-light-red">
          {statistics.booked_rooms}
        </div>
      </div>

      <div className="card">
        <div className="text-sm text-gray-600 mb-1">
          Partially Available
        </div>
        <div className="text-3xl font-bold text-spacemacs-light-yellow">
          {statistics.partially_available_rooms}
        </div>
      </div>
    </div>
  );
};

export const ScrapeConfig = ({ config }) => {
  if (!config) return null;

  return (
    <div className="card">
      <div className="flex items-center gap-3 mb-3">
        <div className="p-2 bg-spacemacs-light-blue/10 rounded-lg">
          <svg className="w-6 h-6 text-spacemacs-light-blue" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.065 2.572c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.572 1.065c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.065-2.572c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" />
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
          </svg>
        </div>
        <div>
          <h3 className="font-bold text-lg">Scrape Configuration</h3>
          <p className="text-sm text-gray-600">
            Current scraper settings
          </p>
        </div>
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
  );
};

export default Statistics;
