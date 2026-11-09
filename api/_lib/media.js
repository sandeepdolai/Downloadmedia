import { Agent, fetch } from 'undici';
import ipaddr from 'ipaddr.js';
import { lookup } from 'node:dns/promises';
import { isIP } from 'node:net';
import { Readable, Transform } from 'node:stream';
import { pipeline } from 'node:stream/promises';

const MAX_FILE_SIZE = Math.max(1, Number.parseInt(process.env.MAX_FILE_SIZE_MB || '300', 10)) * 1024 * 1024;
const REDIRECT_LIMIT = 4;

const MEDIA_EXTENSIONS = [
  '.mp4', '.m4v', '.mov', '.webm', '.mkv', '.avi', '.flv', '.ts', '.wmv', '.mpg', '.mpeg', '.3gp',
  '.mp3', '.m4a', '.aac', '.wav', '.ogg', '.oga', '.opus', '.flac', '.wma', '.aiff',
  '.jpg', '.jpeg', '.png', '.gif', '.webp', '.avif', '.bmp'
];
const IMAGE_EXTENSIONS = ['.jpg', '.jpeg', '.png', '.gif', '.webp', '.avif', '.bmp', '.svg'];
const MANIFEST_EXTENSIONS = ['.m3u8', '.mpd', '.m3u'];

export class ApiError extends Error {
  constructor(statusCode, message) {
    super(message);
    this.statusCode = statusCode;
  }
}

export function parseRequestBody(req) {
  if (req.body && typeof req.body === 'object' && !Buffer.isBuffer(req.body)) return req.body;
  if (Buffer.isBuffer(req.body)) req.body = req.body.toString('utf8');
  if (typeof req.body === 'string') {
    const contentType = String(req.headers['content-type'] || '');
    if (contentType.includes('application/x-www-form-urlencoded')) {
      return Object.fromEntries(new URLSearchParams(req.body));
    }
    try { return JSON.parse(req.body); } catch { throw new ApiError(400, 'The request body is invalid.'); }
  }
  return {};
}

export function sendJson(res, statusCode, payload) {
  if (res.headersSent) {
    res.destroy();
    return;
  }
  res.statusCode = statusCode;
  res.setHeader('Content-Type', 'application/json; charset=utf-8');
  res.setHeader('Cache-Control', 'no-store');
  res.end(JSON.stringify(payload));
}

export function sendApiError(res, error) {
  const status = Number.isInteger(error?.statusCode) ? error.statusCode : 500;
  if (status >= 500) console.error('MemorableDay API error:', error);
  sendJson(res, status, {
    error: status >= 500 ? 'The download service encountered a problem. Try again.' : error.message
  });
}

export function validateMediaUrl(value) {
  if (typeof value !== 'string' || value.length > 4096) {
    throw new ApiError(400, 'Enter a valid media URL.');
  }
  let url;
  try { url = new URL(value); } catch { throw new ApiError(400, 'Enter a valid media URL.'); }
  if (url.protocol !== 'https:') throw new ApiError(400, 'Only HTTPS media URLs are supported.');
  if (url.username || url.password) throw new ApiError(400, 'URLs containing embedded credentials are not supported.');
  if (url.port && url.port !== '443') throw new ApiError(400, 'Only standard HTTPS links are supported.');
  url.hash = '';
  return url;
}

function assertPublicIp(address) {
  let parsed;
  try { parsed = ipaddr.parse(address); }
  catch { throw new ApiError(400, 'The media host does not resolve to a public address.'); }
  if (parsed.kind() === 'ipv6' && parsed.isIPv4MappedAddress()) parsed = parsed.toIPv4Address();
  if (parsed.range() !== 'unicast') {
    throw new ApiError(400, 'Links to private, local or reserved network addresses are not allowed.');
  }
}

async function resolvePublicAddresses(hostname) {
  const normalized = hostname.replace(/^\[|\]$/g, '').toLowerCase();
  if (
    normalized === 'localhost' ||
    normalized.endsWith('.localhost') ||
    normalized.endsWith('.local') ||
    normalized.endsWith('.internal') ||
    normalized.endsWith('.test')
  ) {
    throw new ApiError(400, 'Local and private network addresses are not allowed.');
  }
  let records;
  if (isIP(normalized)) {
    records = [{ address: normalized, family: isIP(normalized) }];
  } else {
    try { records = await lookup(normalized, { all: true, verbatim: true }); }
    catch { throw new ApiError(422, 'The media host could not be resolved.'); }
  }
  if (!records.length) throw new ApiError(422, 'The media host could not be resolved.');
  for (const record of records) assertPublicIp(record.address);
  return records;
}

