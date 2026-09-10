/* Loaded before application scripts: credentials stay inside this origin. */
(() => {
  "use strict";
  const nativeFetch = window.fetch.bind(window);
  let sessionPromise = null;

  function sessionToken() {
    if (!sessionPromise) {
      sessionPromise = nativeFetch("/api/local-session", {
        headers: { "X-Amadeus-Client": "desktop" },
        cache: "no-store",
        credentials: "same-origin",
      }).then(async (response) => {
        if (!response.ok) throw new Error("local_session_bootstrap_failed");
        const payload = await response.json();
        if (!payload.token) throw new Error("local_session_token_missing");
        return payload.token;
      }).catch((error) => {
        sessionPromise = null;
        throw error;
      });
    }
    return sessionPromise;
  }

  window.fetch = async (resource, options = {}) => {
    const isRequest = resource instanceof Request;
    const url = new URL(isRequest ? resource.url : resource, window.location.href);
    const method = String(options.method || (isRequest ? resource.method : "GET")).toUpperCase();
    if (url.origin !== window.location.origin || !url.pathname.startsWith("/api/") || method !== "POST") {
      return nativeFetch(resource, options);
    }
    const headers = new Headers(options.headers || (isRequest ? resource.headers : undefined));
    headers.set("X-Amadeus-Session", await sessionToken());
    const retryResource = isRequest ? resource.clone() : resource;
    const response = await nativeFetch(resource, { ...options, headers });
    if (response.status !== 403) return response;
    const failure = await response.clone().json().catch(() => ({}));
    // Only this pre-dispatch rejection guarantees the POST had no side effects.
    if (failure.code !== "local_session_required") return response;
    sessionPromise = null;
    headers.set("X-Amadeus-Session", await sessionToken());
    return nativeFetch(retryResource, { ...options, headers });
  };
})();
