// Install only in the scheduled scraper, before Microsoft's sign-in scripts.
// This never supplies a credential: it lets the caller cancel a pending request.
(() => {
  if (!["login.microsoft.com", "login.microsoftonline.com"].includes(location.hostname)) return;
  if (!navigator.credentials || window.__sagasuCancelPasskeyRequest) return;
  const original = navigator.credentials.get;
  const pending = new Set();
  navigator.credentials.get = function (options) {
    if (!options || !options.publicKey) return original.call(this, options);
    const controller = new AbortController();
    const originalSignal = options.signal;
    const forwardAbort = () => controller.abort(originalSignal.reason);
    if (originalSignal) {
      if (originalSignal.aborted) forwardAbort();
      else originalSignal.addEventListener("abort", forwardAbort, { once: true });
    }
    pending.add(controller);
    const cleanup = () => {
      pending.delete(controller);
      if (originalSignal) originalSignal.removeEventListener("abort", forwardAbort);
    };
    try {
      return original.call(this, { ...options, signal: controller.signal }).finally(cleanup);
    } catch (error) {
      cleanup();
      throw error;
    }
  };
  Object.defineProperty(window, "__sagasuCancelPasskeyRequest", {
    value: () => {
      const count = pending.size;
      for (const controller of pending) controller.abort();
      return count;
    }
  });
})();