function createPinnedAgent(hostname, addresses) {
  const expectedHost = hostname.replace(/^\[|\]$/g, '').toLowerCase();
  const pinnedLookup = (requestedHost, options, callback) => {
    const requested = requestedHost.replace(/^\[|\]$/g, '').toLowerCase();
    if (requested !== expectedHost) return callback(new Error('Blocked unexpected DNS lookup.'));
    const opts = typeof options === 'object' && options !== null ? options : { family: options };
    const available = addresses.filter((entry) => !opts.family || opts.family === 0 || entry.family === opts.family);
    if (!available.length) return callback(new Error('No public IP is available.'));
    if (opts.all) {
      return callback(null, available.map((entry) => ({ address: entry.address, family: entry.family })));
    }
    return callback(null, available[0].address, available[0].family);
  };
  return new Agent({ connect: { lookup: pinnedLookup } });
}

export async function openRemote(initialUrl, method, extraHeaders = {}, timeoutMs = 20000) {
  let currentUrl = validateMediaUrl(initialUrl);
  for (let redirects = 0; redirects <= REDIRECT_LIMIT; redirects += 1) {
    const addresses = await resolvePublicAddresses(currentUrl.hostname);
    const dispatcher = createPinnedAgent(currentUrl.hostname, addresses);
    let response;
    try {
      response = await fetch(currentUrl, {
        method,
        redirect: 'manual',
        dispatcher,
        headers: {
          'user-agent': 'MemorableDayDownloader/1.0 (+https://memorableday.in)',
          ...extraHeaders
        },
        signal: AbortSignal.timeout(timeoutMs)
      });
    } catch (error) {
      await dispatcher.close().catch(() => {});
      if (error instanceof ApiError) throw error;
      throw new ApiError(502, 'The media source could not be reached. Check the link and try again.');
    }

    if ([301, 302, 303, 307, 308].includes(response.status)) {
      const location = response.headers.get('location');
      await response.body?.cancel().catch(() => {});
      await dispatcher.close().catch(() => {});
      if (!location || redirects === REDIRECT_LIMIT) throw new ApiError(502, 'The media source returned too many redirects.');
      try { currentUrl = validateMediaUrl(new URL(location, currentUrl).toString()); }
      catch (error) {
        if (error instanceof ApiError) throw error;
        throw new ApiError(502, 'The media source returned an invalid redirect.');
      }
      continue;
    }

    if (!response.ok && !(method === 'HEAD' && [405, 501].includes(response.status))) {
      await response.body?.cancel().catch(() => {});
      await dispatcher.close().catch(() => {});
      if (response.status === 401 || response.status === 403) {
        throw new ApiError(422, 'This media requires access or permission that the downloader does not have.');
      }
      throw new ApiError(502, 'The media host returned HTTP ' + response.status + '.');
    }
    return { response, dispatcher, url: currentUrl };
  }
  throw new ApiError(502, 'The media source could not be reached.');
}

export function extensionFrom(url) {
  const match = url.pathname.toLowerCase().match(/\.[a-z0-9]{2,5}$/);
  return match ? match[0] : '';
}

export function looksLikeManifest(urlValue) {
  try {
    const url = new URL(urlValue);
    return MANIFEST_EXTENSIONS.includes(url.pathname.toLowerCase().match(/\.[a-z0-9]{2,5}$/)?.[0] || '');
  } catch {
    return false;
  }
}

export function isPlainHttpUrl(urlValue) {
  return /^http:\/\//i.test(String(urlValue || '').trim());
}

export function engineBaseUrl() {
  return (process.env.DOWNLOADER_API_URL || 'https://memorableday-downloader-api.onrender.com').replace(/\/+$/, '');
}

export function mediaTypeFrom(response) {
  return (response.headers.get('content-type') || '').split(';')[0].trim().toLowerCase();
}

export function isDirectMedia(response, url) {
  const type = mediaTypeFrom(response);
  if (type === 'text/html' || type === 'application/xhtml+xml') return false;
  const extension = extensionFrom(url);
  return type.startsWith('video/') ||
    type.startsWith('audio/') ||
    type.startsWith('image/') ||
    (type === 'application/octet-stream' && MEDIA_EXTENSIONS.includes(extension)) ||
    MEDIA_EXTENSIONS.includes(extension) ||
    IMAGE_EXTENSIONS.includes(extension);
}

