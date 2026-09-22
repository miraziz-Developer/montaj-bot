// Run: node --test tests/js/   (Node >= 22, no npm packages)
import assert from "node:assert/strict";
import test from "node:test";

import {
  ProgressTracker,
  UploadAbortedError,
  UploadFailedError,
  blockId,
  blockListXml,
  uploadFile,
} from "../../miniapp/azure-upload.js";

const BLOCK = 1000;
const URL0 = "http://azure.test/uploads/u/source.mp4?sas=one";
const noSleep = async () => {};

const makeFile = (bytes, type = "video/mp4") => new File([new Uint8Array(bytes)], "clip.mp4", { type });
const query = (call) => new URL(call.url).searchParams;

// Fake Azure: answers 201 to everything unless `respond(call, n)` says otherwise.
function fakeAzure(respond = () => 201, delayMs = 2) {
  const calls = [];
  let inflight = 0;
  const stats = { maxInflight: 0 };
  const fetchImpl = async (url, init) => {
    const call = { url, method: init.method, headers: init.headers ?? {}, body: init.body };
    calls.push(call);
    inflight += 1;
    stats.maxInflight = Math.max(stats.maxInflight, inflight);
    try {
      await new Promise((resolve, reject) => {
        const timer = setTimeout(resolve, delayMs);
        init.signal?.addEventListener("abort", () => {
          clearTimeout(timer);
          reject(Object.assign(new Error("aborted"), { name: "AbortError" }));
        });
      });
      const status = respond(call, calls.length);
      if (status instanceof Error) throw status;
      return { status };
    } finally {
      inflight -= 1;
    }
  };
  return { fetchImpl, calls, stats };
}

const baseOptions = (overrides) => ({
  file: makeFile(2500),
  uploadUrl: URL0,
  blockSize: BLOCK,
  maxParallel: 4,
  getUploadedBlockIds: async () => [],
  refreshUploadUrl: async () => URL0.replace("one", "two"),
  sleep: noSleep,
  ...overrides,
});

test("block ids are base64 of a zero-padded 6-digit index, all the same length", () => {
  assert.equal(blockId(0), "MDAwMDAw");
  assert.equal(blockId(1), "MDAwMDAx");
  assert.equal(new Set([0, 9, 10, 999999].map((i) => blockId(i).length)).size, 1);
});

test("uploads every block then commits the ordered block list", async () => {
  const azure = fakeAzure();
  const result = await uploadFile(baseOptions({ fetchImpl: azure.fetchImpl }));
  assert.deepEqual(result, { blockCount: 3, uploadedBlocks: 3 });

  const puts = azure.calls.filter((c) => query(c).get("comp") === "block");
  assert.deepEqual(puts.map((c) => query(c).get("blockid")).sort(), [0, 1, 2].map(blockId));
  assert.deepEqual(puts.map((c) => c.body.size).sort((a, b) => a - b), [500, 1000, 1000]);
  assert.ok(puts.every((c) => c.method === "PUT" && c.url.startsWith(URL0 + "&comp=block")));
  assert.ok(puts.every((c) => !("x-ms-version" in c.headers)));

  const commit = azure.calls.at(-1);
  assert.equal(query(commit).get("comp"), "blocklist");
  assert.equal(commit.headers["Content-Type"], "application/xml");
  assert.equal(commit.headers["x-ms-blob-content-type"], "video/mp4");
  assert.equal(commit.body, blockListXml([0, 1, 2].map(blockId)));
  assert.match(commit.body, /^<\?xml version="1.0" encoding="utf-8"\?><BlockList><Latest>MDAwMDAw<\/Latest>/);
});

test("commit falls back to video/mp4 when the file has no type", async () => {
  const azure = fakeAzure();
  await uploadFile(baseOptions({ file: makeFile(500, ""), fetchImpl: azure.fetchImpl }));
  assert.equal(azure.calls.at(-1).headers["x-ms-blob-content-type"], "video/mp4");
});

