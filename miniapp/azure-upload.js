// Chunked direct-to-Azure upload (Put Block / Put Block List). No DOM code: fully testable in Node.
// The SAS URL lives only in memory (never in localStorage).

const RETRY_DELAYS_MS = [1000, 2000, 4000, 8000, 16000];
const PROGRESS_INTERVAL_MS = 200; // UI update rate limit: 5 per second
const SPEED_WINDOW_MS = 10000;
const INFLIGHT_CAP = 0.9; // an in-flight block never counts for more than 90% of its size

export const blockId = (index) => btoa(String(index).padStart(6, "0"));

export class UploadAbortedError extends Error {
  constructor() {
    super("Upload aborted");
    this.name = "UploadAbortedError";
  }
}

export class UploadFailedError extends Error {
  constructor(message, status = 0, fatal = false) {
    super(message);
    this.name = "UploadFailedError";
    this.status = status;
    this.fatal = fatal;
  }
}

const isRetryable = (status) => status >= 500 || status === 408 || status === 429;

export function abortableSleep(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) return reject(new UploadAbortedError());
    const onAbort = () => {
      clearTimeout(timer);
      reject(new UploadAbortedError());
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

// Bytes of completed blocks + an estimate for blocks in flight (fetch has no upload progress events).
export class ProgressTracker {
  constructor(totalBytes, now = Date.now) {
    this.totalBytes = totalBytes;
    this.now = now;
    this.startedAt = now();
    this.preloaded = 0; // already on the server before this session (resume)
    this.sessionBytes = 0;
    this.samples = []; // [time, sessionBytes] after each completed block
    this.inflight = new Map(); // index -> {size, startedAt}
  }

  addPreloaded(bytes) {
    this.preloaded += bytes;
  }

  blockStarted(index, size) {
    this.inflight.set(index, { size, startedAt: this.now() });
  }

  blockFailed(index) {
    this.inflight.delete(index);
  }

  blockDone(index, size) {
    this.inflight.delete(index);
    this.sessionBytes += size;
    const t = this.now();
    this.samples.push([t, this.sessionBytes]);
    while (this.samples.length > 2 && t - this.samples[0][0] > SPEED_WINDOW_MS) this.samples.shift();
  }

  speedBps() {
    const t = this.now();
    if (this.samples.length >= 2) {
      const [t0, b0] = this.samples[0];
      const [t1, b1] = this.samples[this.samples.length - 1];
      if (t1 > t0) return ((b1 - b0) / (t1 - t0)) * 1000;
    }
    const elapsed = (t - this.startedAt) / 1000;
    return elapsed > 0.5 ? this.sessionBytes / elapsed : 0;
  }

  snapshot() {
    const speed = this.speedBps();
    const t = this.now();
    let estimate = 0;
    if (speed > 0 && this.inflight.size > 0) {
      const perSlot = speed / this.inflight.size;
      for (const { size, startedAt } of this.inflight.values()) {
        estimate += Math.min(size * INFLIGHT_CAP, (perSlot * (t - startedAt)) / 1000);
      }
    }
    const loaded = Math.min(this.totalBytes, this.preloaded + this.sessionBytes + estimate);
    const remaining = this.totalBytes - loaded;
    return {
      loadedBytes: loaded,
      totalBytes: this.totalBytes,
      percent: Math.floor((loaded / this.totalBytes) * 100),
      speedBps: speed,
      etaSec: speed > 0 ? Math.ceil(remaining / speed) : null,
    };
  }
}

export function blockListXml(ids) {
  return (
    '<?xml version="1.0" encoding="utf-8"?><BlockList>' +
    ids.map((id) => `<Latest>${id}</Latest>`).join("") +
    "</BlockList>"
  );
}

/**
 * Upload `file` to `uploadUrl` in blocks and commit the block list.
 * Resolves after the commit (HTTP 201); the caller then calls POST /api/uploads/{id}/complete.
 *
 * getUploadedBlockIds(): Promise<string[]>   ids (base64) already on the server -> skipped
 * refreshUploadUrl():    Promise<string>     fresh SAS URL (called on HTTP 403)
 * onProgress(snapshot):  called at most 5x/s, and once at the end
 */
export async function uploadFile({
  file,
  uploadUrl,
  blockSize,
  maxParallel,
  getUploadedBlockIds,
  refreshUploadUrl,
  onProgress,
  signal,
  fetchImpl = (...args) => fetch(...args),
  sleep = abortableSleep,
  now = Date.now,
}) {
  if (!file.size) throw new UploadFailedError("Empty file", 0, true);
  const count = Math.ceil(file.size / blockSize);
  const ids = Array.from({ length: count }, (_, i) => blockId(i));
  const sizeOf = (i) => Math.min(blockSize, file.size - i * blockSize);

  const alreadyUploaded = new Set(await getUploadedBlockIds());
  const tracker = new ProgressTracker(file.size, now);
  const pending = [];
  for (let i = 0; i < count; i++) {
    if (alreadyUploaded.has(ids[i])) tracker.addPreloaded(sizeOf(i));
    else pending.push(i);
  }

  const ctrl = new AbortController();
  const onExternalAbort = () => ctrl.abort();
  if (signal?.aborted) ctrl.abort();
  else signal?.addEventListener("abort", onExternalAbort, { once: true });

  let lastEmit = -Infinity;
  const emit = (force = false) => {
    const t = now();
    if (!force && t - lastEmit < PROGRESS_INTERVAL_MS) return;
    lastEmit = t;
    onProgress?.(tracker.snapshot());
  };
  const ticker = setInterval(emit, PROGRESS_INTERVAL_MS);

  let url = uploadUrl;
  let refreshing = null;
  const freshUrl = (stale) => {
    if (url !== stale) return Promise.resolve(url);
    refreshing ??= refreshUploadUrl()
      .then((next) => (url = next))
      .finally(() => (refreshing = null));
    return refreshing;
  };

  // One HTTP request with retries. `build(url)` returns [fullUrl, init]. Success = HTTP 201.
  async function send(build) {
    let lastError;
    for (let attempt = 0; attempt <= RETRY_DELAYS_MS.length; attempt++) {
      if (ctrl.signal.aborted) throw new UploadAbortedError();
      const used = url;
      let refreshed = false;
      try {
        const [target, init] = build(used);
        const response = await fetchImpl(target, { ...init, signal: ctrl.signal });
        if (response.status === 201) return;
        if (response.status === 403) {
          try {
            refreshed = (await freshUrl(used)) !== used;
          } catch (error) {
            throw new UploadFailedError(`Cannot refresh upload URL: ${error.message}`, 403, true);
          }
          lastError = new UploadFailedError("Upload URL rejected (403)", 403);
        } else if (isRetryable(response.status)) {
          lastError = new UploadFailedError(`Server error ${response.status}`, response.status);
        } else {
          throw new UploadFailedError(`Unexpected status ${response.status}`, response.status, true);
        }
      } catch (error) {
        if (ctrl.signal.aborted || error instanceof UploadAbortedError || error?.name === "AbortError") {
          throw new UploadAbortedError();
        }
        if (error instanceof UploadFailedError && error.fatal) throw error;
        lastError = error;
      }
      if (refreshed) continue; // new URL in hand: retry right away
      if (attempt < RETRY_DELAYS_MS.length) await sleep(RETRY_DELAYS_MS[attempt], ctrl.signal);
    }
    throw lastError ?? new UploadFailedError("Upload failed");
  }

  async function putBlock(index) {
    const size = sizeOf(index);
    const body = file.slice(index * blockSize, index * blockSize + size);
    tracker.blockStarted(index, size);
    try {
      await send((u) => [`${u}&comp=block&blockid=${encodeURIComponent(ids[index])}`, { method: "PUT", body }]);
    } catch (error) {
      tracker.blockFailed(index);
      throw error;
    }
    tracker.blockDone(index, size);
    emit();
  }

  try {
    emit(true);
    const queue = [...pending];
    let firstError = null;
    const worker = async () => {
      while (queue.length && !firstError) {
        try {
          await putBlock(queue.shift());
        } catch (error) {
          firstError ??= error;
          ctrl.abort(); // stop the other workers
          return;
        }
      }
    };
    await Promise.all(Array.from({ length: Math.min(maxParallel, queue.length) }, worker));
    if (firstError) throw firstError;

    await send((u) => [
      `${u}&comp=blocklist`,
      {
        method: "PUT",
        headers: {
          "Content-Type": "application/xml",
          "x-ms-blob-content-type": file.type || "video/mp4",
        },
        body: blockListXml(ids),
      },
    ]);
    emit(true);
    return { blockCount: count, uploadedBlocks: pending.length };
  } finally {
    clearInterval(ticker);
    signal?.removeEventListener("abort", onExternalAbort);
  }
}