export function filenameFrom(response, url, contentType) {
  const disposition = response.headers.get('content-disposition') || '';
  let candidate = '';
  const extended = disposition.match(/filename\*=utf-8''([^;]+)/i);
  const ordinary = disposition.match(/filename\s*=\s*"?([^";]+)"?/i);
  if (extended) {
    try { candidate = decodeURIComponent(extended[1]); } catch { candidate = extended[1]; }
  } else if (ordinary) {
    candidate = ordinary[1];
  } else {
    try { candidate = decodeURIComponent(url.pathname.split('/').filter(Boolean).pop() || 'media-file'); }
    catch { candidate = 'media-file'; }
  }
  candidate = candidate.normalize('NFKC')
    .replace(/[\\/\u0000-\u001f\u007f"<>:|?*]/g, '_')
    .replace(/\s+/g, ' ')
    .trim()
    .slice(0, 120);
  if (!candidate || candidate === '.' || candidate === '..') candidate = 'media-file';
  if (!/\.[a-z0-9]{2,5}$/i.test(candidate)) candidate += extensionFrom(url) || '.media';
  return candidate;
}

export async function inspectMediaUrl(urlValue) {
  let remote = await openRemote(urlValue, 'HEAD');
  if ([405, 501].includes(remote.response.status)) {
    await remote.dispatcher.close().catch(() => {});
    remote = await openRemote(urlValue, 'GET', { range: 'bytes=0-0' });
  }
  const { response, dispatcher, url } = remote;
  const contentType = mediaTypeFrom(response);
  let sizeBytes = Number.parseInt(response.headers.get('content-length') || '', 10);
  if (response.status === 206) {
    const total = response.headers.get('content-range')?.match(/\/(\d+)$/);
    if (total) sizeBytes = Number.parseInt(total[1], 10);
  }
  const filename = filenameFrom(response, url, contentType);
  const supported = isDirectMedia(response, url);
  await response.body?.cancel().catch(() => {});
  await dispatcher.close().catch(() => {});
  if (Number.isFinite(sizeBytes) && sizeBytes > MAX_FILE_SIZE) {
    throw new ApiError(413, 'This file is larger than the current download limit.');
  }
  if (!supported) {
    throw new ApiError(422, 'This link opens a webpage rather than a direct media file.');
  }
  const isAudioVideo = contentType.startsWith('video/') || contentType.startsWith('audio/') ||
    MEDIA_EXTENSIONS.includes(extensionFrom(url));
  const extensionLabel = extensionFrom(url).replace('.', '').toUpperCase() || 'MEDIA';
  const formats = [{ id: 'original', label: 'Original file (' + extensionLabel + ')', available: true }];
  if (isAudioVideo) formats.push({ id: 'mp3', label: 'MP3 audio · 192 kbps', available: true });
  return {
    title: filename,
    filename,
    contentType: contentType || 'application/octet-stream',
    sizeBytes: Number.isFinite(sizeBytes) ? sizeBytes : null,
    formats
  };
}

// ---------------------------------------------------------------------------
// Universal engine gateway
//
// Direct media files are handled locally above. Everything else — extractor
// sites (YouTube, Instagram, TikTok, ...), HLS/DASH manifests, and generic
// webpages with embedded media — is handled by the Python universal engine.
// ---------------------------------------------------------------------------

const ENGINE_COLD_START_MESSAGE =
  'The multi-site download engine is waking up. Please try again in about a minute.';

export async function forwardJsonToEngine(res, path, payload, timeoutMs = 85000) {
  let response;
  try {
    response = await fetch(engineBaseUrl() + path, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(timeoutMs)
    });
  } catch {
    throw new ApiError(503, ENGINE_COLD_START_MESSAGE);
  }
  const text = await response.text().catch(() => '');
  let enginePayload = null;
  try { enginePayload = JSON.parse(text); } catch { enginePayload = null; }
  if (!enginePayload || typeof enginePayload !== 'object') {
    throw new ApiError(502, 'The download engine returned an unexpected response.');
  }
  return sendJson(res, response.status, enginePayload);
}

export function withSizeLimit() {
  let total = 0;
  return new Transform({
    transform(chunk, _encoding, callback) {
      total += chunk.length;
      if (total > MAX_FILE_SIZE) {
        callback(new ApiError(413, 'This file is larger than the current download limit.'));
        return;
      }
      callback(null, chunk);
    }
  });
}

export async function forwardFileToEngine(res, urlValue, format, timeoutMs = 285000) {
  let response;
  try {
    response = await fetch(engineBaseUrl() + '/v1/download', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ url: urlValue, format }),
      signal: AbortSignal.timeout(timeoutMs)
    });
  } catch {
    throw new ApiError(503, ENGINE_COLD_START_MESSAGE);
  }

  if (!response.ok) {
    const text = await response.text().catch(() => '');
    let enginePayload = {};
    try { enginePayload = JSON.parse(text); } catch { enginePayload = {}; }
    const message = enginePayload && typeof enginePayload.error === 'string'
      ? enginePayload.error
      : 'The download engine could not process this link.';
    throw new ApiError(response.status, message);
  }

  if (!response.body) throw new ApiError(502, 'The download engine returned an empty response.');

  const headers = {
    'Content-Type': response.headers.get('content-type') || 'application/octet-stream',
    'Cache-Control': 'no-store',
    'X-Content-Type-Options': 'nosniff'
  };
  const disposition = response.headers.get('content-disposition');
  if (disposition) headers['Content-Disposition'] = disposition;
  const length = response.headers.get('content-length');
  if (length) headers['Content-Length'] = length;
  res.writeHead(200, headers);

  await pipeline(Readable.fromWeb(response.body), withSizeLimit(), res);
}
