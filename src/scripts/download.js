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
    setInspecting(true);
    showFeedback('Checking your media link...');

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
    showFeedback('');
  });
}
