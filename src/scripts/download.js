const downloadForm = document.querySelector('#download-form');
const mediaUrl = document.querySelector('#media-url');
const feedback = document.querySelector('#form-feedback');
const downloadButton = downloadForm?.querySelector('button[type="submit"]');
const resultsPanel = document.querySelector('#download-results');
const resultTitle = document.querySelector('#download-result-title');
const resultMeta = document.querySelector('#download-result-meta');
const formatSelect = document.querySelector('#download-format');
const selectedDownloadButton = document.querySelector('#download-selected');

const apiBase = (downloadForm?.dataset.apiBase || '').replace(/\/+$/, '');
let inspectedMedia = null;

// The preview is inserted immediately below the paste box, so the layout
// stays compact and the rest of the existing landing page is untouched.
const previewPanel = document.createElement('section');
previewPanel.className = 'media-preview';
previewPanel.id = 'media-preview';
previewPanel.setAttribute('aria-label', 'Media preview');
previewPanel.hidden = true;

const previewHeading = document.createElement('div');
previewHeading.className = 'media-preview-heading';
previewHeading.textContent = 'Video preview';

const previewFrame = document.createElement('div');
previewFrame.className = 'media-preview-frame';

const previewNote = document.createElement('p');
previewNote.className = 'media-preview-note';
previewNote.textContent = 'Preview availability depends on the source website.';

previewPanel.append(previewHeading, previewFrame, previewNote);
downloadForm?.insertAdjacentElement('afterend', previewPanel);

function showFeedback(message, isError = false) {
  if (!feedback) return;
  feedback.textContent = message;
  feedback.dataset.state = isError ? 'error' : 'info';
}

function setInspecting(isBusy) {
  if (!(downloadButton instanceof HTMLButtonElement)) return;
  downloadButton.disabled = isBusy;
  const label = downloadButton.querySelector('span');
  if (label) label.textContent = isBusy ? 'Checking...' : 'Download Now';
}

function formatBytes(value) {
  if (!Number.isFinite(value) || value <= 0) return 'Size not provided';
  const units = ['B', 'KB', 'MB', 'GB'];
  let amount = value;
  let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) {
    amount /= 1024;
    unit += 1;
  }
  return `${amount.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

async function readApiResponse(response) {
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.error || payload.message || `Request failed (${response.status}).`);
  }
  return payload;
}

function populateFormats(formats) {
  if (!(formatSelect instanceof HTMLSelectElement)) return;
  formatSelect.replaceChildren();
  for (const format of formats) {
    if (!format || typeof format.id !== 'string' || typeof format.label !== 'string') continue;
    const option = document.createElement('option');
    option.value = format.id;
    option.textContent = format.label;
    option.disabled = format.available === false;
    formatSelect.append(option);
  }
}

function getPreviewSource(rawUrl) {
  let url;
  try {
    url = new URL(rawUrl);
  } catch {
    throw new Error('Enter a valid media link to preview it.');
  }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) {
    throw new Error('Only public web links can be previewed.');
  }

  const host = url.hostname.toLowerCase().replace(/^www\./, '');
  const pathParts = url.pathname.split('/').filter(Boolean);

  // YouTube videos, Shorts, live streams and youtu.be links.
  if (host === 'youtube.com' || host === 'm.youtube.com' || host === 'youtu.be' || host === 'youtube-nocookie.com') {
    let id = url.searchParams.get('v');
    if (host === 'youtu.be') id = pathParts[0];
    if (!id) {
      const marker = pathParts.findIndex((part) => ['shorts', 'embed', 'live', 'v'].includes(part));
      if (marker >= 0) id = pathParts[marker + 1];
    }
    if (id && /^[A-Za-z0-9_-]{6,20}$/.test(id)) {
      return { type: 'iframe', src: `https://www.youtube-nocookie.com/embed/${encodeURIComponent(id)}?rel=0` };
    }
  }

  // Vimeo.
  if (host === 'vimeo.com' || host === 'player.vimeo.com') {
    const id = pathParts.find((part) => /^\d{6,12}$/.test(part));
    if (id) return { type: 'iframe', src: `https://player.vimeo.com/video/${id}` };
  }

  // Dailymotion and dai.ly.
  if (host === 'dailymotion.com' || host === 'dai.ly') {
    let id = pathParts[0] === 'video' ? pathParts[1] : pathParts[0];
    if (id && /^[A-Za-z0-9]+$/.test(id)) {
      return { type: 'iframe', src: `https://www.dailymotion.com/embed/video/${encodeURIComponent(id)}` };
    }
  }

  // Instagram public posts, reels and IGTV clips.
  if (host === 'instagram.com') {
    const kindIndex = pathParts.findIndex((part) => ['p', 'reel', 'reels', 'tv'].includes(part));
    const shortcode = kindIndex >= 0 ? pathParts[kindIndex + 1] : '';
    if (shortcode && /^[A-Za-z0-9_-]+$/.test(shortcode)) {
      const kind = pathParts[kindIndex] === 'reels' ? 'reel' : pathParts[kindIndex];
      return { type: 'iframe', src: `https://www.instagram.com/${kind}/${encodeURIComponent(shortcode)}/embed/` };
    }
  }

  // TikTok public videos.
  if (host === 'tiktok.com') {
    const videoIndex = pathParts.indexOf('video');
    const id = videoIndex >= 0 ? pathParts[videoIndex + 1] : '';
    if (id && /^\d+$/.test(id)) {
      return { type: 'iframe', src: `https://www.tiktok.com/player/v1/${id}?music_info=1&description=1` };
    }
  }

  // Facebook video plugin.
  if (host === 'facebook.com' || host === 'fb.watch') {
    const embed = new URL('https://www.facebook.com/plugins/video.php');
    embed.searchParams.set('href', url.toString());
    embed.searchParams.set('show_text', 'false');
    embed.searchParams.set('width', '960');
    return { type: 'iframe', src: embed.toString() };
  }

  // Direct video files can be previewed by the browser without an iframe.
  const extension = url.pathname.toLowerCase().match(/\.([a-z0-9]{2,5})$/)?.[1] || '';
  if (['mp4', 'm4v', 'webm', 'mov', 'ogv', 'm3u8'].includes(extension)) {
    return { type: 'video', src: url.toString() };
  }

  // For other public pages, try their own embedded player. Some hosts block
  // iframe embedding; the download inspection still runs independently.
  return { type: 'iframe', src: url.toString(), generic: true };
}

