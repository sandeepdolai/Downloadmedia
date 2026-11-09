import {
  ApiError,
  forwardJsonToEngine,
  inspectMediaUrl,
  isPlainHttpUrl,
  looksLikeManifest,
  parseRequestBody,
  sendApiError,
  sendJson
} from '../_lib/media.js';

export default async function handler(req, res) {
  if (req.method !== 'POST') {
    res.setHeader('Allow', 'POST');
    return sendJson(res, 405, { error: 'Method not allowed.' });
  }

  let body;
  try {
    body = parseRequestBody(req);
  } catch (error) {
    return sendApiError(res, error);
  }

  const urlValue = typeof body.url === 'string' ? body.url.trim() : '';
  if (!urlValue || urlValue.length > 4096) {
    return sendApiError(res, new ApiError(400, 'Enter a valid media URL.'));
  }

  // Direct HTTPS media files are inspected locally (fast path). Everything
  // else — webpages, extractor sites, HTTP links, HLS/DASH manifests — goes
  // to the universal Python engine.
  if (!isPlainHttpUrl(urlValue) && !looksLikeManifest(urlValue)) {
    try {
      const result = await inspectMediaUrl(urlValue);
      return sendJson(res, 200, { ok: true, ...result });
    } catch (error) {
      const status = error instanceof ApiError ? error.statusCode : 500;
      // Size limits and invalid input are final; anything else (webpage,
      // access denied, unreachable) deserves the engine's full cascade.
      if (status === 400 || status === 413) return sendApiError(res, error);
    }
  }

  try {
    return await forwardJsonToEngine(res, '/v1/inspect', { url: urlValue }, 85000);
  } catch (error) {
    return sendApiError(res, error);
  }
}