test("runs at most maxParallel uploads at once", async () => {
  const azure = fakeAzure(() => 201, 10);
  await uploadFile(baseOptions({ file: makeFile(10_000), maxParallel: 3, fetchImpl: azure.fetchImpl }));
  assert.equal(azure.stats.maxInflight, 3);
});

test("resume: blocks already on the server are skipped but stay in the commit list", async () => {
  const azure = fakeAzure();
  const result = await uploadFile(
    baseOptions({ getUploadedBlockIds: async () => [blockId(0), blockId(2)], fetchImpl: azure.fetchImpl }),
  );
  assert.deepEqual(result, { blockCount: 3, uploadedBlocks: 1 });
  const puts = azure.calls.filter((c) => query(c).get("comp") === "block");
  assert.deepEqual(puts.map((c) => query(c).get("blockid")), [blockId(1)]);
  assert.equal(azure.calls.at(-1).body, blockListXml([0, 1, 2].map(blockId)));
});

test("everything already uploaded: only the commit is sent", async () => {
  const azure = fakeAzure();
  await uploadFile(
    baseOptions({ getUploadedBlockIds: async () => [0, 1, 2].map(blockId), fetchImpl: azure.fetchImpl }),
  );
  assert.equal(azure.calls.length, 1);
  assert.equal(query(azure.calls[0]).get("comp"), "blocklist");
});

test("retries 5xx with exponential backoff 1s,2s then succeeds", async () => {
  const delays = [];
  let failures = 2;
  const azure = fakeAzure((call) => (query(call).get("blockid") === blockId(0) && failures-- > 0 ? 503 : 201));
  await uploadFile(
    baseOptions({
      file: makeFile(500),
      fetchImpl: azure.fetchImpl,
      sleep: async (ms) => void delays.push(ms),
    }),
  );
  assert.deepEqual(delays, [1000, 2000]);
});

test("network errors are retried too; gives up after 5 retries with delays 1,2,4,8,16 s", async () => {
  const delays = [];
  const azure = fakeAzure(() => new TypeError("network down"));
  await assert.rejects(
    uploadFile(
      baseOptions({
        file: makeFile(500),
        fetchImpl: azure.fetchImpl,
        sleep: async (ms) => void delays.push(ms),
      }),
    ),
    (error) => error instanceof TypeError && error.message === "network down",
  );
  assert.deepEqual(delays, [1000, 2000, 4000, 8000, 16000]);
  assert.equal(azure.calls.length, 6);
  assert.ok(azure.calls.every((c) => query(c).get("comp") === "block")); // never committed
});

test("HTTP 403 refreshes the SAS URL once (even with parallel blocks) and continues with the new URL", async () => {
  let refreshes = 0;
  const azure = fakeAzure((call) => (call.url.startsWith(URL0) ? 403 : 201));
  await uploadFile(
    baseOptions({
      file: makeFile(4000),
      fetchImpl: azure.fetchImpl,
      refreshUploadUrl: async () => {
        refreshes += 1;
        return URL0.replace("one", "two");
      },
    }),
  );
  assert.equal(refreshes, 1);
  const fresh = azure.calls.filter((c) => c.url.includes("sas=two"));
  assert.ok(fresh.length >= 5); // 4 blocks + commit
  assert.equal(azure.calls.at(-1).url.startsWith(URL0.replace("one", "two") + "&comp=blocklist"), true);
});

test("a failing URL refresh is fatal", async () => {
  const azure = fakeAzure(() => 403);
  await assert.rejects(
    uploadFile(
      baseOptions({
        file: makeFile(500),
        fetchImpl: azure.fetchImpl,
        refreshUploadUrl: async () => {
          throw new Error("resume failed");
        },
      }),
    ),
    (error) => error instanceof UploadFailedError && error.fatal && error.status === 403,
  );
});

