import { useState, useEffect } from 'react';

const GITHUB_RAW_URL = 'https://raw.githubusercontent.com/gongahkia/sagasu-4/main/backend/log/scraped_log.json';
const BOOKINGS_URL = 'https://raw.githubusercontent.com/gongahkia/sagasu-4/main/backend/log/bookings_log.json';

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

export const useBookingData = () => {
  const [bookings, setBookings] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    const fetchBookings = async () => {
      try {
        setLoading(true);
        const response = await fetch(BOOKINGS_URL + '?t=' + Date.now());

        if (!response.ok) {
          // Bookings file might not exist yet
          setBookings({ metadata: { total_bookings: 0 }, bookings: [] });
          return;
        }

        const data = await response.json();
        setBookings(data);
      } catch (err) {
        // File doesn't exist yet, use empty data
        setBookings({ metadata: { total_bookings: 0 }, bookings: [] });
      } finally {
        setLoading(false);
      }
    };

    fetchBookings();
  }, []);

  return { bookings, loading, error };
};
