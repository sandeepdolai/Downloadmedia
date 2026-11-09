import { spawn } from 'node:child_process';
import { createReadStream, createWriteStream } from 'node:fs';
import { mkdtemp, rm, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import ffmpegPath from 'ffmpeg-static';
import {
  ApiError,
  forwardFileToEngine,
  isDirectMedia,
  isPlainHttpUrl,
  looksLikeManifest,
  mediaTypeFrom,
  filenameFrom,
  openRemote,
  parseRequestBody,
  sendApiError,
  sendJson,
  withSizeLimit
} from '../_lib/media.js';

function attachmentHeader(filename) {
  const ascii = filename.replace(/[^\x20-\x7E]/g, '_').replace(/["\\]/g, '_');
  return 'attachment; filename="' + ascii + '"; filename*=UTF-8\'\'' + encodeURIComponent(filename);
}

function maxFileSize() {
  return Math.max(1, Number.parseInt(process.env.MAX_FILE_SIZE_MB || '300', 10)) * 1024 * 1024;
}

export const config = {
  api: { bodyParser: { sizeLimit: '256kb' } }
};

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
  const format = typeof body.format === 'string' && body.format ? body.format : 'best';
  const cookiesText = typeof body.cookies === 'string' ? body.cookies : '';
  const userAgent = typeof body.user_agent === 'string' ? body.user_agent : '';
  const session = {
    ...(cookiesText ? { cookies: cookiesText } : {}),
    ...(userAgent ? { user_agent: userAgent } : {})
  };
  if (!urlValue || urlValue.length > 4096) {
    return sendApiError(res, new ApiError(400, 'Provide a media link.'));
  }

  // Only direct HTTPS media files with original/mp3 formats can be served
  // locally. Everything else (site formats like best/p1080/mp3 of an
  // extractor page, scraped page items, manifests, HTTP links) is produced
  // by the universal Python engine.
  const canServeLocally =
    (format === 'original' || format === 'mp3') &&
    !isPlainHttpUrl(urlValue) &&
    !looksLikeManifest(urlValue) &&
    !cookiesText;

  if (!canServeLocally) {
    try {
      return await forwardFileToEngine(res, urlValue, format, session);
    } catch (error) {
      return sendApiError(res, error);
    }
  }

  let dispatcher;
  let ffmpeg;
  let tempDir;
  try {
    let remote;
    try {
      remote = await openRemote(urlValue, 'GET', {}, 270000);
    } catch (error) {
      const status = error instanceof ApiError ? error.statusCode : 500;
      if (status === 400 || status === 413) throw error;
      // Unreachable or access-restricted from here: let the engine try with
      // its own client behaviour and extractor cascade.
      return await forwardFileToEngine(res, urlValue, format, session);
    }

    const response = remote.response;
    dispatcher = remote.dispatcher;
    const url = remote.url;

    if (!isDirectMedia(response, url)) {
      await response.body?.cancel().catch(() => {});
      await dispatcher.close().catch(() => {});
      dispatcher = null;
      return await forwardFileToEngine(res, urlValue, format, session);
    }
    const contentType = mediaTypeFrom(response);
    const sourceLength = Number.parseInt(response.headers.get('content-length') || '', 10);
    if (Number.isFinite(sourceLength) && sourceLength > maxFileSize()) {
      await response.body?.cancel().catch(() => {});
      throw new ApiError(413, 'This file is larger than the current download limit.');
    }
    if (!response.body) throw new ApiError(502, 'The media source returned an empty response.');

    const originalName = filenameFrom(response, url, contentType);
    const baseName = originalName.replace(/\.[^.]+$/, '') || 'media-file';

    const source = Readable.fromWeb(response.body);
    const limiter = withSizeLimit();
    res.on('close', () => {
      if (ffmpeg && !ffmpeg.killed) ffmpeg.kill('SIGTERM');
    });

    if (format === 'original') {
      res.writeHead(200, {
        'Content-Type': contentType || 'application/octet-stream',
        'Content-Disposition': attachmentHeader(originalName),
        'Cache-Control': 'no-store',
        'X-Content-Type-Options': 'nosniff',
        ...(Number.isFinite(sourceLength) ? { 'Content-Length': String(sourceLength) } : {})
      });
      try {
        await pipeline(source, limiter, res);
      } finally {
        await dispatcher.close().catch(() => {});
      }
      return;
    }

    // MP3 conversion: download to a seekable temp file first. MP4 inputs with
    // the moov atom at the end cannot be demuxed from a pipe, so a
    // stream-only conversion silently fails for many real-world files.
    if (!ffmpegPath) {
      await response.body.cancel().catch(() => {});
      throw new ApiError(503, 'MP3 conversion is unavailable on this deployment.');
    }

    tempDir = await mkdtemp(join(tmpdir(), 'memorableday-'));
    const inputPath = join(tempDir, 'input.media');
    const outputPath = join(tempDir, 'output.mp3');
    try {
      await pipeline(source, limiter, createWriteStream(inputPath));
      const written = await stat(inputPath);
      if (written.size <= 0) throw new ApiError(502, 'The media source returned an empty response.');

      await new Promise((resolve, reject) => {
        ffmpeg = spawn(ffmpegPath, [
          '-hide_banner', '-loglevel', 'error',
          '-i', inputPath,
          '-vn', '-c:a', 'libmp3lame', '-b:a', '192k',
          outputPath
        ], { stdio: ['ignore', 'ignore', 'pipe'] });
        ffmpeg.stderr.on('data', () => {});
        ffmpeg.once('error', reject);
        ffmpeg.once('close', (code) => {
          if (code === 0) resolve();
          else reject(new ApiError(422, 'This file does not contain audio that can be converted to MP3.'));
        });
      });

      const converted = await stat(outputPath).catch(() => null);
      if (!converted || converted.size <= 0) {
        throw new ApiError(422, 'This file does not contain audio that can be converted to MP3.');
      }
      res.writeHead(200, {
        'Content-Type': 'audio/mpeg',
        'Content-Disposition': attachmentHeader(baseName + '.mp3'),
        'Cache-Control': 'no-store',
        'X-Content-Type-Options': 'nosniff'
      });
      await pipeline(createReadStream(outputPath), res);
    } finally {
      if (ffmpeg && !ffmpeg.killed) ffmpeg.kill('SIGKILL');
      if (tempDir) await rm(tempDir, { recursive: true, force: true }).catch(() => {});
      await dispatcher.close().catch(() => {});
    }
  } catch (error) {
    if (ffmpeg && !ffmpeg.killed) ffmpeg.kill('SIGKILL');
    if (dispatcher) await dispatcher.close().catch(() => {});
    if (tempDir) await rm(tempDir, { recursive: true, force: true }).catch(() => {});
    if (res.headersSent) {
      if (!res.destroyed) res.destroy(error);
      return;
    }
    return sendApiError(res, error);
  }
}
