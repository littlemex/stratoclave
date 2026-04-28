/*
 * Stratoclave SPA fallback (CloudFront Function, viewer-request)
 *
 * - /api/* and /v1/* are ALB pass-through cache behaviors; URIs must not be rewritten.
 * - Static assets (assets/*.js, *.css, *.png, /config.json, etc.) must be served as-is.
 * - Virtual SPA routes (React Router deep links such as /callback, /admin/..., /dashboard)
 *   are rewritten to /index.html so the SPA can boot and resume client-side routing.
 *
 * Attach this function ONLY to the default cache behavior (the S3 origin).
 * If it were attached to /api/* or /v1/*, legitimate ALB 4xx/5xx responses
 * would be corrupted into HTML, breaking Frontend fetches.
 */
function handler(event) {
  var req = event.request;
  var uri = req.uri;

  // /api/*, /v1/*, and /.well-known/* must pass through untouched
  // (defensive belt-and-braces; these also have their own CloudFront
  // cache behaviors that forward to the ALB origin).
  if (
    uri.indexOf('/api/') === 0 ||
    uri.indexOf('/v1/') === 0 ||
    uri.indexOf('/.well-known/') === 0
  ) {
    return req;
  }

  // Static assets (any path whose last segment contains a '.') pass through.
  var lastSlash = uri.lastIndexOf('/');
  var lastSegment = uri.substring(lastSlash + 1);
  if (lastSegment.indexOf('.') >= 0) {
    return req;
  }

  // SPA fallback: rewrite to /index.html so the SPA can take over routing.
  req.uri = '/index.html';
  return req;
}
