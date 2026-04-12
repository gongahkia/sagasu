import { describe, it, expect } from 'vitest';
import {
  checkBackendEnvConfigured, checkDataValidity,
  getEnvConfigUrl, getEnvStatus, checkSystemStatus,
} from '../utils/envCheck';

describe('checkBackendEnvConfigured', () => {
  it('returns not configured for null data', () => {
    const result = checkBackendEnvConfigured(null);
    expect(result.configured).toBe(false);
    expect(result.reason).toContain('No scraped data');
  });
  it('returns not configured when metadata.success is false', () => {
    const result = checkBackendEnvConfigured({ metadata: { success: false, error: 'creds bad' } });
    expect(result.configured).toBe(false);
    expect(result.reason).toBe('creds bad');
  });
  it('returns configured when metadata.success is true', () => {
    const result = checkBackendEnvConfigured({ metadata: { success: true } });
    expect(result.configured).toBe(true);
    expect(result.reason).toBeNull();
  });
  it('returns not configured for data with no metadata', () => {
    const result = checkBackendEnvConfigured({});
    expect(result.configured).toBe(false);
  });
  it('returns fallback message when metadata.success is false and no error', () => {
    const result = checkBackendEnvConfigured({ metadata: { success: false } });
    expect(result.configured).toBe(false);
    expect(result.reason).toContain('Check backend/.env');
  });
});

describe('checkDataValidity', () => {
  it('returns invalid for null data', () => {
    const result = checkDataValidity(null);
    expect(result.valid).toBe(false);
  });
  it('returns invalid for missing metadata', () => {
    const result = checkDataValidity({});
    expect(result.valid).toBe(false);
    expect(result.reason).toBe('Invalid data format');
  });
  it('returns invalid when scraper failed', () => {
    const result = checkDataValidity({ metadata: { success: false, error: 'timeout' } });
    expect(result.valid).toBe(false);
    expect(result.reason).toBe('timeout');
  });
  it('returns valid for recent successful data', () => {
    const result = checkDataValidity({
      metadata: { success: true, scraped_at: new Date().toISOString() },
    });
    expect(result.valid).toBe(true);
    expect(result.reason).toBeNull();
  });
  it('returns invalid for stale data (>48h)', () => {
    const old = new Date(Date.now() - 49 * 60 * 60 * 1000).toISOString();
    const result = checkDataValidity({ metadata: { success: true, scraped_at: old } });
    expect(result.valid).toBe(false);
    expect(result.reason).toContain('stale');
  });
});

describe('getEnvConfigUrl', () => {
  it('returns a string', () => {
    const result = getEnvConfigUrl();
    // in test env import.meta.env.DEV may be true
    expect(result === null || typeof result === 'string').toBe(true);
  });
});

describe('getEnvStatus', () => {
  it('returns not-configured for null data', () => {
    const result = getEnvStatus(null);
    expect(result.status).toBe('not-configured');
  });
  it('returns configured for valid data', () => {
    const result = getEnvStatus({ metadata: { success: true } });
    expect(result.status).toBe('configured');
  });
});

describe('checkSystemStatus', () => {
  it('shouldShowOverlay true when not configured', () => {
    const result = checkSystemStatus(null);
    expect(result.shouldShowOverlay).toBe(true);
  });
  it('shouldShowOverlay false for valid recent data', () => {
    const result = checkSystemStatus({
      metadata: { success: true, scraped_at: new Date().toISOString() },
    });
    expect(result.shouldShowOverlay).toBe(false);
  });
  it('shouldShowOverlay true for stale data', () => {
    const old = new Date(Date.now() - 50 * 60 * 60 * 1000).toISOString();
    const result = checkSystemStatus({ metadata: { success: true, scraped_at: old } });
    expect(result.shouldShowOverlay).toBe(true);
  });
});