function showPreview(rawUrl) {
  previewFrame.replaceChildren();
  previewNote.textContent = 'Preview availability depends on the source website.';

  let source;
  try {
    source = getPreviewSource(rawUrl);
  } catch (error) {
    previewPanel.hidden = false;
    previewNote.textContent = error instanceof Error ? error.message : 'This link cannot be previewed.';
    return;
  }

  if (source.type === 'video') {
    const video = document.createElement('video');
    video.className = 'media-preview-player';
    video.controls = true;
    video.playsInline = true;
    video.preload = 'metadata';
    video.src = source.src;
    video.setAttribute('aria-label', 'Media preview player');
    video.addEventListener('error', () => {
      previewNote.textContent = 'This video format cannot be previewed in this browser. You can still use the download options if the link is supported.';
    }, { once: true });
    previewFrame.append(video);
  } else {
    const iframe = document.createElement('iframe');
    iframe.className = 'media-preview-player';
    iframe.src = source.src;
    iframe.title = 'Embedded media preview';
    iframe.loading = 'lazy';
    iframe.referrerPolicy = 'strict-origin-when-cross-origin';
    iframe.allow = 'accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share';
    iframe.allowFullscreen = true;
    if (source.generic) {
      iframe.setAttribute('sandbox', 'allow-scripts allow-same-origin allow-presentation');
      previewNote.textContent = 'If the source website blocks embedding, its preview may not appear here.';
    }
    previewFrame.append(iframe);
  }

  previewPanel.hidden = false;
}

if (
  downloadForm instanceof HTMLFormElement &&
  mediaUrl instanceof HTMLInputElement &&
  feedback instanceof HTMLElement &&
  downloadButton instanceof HTMLButtonElement &&
  resultsPanel instanceof HTMLElement &&
  resultTitle instanceof HTMLElement &&
  resultMeta instanceof HTMLElement &&
  formatSelect instanceof HTMLSelectElement &&
  selectedDownloadButton instanceof HTMLButtonElement
) {
  downloadForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const url = mediaUrl.value.trim();

    if (!url) {
      showFeedback('Paste a media link to continue.', true);
      mediaUrl.focus();
      return;
    }

    if (!apiBase) {
      showFeedback('The download service is not configured yet.', true);
      return;
    }

    inspectedMedia = null;
    resultsPanel.hidden = true;
    showPreview(url);
    setInspecting(true);
    showFeedback('Loading the video preview and checking your link...');

    try {
      const response = await fetch(`${apiBase}/v1/inspect`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ url }),
        signal: AbortSignal.timeout(90000)
      });
      const result = await readApiResponse(response);
      if (!Array.isArray(result.formats) || result.formats.length === 0) {
        throw new Error('No downloadable formats were returned for this link.');
      }

      inspectedMedia = { url, result };
      resultTitle.textContent = result.title || 'Media file detected';
      resultMeta.textContent = [
        result.contentType,
        formatBytes(result.sizeBytes)
      ].filter(Boolean).join(' · ');
      populateFormats(result.formats);
      resultsPanel.hidden = false;
      showFeedback('Your media is ready. Choose a format to download.');
    } catch (error) {
      showFeedback(error instanceof Error ? error.message : 'Unable to inspect this link.', true);
    } finally {
      setInspecting(false);
    }
  });

  selectedDownloadButton.addEventListener('click', () => {
    if (!inspectedMedia) {
      showFeedback('Check a media link first.', true);
      return;
    }
    const format = formatSelect.value;
    if (!format) {
      showFeedback('Choose an available format.', true);
      return;
    }

    const form = document.createElement('form');
    form.method = 'POST';
    form.action = `${apiBase}/v1/download`;
    form.style.display = 'none';

    for (const [name, value] of [['url', inspectedMedia.url], ['format', format]]) {
      const input = document.createElement('input');
      input.type = 'hidden';
      input.name = name;
      input.value = value;
      form.append(input);
    }

    document.body.append(form);
    showFeedback('Preparing your download...');
    form.submit();
    window.setTimeout(() => form.remove(), 60000);
  });

  mediaUrl.addEventListener('input', () => {
    inspectedMedia = null;
    resultsPanel.hidden = true;
    previewPanel.hidden = true;
    previewFrame.replaceChildren();
    showFeedback('');
  });
}
