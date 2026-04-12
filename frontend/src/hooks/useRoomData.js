import { useState, useEffect } from 'react';
import {
  ROOM_DATA_URL as GITHUB_RAW_URL,
  BOOKINGS_URL,
  TASKS_URL,
  SCRAPER_CONSOLE_URL,
} from '../config';
import { RoomDataSchema, BookingsDataSchema, TasksDataSchema } from '../schemas';
import { logger } from '../logger';

const STALE_THRESHOLD_MS = 24 * 60 * 60 * 1000; // 24 hours

function computeStaleness(jsonData) { // returns { isStale, hoursAgo }
  if (!jsonData?.metadata?.scraped_at) return { isStale: false, hoursAgo: 0 };
  const scrapedAt = new Date(jsonData.metadata.scraped_at);
  const diff = Date.now() - scrapedAt.getTime();
  return { isStale: diff > STALE_THRESHOLD_MS, hoursAgo: Math.floor(diff / (1000 * 60 * 60)) };
}

export const useRoomData = (autoRefresh = false, intervalMs = 30000) => {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [isStale, setIsStale] = useState(false);
  const [hoursAgo, setHoursAgo] = useState(0);

  const fetchData = async () => {
    try {
      setLoading(true);
      setError(null);

      const response = await fetch(GITHUB_RAW_URL + '?t=' + Date.now());

      if (!response.ok) {
        throw new Error(`Failed to fetch data: ${response.statusText}`);
      }

      const jsonData = await response.json();
      const parsed = RoomDataSchema.safeParse(jsonData);
      if (!parsed.success) {
        logger.error('Room data schema validation failed', { issues: parsed.error.issues });
        setError('Data format unavailable');
        return;
      }
      setData(parsed.data);
      const staleness = computeStaleness(parsed.data);
      setIsStale(staleness.isStale);
      setHoursAgo(staleness.hoursAgo);
    } catch (err) {
      setError(err.message);
      logger.error('Error fetching room data', { error: err.message });
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

  return { data, loading, error, isStale, hoursAgo, refetch: fetchData };
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
      const parsed = BookingsDataSchema.safeParse(jsonData);
      if (!parsed.success) {
        logger.error('Bookings data schema validation failed', { issues: parsed.error.issues });
        setError('Data format unavailable');
        return;
      }
      setData(parsed.data);
    } catch (err) {
      setError(err.message);
      logger.error('Error fetching bookings data', { error: err.message });
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
      const parsed = TasksDataSchema.safeParse(jsonData);
      if (!parsed.success) {
        logger.error('Tasks data schema validation failed', { issues: parsed.error.issues });
        setError('Data format unavailable');
        return;
      }
      setData(parsed.data);
    } catch (err) {
      setError(err.message);
      logger.error('Error fetching tasks data', { error: err.message });
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

export const useScraperConsoleLog = (autoRefresh = false, intervalMs = 30000) => {
  const [text, setText] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const fetchLog = async () => {
    try {
      setLoading(true);
      setError(null);

      const response = await fetch(SCRAPER_CONSOLE_URL + '?t=' + Date.now());
      if (!response.ok) {
        if (response.status === 404) {
          setText('');
          return;
        }
        throw new Error(`Failed to fetch log: ${response.statusText}`);
      }

      const logText = await response.text();
      setText(logText);
    } catch (err) {
      setError(err.message);
      logger.error('Error fetching scraper log', { error: err.message });
      setText('');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    fetchLog();

    if (autoRefresh) {
      const interval = setInterval(fetchLog, intervalMs);
      return () => clearInterval(interval);
    }
  }, [autoRefresh, intervalMs]);

  return { text, loading, error, refetch: fetchLog };
};
