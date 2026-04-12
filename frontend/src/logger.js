const isDev = typeof import.meta !== 'undefined' && import.meta.env && import.meta.env.DEV;
function emit(level, message, context) {
  const ts = new Date().toISOString();
  if (isDev) {
    const fn = console[level] || console.log; // fallback
    if (context) fn(`[sagasu-2] ${ts} ${message}`, context);
    else fn(`[sagasu-2] ${ts} ${message}`);
  } else {
    const entry = { ts, level, message, ...(context || {}) };
    const fn = console[level] || console.log;
    fn(JSON.stringify(entry));
  }
}
export const logger = {
  info: (message, context) => emit('info', message, context),
  warn: (message, context) => emit('warn', message, context),
  error: (message, context) => emit('error', message, context),
  debug: (message, context) => emit('debug', message, context),
};
export default logger;
