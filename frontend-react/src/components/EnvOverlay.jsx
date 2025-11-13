import React from 'react';
import { isDevelopment } from '../utils/envCheck';

/**
 * EnvOverlay Component
 * Displays a translucent overlay when environment variables are not configured
 * or when data is invalid/stale
 */
const EnvOverlay = ({ envStatus, dataStatus }) => {
  const showEnvWarning = envStatus.status === 'not-configured';
  const showDataWarning = !dataStatus.valid;

  // Don't render if everything is ok
  if (!showEnvWarning && !showDataWarning) {
    return null;
  }

  return (
    <div className="absolute inset-0 z-10 flex items-center justify-center bg-spacemacs-light-bg/80 dark:bg-spacemacs-dark-bg/80 backdrop-blur-sm rounded-lg">
      <div className="text-center p-4 max-w-sm">
        {showEnvWarning && (
          <div className="mb-4">
            <svg
              className="w-12 h-12 mx-auto mb-3 text-spacemacs-light-red dark:text-spacemacs-dark-red"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"
              />
            </svg>
            <h3 className="font-bold text-lg mb-2 text-spacemacs-light-fg dark:text-spacemacs-dark-fg">
              Environment Variables Not Configured
            </h3>
            <p className="text-sm text-spacemacs-light-fg/80 dark:text-spacemacs-dark-fg/80 mb-3">
              {envStatus.message}
            </p>
            {isDevelopment ? (
              <div className="text-xs bg-spacemacs-light-bg-hl dark:bg-spacemacs-dark-bg-hl p-3 rounded-md text-left">
                <p className="font-mono mb-2">Configure <span className="font-bold">backend/.env</span> with:</p>
                <code className="block font-mono text-spacemacs-light-green dark:text-spacemacs-dark-green">
                  SMU_EMAIL=your.email@smu.edu.sg<br />
                  SMU_PASSWORD=your_password<br />
                  <span className="text-spacemacs-light-fg/60 dark:text-spacemacs-dark-fg/60"># + other scrape config...</span>
                </code>
                <p className="mt-2 text-spacemacs-light-fg/70 dark:text-spacemacs-dark-fg/70">
                  Then run: <span className="font-bold">npm run scrape:prod</span>
                </p>
              </div>
            ) : (
              envStatus.url && (
                <a
                  href={envStatus.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-2 px-4 py-2 bg-spacemacs-light-accent dark:bg-spacemacs-dark-accent text-white rounded-md hover:opacity-90 transition-opacity text-sm"
                >
                  Configure in GitHub Secrets
                  <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                    <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M10 6H6a2 2 0 00-2 2v10a2 2 0 002 2h10a2 2 0 002-2v-4M14 4h6m0 0v6m0-6L10 14" />
                  </svg>
                </a>
              )
            )}
          </div>
        )}

        {showDataWarning && !showEnvWarning && (
          <div>
            <svg
              className="w-12 h-12 mx-auto mb-3 text-spacemacs-light-orange dark:text-spacemacs-dark-orange"
              fill="none"
              stroke="currentColor"
              viewBox="0 0 24 24"
            >
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M12 8v4m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"
              />
            </svg>
            <h3 className="font-bold text-lg mb-2 text-spacemacs-light-fg dark:text-spacemacs-dark-fg">
              Data Unavailable
            </h3>
            <p className="text-sm text-spacemacs-light-fg/80 dark:text-spacemacs-dark-fg/80">
              {dataStatus.reason}
            </p>
          </div>
        )}
      </div>
    </div>
  );
};

/**
 * EnvStatusCard Component
 * Shows a card indicating backend environment configuration status
 */
export const EnvStatusCard = ({ envStatus, dataStatus }) => {
  const isConfigured = envStatus.status === 'configured' && dataStatus.valid;

  return (
    <div className="card">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <div className={`p-2 rounded-lg ${
            isConfigured
              ? 'bg-spacemacs-light-green/10 dark:bg-spacemacs-dark-green/10'
              : 'bg-spacemacs-light-red/10 dark:bg-spacemacs-dark-red/10'
          }`}>
            {isConfigured ? (
              <svg
                className="w-6 h-6 text-spacemacs-light-green dark:text-spacemacs-dark-green"
                fill="currentColor"
                viewBox="0 0 20 20"
              >
                <path
                  fillRule="evenodd"
                  d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.707-9.293a1 1 0 00-1.414-1.414L9 10.586 7.707 9.293a1 1 0 00-1.414 1.414l2 2a1 1 0 001.414 0l4-4z"
                  clipRule="evenodd"
                />
              </svg>
            ) : (
              <svg
                className="w-6 h-6 text-spacemacs-light-red dark:text-spacemacs-dark-red"
                fill="none"
                stroke="currentColor"
                viewBox="0 0 24 24"
              >
                <path
                  strokeLinecap="round"
                  strokeLinejoin="round"
                  strokeWidth={2}
                  d="M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-3L13.732 4c-.77-1.333-2.694-1.333-3.464 0L3.34 16c-.77 1.333.192 3 1.732 3z"
                />
              </svg>
            )}
          </div>
          <div>
            <h3 className="font-bold text-lg">
              {isConfigured ? 'Backend Configured' : 'Backend Not Configured'}
            </h3>
            <p className="text-sm text-gray-600 dark:text-gray-400">
              {envStatus.message}
            </p>
          </div>
        </div>

        {envStatus.url && !isDevelopment && isConfigured && (
          <a
            href={envStatus.url}
            target="_blank"
            rel="noopener noreferrer"
            className="px-4 py-2 bg-spacemacs-light-accent/10 dark:bg-spacemacs-dark-accent/10 text-spacemacs-light-accent dark:text-spacemacs-dark-accent rounded-md hover:bg-spacemacs-light-accent/20 dark:hover:bg-spacemacs-dark-accent/20 transition-colors text-sm font-medium"
          >
            View Secrets →
          </a>
        )}
      </div>
    </div>
  );
};

export default EnvOverlay;
