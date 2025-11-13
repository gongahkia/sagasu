/**
 * Environment variable detection utility
 * Checks if backend has successfully scraped data (indicating backend .env is configured)
 */

// Check if we're in development or production
export const isDevelopment = import.meta.env.DEV;
export const isProduction = import.meta.env.PROD;

/**
 * Check if backend environment is configured by checking data validity
 * Note: Frontend doesn't need SMU credentials - only backend does
 * @param {Object} roomData - Room data from useRoomData hook
 * @returns {Object} { configured: boolean, reason: string }
 */
export const checkBackendEnvConfigured = (roomData) => {
  // If no data, backend env might not be configured
  if (!roomData) {
    return {
      configured: false,
      reason: 'No scraped data available. Backend .env might not be configured.'
    };
  }

  // If data exists but scrape failed, backend env is likely misconfigured
  if (roomData.metadata && !roomData.metadata.success) {
    return {
      configured: false,
      reason: roomData.metadata.error || 'Backend scraper failed. Check backend/.env credentials.'
    };
  }

  // If data exists and is successful, backend is configured
  if (roomData.metadata && roomData.metadata.success) {
    return {
      configured: true,
      reason: null
    };
  }

  // Unknown state
  return {
    configured: false,
    reason: 'Unable to determine backend configuration status'
  };
};

/**
 * Check if scraped data is valid and recent
 * @param {Object} roomData - Room data from useRoomData hook
 * @returns {Object} { valid: boolean, reason: string }
 */
export const checkDataValidity = (roomData) => {
  if (!roomData) {
    return { valid: false, reason: 'No data available' };
  }

  if (!roomData.metadata) {
    return { valid: false, reason: 'Invalid data format' };
  }

  if (!roomData.metadata.success) {
    return {
      valid: false,
      reason: roomData.metadata.error || 'Scraper failed'
    };
  }

  // Check if data is recent (within last 48 hours)
  const scrapedAt = new Date(roomData.metadata.scraped_at);
  const now = new Date();
  const hoursSinceUpdate = (now - scrapedAt) / (1000 * 60 * 60);

  if (hoursSinceUpdate > 48) {
    return {
      valid: false,
      reason: `Data is stale (${Math.floor(hoursSinceUpdate)} hours old)`
    };
  }

  return { valid: true, reason: null };
};

/**
 * Get the appropriate URL for configuring environment variables
 * @returns {string} URL to GitHub secrets or local backend .env
 */
export const getEnvConfigUrl = () => {
  if (isDevelopment) {
    return null; // Local backend/.env file, no URL
  }
  return 'https://github.com/gongahkia/sagasu-4/settings/secrets/actions';
};

/**
 * Get environment status message based on backend data
 * @param {Object} roomData - Room data from useRoomData hook
 * @returns {Object} { status: 'configured' | 'not-configured', message: string, url: string | null }
 */
export const getEnvStatus = (roomData) => {
  const backendEnv = checkBackendEnvConfigured(roomData);

  if (!backendEnv.configured) {
    return {
      status: 'not-configured',
      message: isDevelopment
        ? 'Backend environment not configured. Check backend/.env file with SMU_EMAIL and SMU_PASSWORD.'
        : 'Backend environment not configured in GitHub Secrets.',
      url: getEnvConfigUrl(),
      reason: backendEnv.reason,
    };
  }

  return {
    status: 'configured',
    message: isDevelopment
      ? 'Backend configured via backend/.env'
      : 'Backend configured via GitHub Secrets',
    url: getEnvConfigUrl(),
    reason: null,
  };
};

/**
 * Combined check for backend configuration and data validity
 * @param {Object} roomData - Room data from useRoomData hook
 * @returns {Object} { shouldShowOverlay: boolean, envStatus: Object, dataStatus: Object }
 */
export const checkSystemStatus = (roomData) => {
  const envStatus = getEnvStatus(roomData);
  const dataStatus = checkDataValidity(roomData);

  // Show overlay if either backend is not configured OR data is invalid
  const shouldShowOverlay = envStatus.status === 'not-configured' || !dataStatus.valid;

  return {
    shouldShowOverlay,
    envStatus,
    dataStatus,
  };
};
