export const formatTime = (timeStr) => {
  return timeStr;
};

export const formatDuration = (minutes) => {
  const hours = Math.floor(minutes / 60);
  const mins = minutes % 60;

  if (hours === 0) return `${mins}m`;
  if (mins === 0) return `${hours}h`;
  return `${hours}h ${mins}m`;
};

export const formatDateTime = (isoString) => {
  const date = new Date(isoString);
  return date.toLocaleString('en-SG', {
    dateStyle: 'medium',
    timeStyle: 'short',
  });
};

export const formatDateShort = (dateStr) => {
  return dateStr;
};

export const getStatusColor = (status) => {
  switch (status) {
    case 'free':
      return 'text-spacemacs-light-green';
    case 'booked':
      return 'text-spacemacs-light-red';
    case 'unavailable':
      return 'text-gray-400';
    default:
      return 'text-gray-600';
  }
};

export const getStatusBadgeClass = (status) => {
  switch (status) {
    case 'free':
      return 'badge-success';
    case 'booked':
      return 'badge-danger';
    case 'unavailable':
      return 'badge bg-gray-100 text-gray-600';
    default:
      return 'badge-info';
  }
};

export const getBookingStatusColor = (status) => {
  switch (status) {
    case 'confirmed':
      return 'badge-success';
    case 'pending':
      return 'badge-warning';
    case 'failed':
      return 'badge-danger';
    case 'cancelled':
      return 'badge bg-gray-100 text-gray-600';
    default:
      return 'badge-info';
  }
};
