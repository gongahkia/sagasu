import React, { useState, useMemo } from 'react';

const MyTasks = ({ tasksData }) => {
  const [sortBy, setSortBy] = useState('date'); // 'date', 'status', 'type'
  const [filterStatus, setFilterStatus] = useState('all'); // 'all', 'pending', 'approved', 'rejected'

  if (!tasksData?.tasks) {
    return null;
  }

  const { statistics, tasks } = tasksData;

  // Filter and sort tasks
  const filteredAndSortedTasks = useMemo(() => {
    let filtered = [...tasks];

    // Filter by status
    if (filterStatus === 'pending') {
      filtered = filtered.filter(t => t.status === 'Pending Confirmation');
    } else if (filterStatus === 'approved') {
      filtered = filtered.filter(t => t.status === 'Approved');
    } else if (filterStatus === 'rejected') {
      filtered = filtered.filter(t => t.status === 'Rejected');
    }

    // Sort tasks
    filtered.sort((a, b) => {
      if (sortBy === 'date') {
        // Parse dates and compare
        const dateA = new Date(a.date.split('-').reverse().join('-'));
        const dateB = new Date(b.date.split('-').reverse().join('-'));
        return dateA - dateB;
      } else if (sortBy === 'status') {
        return a.status.localeCompare(b.status);
      } else if (sortBy === 'type') {
        return a.task_type.localeCompare(b.task_type);
      }
      return 0;
    });

    return filtered;
  }, [tasks, sortBy, filterStatus]);

  const getStatusBadgeClass = (status) => {
    if (status === 'Approved') {
      return 'badge-success';
    } else if (status === 'Pending Confirmation') {
      return 'badge-warning';
    } else if (status === 'Rejected') {
      return 'badge-error';
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

  if (tasks.length === 0) {
    return (
      <div className="card text-center py-8">
        <div className="mb-4">
          <svg className="w-16 h-16 mx-auto text-gray-300" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-6 9l2 2 4-4" />
          </svg>
        </div>
        <h3 className="text-lg font-semibold text-gray-700 mb-2">No Tasks Found</h3>
        <p className="text-sm text-gray-500">You don't have any pending tasks.</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {/* Statistics Cards */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <div className="card">
          <div className="text-sm text-gray-600 mb-1">
            Total Tasks
          </div>
          <div className="text-3xl font-bold text-spacemacs-light-accent">
            {statistics.total_tasks}
          </div>
        </div>

        <div className="card">
          <div className="text-sm text-gray-600 mb-1">
            Pending
          </div>
          <div className="text-3xl font-bold text-spacemacs-light-yellow">
            {statistics.pending_tasks}
          </div>
        </div>

        <div className="card">
          <div className="text-sm text-gray-600 mb-1">
            Approved
          </div>
          <div className="text-3xl font-bold text-spacemacs-light-green">
            {statistics.approved_tasks}
          </div>
        </div>

        <div className="card">
          <div className="text-sm text-gray-600 mb-1">
            Rejected
          </div>
          <div className="text-3xl font-bold text-spacemacs-light-red">
            {statistics.rejected_tasks}
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
              All ({tasks.length})
            </button>
            <button
              onClick={() => setFilterStatus('pending')}
              className={`px-3 py-1.5 rounded-md text-sm font-medium transition-colors ${
                filterStatus === 'pending'
                  ? 'bg-spacemacs-light-yellow text-white'
                  : 'bg-gray-100 text-gray-700 hover:bg-gray-200'
              }`}
            >
              Pending ({statistics.pending_tasks})
            </button>
            <button
              onClick={() => setFilterStatus('approved')}
              className={`px-3 py-1.5 rounded-md text-sm font-medium transition-colors ${
                filterStatus === 'approved'
                  ? 'bg-spacemacs-light-green text-white'
                  : 'bg-gray-100 text-gray-700 hover:bg-gray-200'
              }`}
            >
              Approved ({statistics.approved_tasks})
            </button>
            <button
              onClick={() => setFilterStatus('rejected')}
              className={`px-3 py-1.5 rounded-md text-sm font-medium transition-colors ${
                filterStatus === 'rejected'
                  ? 'bg-spacemacs-light-red text-white'
                  : 'bg-gray-100 text-gray-700 hover:bg-gray-200'
              }`}
            >
              Rejected ({statistics.rejected_tasks})
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
              <option value="type">Task Type</option>
            </select>
          </div>
        </div>
      </div>

      {/* Tasks List */}
      <div className="space-y-3">
        {filteredAndSortedTasks.length === 0 ? (
          <div className="card text-center py-8">
            <p className="text-gray-500">No tasks match the selected filter.</p>
          </div>
        ) : (
          filteredAndSortedTasks.map((task, index) => (
            <TaskCard key={task.reference_number || index} task={task} formatDate={formatDate} getStatusBadgeClass={getStatusBadgeClass} />
          ))
        )}
      </div>
    </div>
  );
};

const TaskCard = ({ task, formatDate, getStatusBadgeClass }) => {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="card hover:shadow-lg transition-shadow">
      <div className="flex items-start justify-between mb-3">
        <div className="flex-1">
          <div className="flex items-center gap-2 mb-2">
            <h3 className="font-bold text-lg text-spacemacs-light-accent">
              {task.room_name}
            </h3>
            <span className={getStatusBadgeClass(task.status)}>
              {task.status}
            </span>
          </div>
          <div className="text-sm text-gray-600 space-y-1">
            <div className="flex items-center gap-2">
              <svg className="w-4 h-4 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M8 7V3m8 4V3m-9 8h10M5 21h14a2 2 0 002-2V7a2 2 0 00-2-2H5a2 2 0 00-2 2v12a2 2 0 002 2z" />
              </svg>
              <span>
                {formatDate(task.date)} • {task.start_time} - {task.end_time}
              </span>
            </div>
            <div className="flex items-center gap-2">
              <svg className="w-4 h-4 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 21V5a2 2 0 00-2-2H7a2 2 0 00-2 2v16m14 0h2m-2 0h-5m-9 0H3m2 0h5M9 7h1m-1 4h1m4-4h1m-1 4h1m-5 10v-5a1 1 0 011-1h2a1 1 0 011 1v5m-4 0h4" />
              </svg>
              <span>{task.building}</span>
            </div>
          </div>
        </div>

        <div className="text-right">
          <div className="px-3 py-1 bg-spacemacs-light-blue/10 rounded-md">
            <div className="text-sm font-semibold text-spacemacs-light-blue">
              {task.task_type}
            </div>
          </div>
          <div className="text-xs text-gray-500 mt-1">
            {task.duration_hours}hrs
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
            <span className="font-mono font-medium">{task.reference_number}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-gray-600">Requested By:</span>
            <span className="font-medium">{task.requested_by}</span>
          </div>
        </div>
      )}
    </div>
  );
};

export default MyTasks;
