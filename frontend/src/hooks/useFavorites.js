import { useState, useEffect } from 'react';

const STORAGE_KEY = 'sagasu-favorites';

export const useFavorites = () => {
  const [favorites, setFavorites] = useState(() => {
    try {
      const stored = localStorage.getItem(STORAGE_KEY);
      return stored ? JSON.parse(stored) : [];
    } catch {
      return [];
    }
  });

  useEffect(() => {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(favorites));
  }, [favorites]);

  const addFavorite = (roomId) => {
    setFavorites(prev => {
      if (prev.includes(roomId)) return prev;
      return [...prev, roomId];
    });
  };

  const removeFavorite = (roomId) => {
    setFavorites(prev => prev.filter(id => id !== roomId));
  };

  const toggleFavorite = (roomId) => {
    if (favorites.includes(roomId)) {
      removeFavorite(roomId);
    } else {
      addFavorite(roomId);
    }
  };

  const isFavorite = (roomId) => favorites.includes(roomId);

  return {
    favorites,
    addFavorite,
    removeFavorite,
    toggleFavorite,
    isFavorite,
  };
};
