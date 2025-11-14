import { useState, useEffect } from 'react';

// Determine if we're in development mode
const isDev = import.meta.env.DEV;

// Use local files in development, GitHub raw URLs in production
const GITHUB_RAW_URL = isDev
  ? '/data/scraped_log.json'
  : 'https://raw.githubusercontent.com/gongahkia/sagasu-4/main/backend/log/scraped_log.json';

const BOOKINGS_URL = isDev
  ? '/data/scraped_bookings.json'
  : 'https://raw.githubusercontent.com/gongahkia/sagasu-4/main/backend/log/scraped_bookings.json';

const TASKS_URL = isDev
  ? '/data/scraped_tasks.json'
  : 'https://raw.githubusercontent.com/gongahkia/sagasu-4/main/backend/log/scraped_tasks.json';

export const useRoomData = (autoRefresh = false, intervalMs = 30000) => {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const fetchData = async () => {
    try {
      setLoading(true);
      setError(null);

      const response = await fetch(GITHUB_RAW_URL + '?t=' + Date.now());

      if (!response.ok) {
        throw new Error(`Failed to fetch data: ${response.statusText}`);
      }

      const jsonData = await response.json();
      setData(jsonData);
    } catch (err) {
      setError(err.message);
      console.error('Error fetching room data:', err);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchData();

    if (autoRefresh) {
      const interval = setInterval(fetchData, intervalMs);
      return () => clearInterval(interval);
    }
  }, [autoRefresh, intervalMs]);

  return { data, loading, error, refetch: fetchData };
};

export const useBookingData = (autoRefresh = false, intervalMs = 30000) => {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const fetchBookings = async () => {
    try {
      setLoading(true);
      setError(null);
      const response = await fetch(BOOKINGS_URL + '?t=' + Date.now());

      if (!response.ok) {
        // Bookings file might not exist yet
        setData({
          metadata: { success: false },
          statistics: { total_bookings: 0, confirmed_bookings: 0, pending_bookings: 0, total_price: 0 },
          bookings: []
        });
        return;
      }

      const jsonData = await response.json();
      setData(jsonData);
    } catch (err) {
      setError(err.message);
      console.error('Error fetching bookings data:', err);
      // File doesn't exist yet, use empty data
      setData({
        metadata: { success: false },
        statistics: { total_bookings: 0, confirmed_bookings: 0, pending_bookings: 0, total_price: 0 },
        bookings: []
      });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchBookings();

    if (autoRefresh) {
      const interval = setInterval(fetchBookings, intervalMs);
      return () => clearInterval(interval);
    }
  }, [autoRefresh, intervalMs]);

  return { data, loading, error, refetch: fetchBookings };
};

export const useTaskData = (autoRefresh = false, intervalMs = 30000) => {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const fetchTasks = async () => {
    try {
      setLoading(true);
      setError(null);
      const response = await fetch(TASKS_URL + '?t=' + Date.now());

      if (!response.ok) {
        // Tasks file might not exist yet
        setData({
          metadata: { success: false },
          statistics: { total_tasks: 0, pending_tasks: 0, approved_tasks: 0, rejected_tasks: 0 },
          tasks: []
        });
        return;
      }

      const jsonData = await response.json();
      setData(jsonData);
    } catch (err) {
      setError(err.message);
      console.error('Error fetching tasks data:', err);
      // File doesn't exist yet, use empty data
      setData({
        metadata: { success: false },
        statistics: { total_tasks: 0, pending_tasks: 0, approved_tasks: 0, rejected_tasks: 0 },
        tasks: []
      });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchTasks();

    if (autoRefresh) {
      const interval = setInterval(fetchTasks, intervalMs);
      return () => clearInterval(interval);
    }
  }, [autoRefresh, intervalMs]);

  return { data, loading, error, refetch: fetchTasks };
};
