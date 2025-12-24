import React from 'react';

const ScraperLogs = ({ text, loading, error, onRefresh }) => {
  if (loading) {
    return (
      <div className="card text-center py-12">
        <div className="animate-spin rounded-full h-12 w-12 border-b-2 border-spacemacs-light-accent mx-auto mb-4"></div>
        <p className="text-gray-600">Loading logs...</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="card text-center py-12">
        <h3 className="text-lg font-semibold text-gray-700 mb-2">Failed to Load Logs</h3>
        <p className="text-sm text-gray-500 mb-4">{error}</p>
        <button onClick={onRefresh} className="btn-primary">
          Retry
        </button>
      </div>
    );
  }

  if (!text || text.trim().length === 0) {
    return (
      <div className="card text-center py-12">
        <h3 className="text-lg font-semibold text-gray-700 mb-2">No Logs Yet</h3>
        <p className="text-sm text-gray-500 mb-4">
          The console log file hasn’t been generated yet.
        </p>
        <button onClick={onRefresh} className="btn-secondary">
          Refresh
        </button>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-lg font-bold text-spacemacs-light-accent">Scraper Logs</h2>
          <p className="text-sm text-gray-600">Latest console output from the scraper workflow.</p>
        </div>
        <button onClick={onRefresh} className="btn-secondary">
          Refresh
        </button>
      </div>

      <div className="card p-0 overflow-hidden">
        <pre className="text-sm leading-5 whitespace-pre-wrap break-words p-4 max-h-[70vh] overflow-auto bg-gray-50 text-gray-800">
{text}
        </pre>
      </div>
    </div>
  );
};

export default ScraperLogs;
