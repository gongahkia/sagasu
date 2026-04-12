const { logger } = require('./logger');
async function withRetry(fn, { maxAttempts = 3, baseDelayMs = 2000 } = {}) { // exponential backoff retry wrapper
  let lastError;
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      return await fn();
    } catch (err) {
      lastError = err;
      if (attempt < maxAttempts) {
        const delay = baseDelayMs * Math.pow(2, attempt - 1);
        logger.warn({ attempt, maxAttempts, delay, error: err.message }, 'retry attempt failed, retrying');
        await new Promise(r => setTimeout(r, delay));
      }
    }
  }
  throw lastError;
}
module.exports = { withRetry };
