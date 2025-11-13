/**
 * Environment variable detection utility
 * Checks for required SMU credentials and data availability
 */

// Check if we're in development or production
export const isDevelopment = import.meta.env.DEV;
export const isProduction = import.meta.env.PROD;

// Required environment variables
const REQUIRED_ENV_VARS = {
  email: 'VITE_SMU_EMAIL',
  password: 'VITE_SMU_PASSWORD',
};

/**
 * Check if all required environment variables are configured
 * @returns {Object} { configured: boolean, missing: string[] }
 */
export const checkEnvVariables = () => {
  const missing = [];

  Object.entries(REQUIRED_ENV_VARS).forEach(([key, envVar]) => {
    const value = import.meta.env[envVar];
    if (!value || value === '' || value.startsWith('your.')) {
      missing.push(envVar);
    }
  });

  return {
    configured: missing.length === 0,
    missing,
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
 * @returns {string} URL to GitHub secrets or local env file
 */
export const getEnvConfigUrl = () => {
  if (isDevelopment) {
    return null; // Local file, no URL
  }
  return 'https://github.com/gongahkia/sagasu-4/settings/secrets/actions';
};

/**
 * Get environment status message
 * @returns {Object} { status: 'configured' | 'not-configured', message: string, url: string | null }
 */
export const getEnvStatus = () => {
  const { configured, missing } = checkEnvVariables();

  if (!configured) {
    return {
      status: 'not-configured',
      message: isDevelopment
        ? 'Environment variables not configured. Create a .env.local file with SMU_EMAIL and SMU_PASSWORD.'
        : 'Environment variables not configured in GitHub Secrets.',
      url: getEnvConfigUrl(),
      missing,
    };
  }

  return {
    status: 'configured',
    message: isDevelopment
      ? 'Environment variables configured in .env.local'
      : 'Environment variables configured in GitHub Secrets',
    url: getEnvConfigUrl(),
    missing: [],
  };
};

/**
 * Combined check for both env variables and data validity
 * @param {Object} roomData - Room data from useRoomData hook
 * @returns {Object} { shouldShowOverlay: boolean, envStatus: Object, dataStatus: Object }
 */
export const checkSystemStatus = (roomData) => {
  const envStatus = getEnvStatus();
  const dataStatus = checkDataValidity(roomData);

  // Show overlay if either env vars are missing OR data is invalid
  const shouldShowOverlay = envStatus.status === 'not-configured' || !dataStatus.valid;

  return {
    shouldShowOverlay,
    envStatus,
    dataStatus,
  };
};
