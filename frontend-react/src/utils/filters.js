export const filterRooms = (rooms, filters) => {
  if (!rooms) return [];

  return rooms.filter(room => {
    // Search filter
    if (filters.search) {
      const searchLower = filters.search.toLowerCase();
      const matchesName = room.name.toLowerCase().includes(searchLower);
      const matchesBuilding = room.building.toLowerCase().includes(searchLower);
      if (!matchesName && !matchesBuilding) return false;
    }

    // Availability filter
    if (filters.availability === 'available') {
      if (!room.availability_summary.is_available_now) return false;
    } else if (filters.availability === 'booked') {
      if (room.availability_summary.free_slots_count > 0) return false;
    }

    // Building filter
    if (filters.buildings.length > 0) {
      if (!filters.buildings.includes(room.building)) return false;
    }

    // Floor filter
    if (filters.floors.length > 0) {
      if (!filters.floors.includes(room.floor)) return false;
    }

    // Facility type filter
    if (filters.facilityTypes.length > 0) {
      if (!filters.facilityTypes.includes(room.facility_type)) return false;
    }

    // Favorites filter
    if (filters.showFavoritesOnly) {
      if (!filters.favoriteIds.includes(room.id)) return false;
    }

    return true;
  });
};

export const getUniqueValues = (rooms, key) => {
  if (!rooms) return [];
  const values = rooms.map(room => room[key]);
  return [...new Set(values)].sort();
};

export const sortRooms = (rooms, sortBy) => {
  if (!rooms) return [];

  const sorted = [...rooms];

  switch (sortBy) {
    case 'name':
      return sorted.sort((a, b) => a.name.localeCompare(b.name));
    case 'building':
      return sorted.sort((a, b) => a.building.localeCompare(b.building));
    case 'availability':
      return sorted.sort((a, b) => {
        if (a.availability_summary.is_available_now === b.availability_summary.is_available_now) {
          return b.availability_summary.free_duration_minutes - a.availability_summary.free_duration_minutes;
        }
        return a.availability_summary.is_available_now ? -1 : 1;
      });
    default:
      return sorted;
  }
};