test("non-retryable status (400) fails immediately without retries and stops other workers", async () => {
  const delays = [];
  const azure = fakeAzure(() => 400, 5);
  await assert.rejects(
    uploadFile(
      baseOptions({
        file: makeFile(10_000),
        fetchImpl: azure.fetchImpl,
        sleep: async (ms) => void delays.push(ms),
      }),
    ),
    (error) => error instanceof UploadFailedError && error.status === 400,
  );
  assert.deepEqual(delays, []);
  assert.ok(azure.calls.length <= 4); // only the first wave started; nothing committed
  assert.ok(azure.calls.every((c) => query(c).get("comp") === "block"));
});

test("aborting stops the upload, throws UploadAbortedError and never commits", async () => {
  const azure = fakeAzure(() => 201, 50);
  const controller = new AbortController();
  const promise = uploadFile(baseOptions({ file: makeFile(10_000), fetchImpl: azure.fetchImpl, signal: controller.signal }));
  setTimeout(() => controller.abort(), 10);
  await assert.rejects(promise, (error) => error instanceof UploadAbortedError);
  assert.ok(azure.calls.every((c) => query(c).get("comp") === "block"));
});

test("an already aborted signal never sends anything", async () => {
  const azure = fakeAzure();
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(
    uploadFile(baseOptions({ fetchImpl: azure.fetchImpl, signal: controller.signal })),
    (error) => error instanceof UploadAbortedError,
  );
  assert.equal(azure.calls.length, 0);
});

test("empty files are rejected", async () => {
  await assert.rejects(uploadFile(baseOptions({ file: makeFile(0) })), UploadFailedError);
});

test("progress is throttled to 5 updates/s, monotonic, and ends at 100%", async () => {
  let clock = 0;
  const seen = [];
  const azure = fakeAzure(() => 201, 0);
  await uploadFile(
    baseOptions({
      file: makeFile(20_000),
      maxParallel: 1,
      fetchImpl: async (url, init) => {
        clock += 10; // each request "takes" 10 ms of fake time
        return azure.fetchImpl(url, init);
      },
      now: () => clock,
      onProgress: (snapshot) => seen.push(snapshot),
    }),
  );
  // 20 blocks * 10 ms + commit = ~210 ms of fake time: initial emit, one throttled tick, the final one.
  assert.ok(seen.length >= 2 && seen.length <= 4, `got ${seen.length} updates`);
  assert.equal(seen.at(-1).percent, 100);
  const percents = seen.map((s) => s.percent);
  assert.deepEqual(percents, [...percents].sort((a, b) => a - b));
});

test("ProgressTracker: percent, speed and ETA from completed blocks", () => {
  let now = 0;
  const tracker = new ProgressTracker(10_000, () => now);
  assert.equal(tracker.snapshot().percent, 0);
  assert.equal(tracker.snapshot().etaSec, null);
  tracker.blockStarted(0, 1000);
  now = 1000;
  tracker.blockDone(0, 1000);
  tracker.blockStarted(1, 1000);
  now = 2000;
  tracker.blockDone(1, 1000);
  const snap = tracker.snapshot();
  assert.equal(snap.loadedBytes, 2000);
  assert.equal(snap.percent, 20);
  assert.equal(snap.speedBps, 1000);
  assert.equal(snap.etaSec, 8);
});

test("ProgressTracker: in-flight estimate is capped at 90% of the block and preloaded bytes count", () => {
  let now = 0;
  const tracker = new ProgressTracker(10_000, () => now);
  tracker.addPreloaded(4000);
  tracker.blockStarted(0, 1000);
  now = 1000;
  tracker.blockDone(0, 1000);
  tracker.blockStarted(1, 1000);
  now = 60_000; // in flight for a very long time
  const snap = tracker.snapshot();
  assert.ok(snap.loadedBytes <= 4000 + 1000 + 900);
  assert.ok(snap.percent < 100);
});
