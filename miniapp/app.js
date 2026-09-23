import { ApiError, createApi } from "./api.js";
import { UploadAbortedError, uploadFile } from "./azure-upload.js";
import { FORMATS, STYLES, errorText, t } from "./i18n.js";

const ALLOWED_EXTENSIONS = [".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"];
const STORAGE_KEY = "montaj_upload"; // {upload_id, name, size, lastModified, committed}; never the SAS URL
const DEV_INIT_DATA_KEY = "__DEV_INIT_DATA__";
const $ = (id) => document.getElementById(id);

const tg = window.Telegram?.WebApp ?? {
  initData: "",
  ready() {},
  expand() {},
  close: () => window.close(),
};

const MAX_BROLL = 4;
const state = { me: null, abort: null, job: null, broll: [], format: "9:16", style: "dynamic_reels" };

// ---------- helpers ----------

function getInitData() {
  if (tg.initData) return tg.initData;
  const debug = new URLSearchParams(location.search).get("debug") === "1";
  if (debug && ["localhost", "127.0.0.1"].includes(location.hostname)) {
    try {
      return localStorage.getItem(DEV_INIT_DATA_KEY) || window[DEV_INIT_DATA_KEY] || "";
    } catch {
      return window[DEV_INIT_DATA_KEY] || "";
    }
  }
  return "";
}

const api = createApi(getInitData);

function loadSaved() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY));
  } catch {
    return null;
  }
}

function saveEntry(entry) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(entry));
  } catch {
    // storage unavailable: resume is a convenience only
  }
}

function clearSaved() {
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch {
    // ignore
  }
}

const clock = (seconds) => {
  const total = Math.max(0, Math.round(seconds));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
};

const extensionOf = (name) => {
  const dot = name.lastIndexOf(".");
  return dot < 0 ? "" : name.slice(dot).toLowerCase();
};

function show(name) {
  for (const section of document.querySelectorAll(".screen")) {
    section.hidden = section.id !== `screen-${name}`;
  }
}

function setClosingGuard(on) {
  if (on) {
    tg.enableClosingConfirmation?.();
    tg.disableVerticalSwipes?.();
  } else {
    tg.disableClosingConfirmation?.();
    tg.enableVerticalSwipes?.();
  }
}

// ---------- screens ----------

function showError(error, onRetry = boot) {
  const message = typeof error === "string" ? error : errorText(error);
  $("error-text").textContent = message;
  const noCredits = error?.code === "NO_CREDITS";
  $("error-tariffs").hidden = !noCredits;
  $("error-retry").hidden = onRetry === null;
  $("error-retry").onclick = () => onRetry?.();
  show("error");
}

function renderHome() {
  const me = state.me;
  $("home-balance").textContent = t.balance(me.balance_units);
  $("home-trial").hidden = !me.trial_available;
  $("home-trial").textContent = t.trialBadge;
  show("home");
}

function renderProgress(snapshot) {
  $("upload-fill").style.width = `${snapshot.percent}%`;
  $("upload-percent").textContent = `${snapshot.percent}%`;
  document.querySelector("#screen-uploading .bar").setAttribute("aria-valuenow", String(snapshot.percent));
  const parts = [t.speed((snapshot.speedBps / 1048576).toFixed(1))];
  if (snapshot.etaSec !== null) parts.push(t.remaining(clock(snapshot.etaSec)));
  $("upload-meta").textContent = parts.join(" · ");
}

function renderChoices(container, items, key) {
  container.replaceChildren();
  for (const item of items) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "choice";
    button.setAttribute("aria-pressed", String(state[key] === item.value));
    button.append(item.label);
    if (item.hint) {
      const hint = document.createElement("small");
      hint.textContent = item.hint;
      button.append(hint);
    }
    button.onclick = () => {
      state[key] = item.value;
      for (const other of container.children) other.setAttribute("aria-pressed", String(other === button));
    };
    container.append(button);
  }
}

function renderConfirm() {
  const job = state.job;
  $("confirm-duration").textContent = `${t.duration}: ${clock(job.duration_sec)}`;
  $("confirm-resolution").textContent = `${t.resolution}: ${job.width}×${job.height}`;
  $("confirm-price").textContent = job.is_trial ? t.trialFree : t.price(job.units_cost);
  $("confirm-balance").textContent = job.is_trial ? "" : t.balanceAfter(job.balance_after);
  $("confirm-balance").hidden = job.is_trial;
  const short = !job.is_trial && job.balance_after < 0;
  $("confirm-warning").hidden = !short;
  $("confirm-warning").textContent = t.insufficient;
  $("start-btn").hidden = short;
  $("tariffs-btn").hidden = !short;
  $("start-btn").disabled = false;
  renderChoices($("format-cards"), FORMATS, "format");
  renderChoices($("style-cards"), STYLES, "style");
  show("confirm");
}

// ---------- upload flow ----------

async function tryCompleteSaved(saved) {
  try {
    return await api.post(`/api/uploads/${saved.upload_id}/complete`);
  } catch {
    clearSaved();
    return null;
  }
}

async function obtainUpload(file) {
  const saved = loadSaved();
  const same = saved && saved.name === file.name && saved.size === file.size && saved.lastModified === file.lastModified;
  if (same) {
    try {
      const init = await api.post(`/api/uploads/${saved.upload_id}/resume`);
      return { id: saved.upload_id, init, resumed: true, committed: Boolean(saved.committed) };
    } catch (error) {
      if (!(error instanceof ApiError) || ![404, 409].includes(error.status)) throw error;
      clearSaved(); // stale upload: start a new one
    }
  }
  const init = await api.post("/api/uploads/init", {
    filename: file.name,
    size_bytes: file.size,
    content_type: file.type || "video/mp4",
  });
  saveEntry({ upload_id: init.upload_id, name: file.name, size: file.size, lastModified: file.lastModified });
  return { id: init.upload_id, init, resumed: false, committed: false };
}

async function startUpload(file) {
  if (!ALLOWED_EXTENSIONS.includes(extensionOf(file.name))) return showError(t.unsupportedFile, renderHome);
  if (file.size === 0) return showError(t.emptyFile, renderHome);

  $("upload-name").textContent = file.name;
  renderProgress({ percent: 0, speedBps: 0, etaSec: null });
  show("uploading");
  setClosingGuard(true);
  state.abort = new AbortController();
  try {
    const upload = await obtainUpload(file);
    let result = null;
    if (upload.committed) {
      show("verifying");
      result = await tryCompleteSaved({ upload_id: upload.id });
      if (!result) show("uploading");
    }
    if (!result) {
      const { init } = upload;
      const saved = { upload_id: upload.id, name: file.name, size: file.size, lastModified: file.lastModified };
      await uploadFile({
        file,
        uploadUrl: init.upload_url,
        blockSize: init.block_size,
        maxParallel: init.max_parallel,
        getUploadedBlockIds: async () => (await api.get(`/api/uploads/${upload.id}/blocks`)).uploaded_block_ids,
        refreshUploadUrl: async () => (await api.post(`/api/uploads/${upload.id}/resume`)).upload_url,
        onProgress: renderProgress,
        signal: state.abort.signal,
      });
      saveEntry({ ...saved, committed: true });
      show("verifying");
      result = await api.post(`/api/uploads/${upload.id}/complete`);
    }
    clearSaved();
    state.job = result;
    state.broll = [];
    renderBroll();
  } catch (error) {
    if (error instanceof UploadAbortedError) return renderHome();
    if (error instanceof ApiError && error.status >= 400 && error.code !== "BLOB_MISSING") clearSaved();
    showError(error instanceof ApiError ? error : t.genericError, renderHome);
  } finally {
    setClosingGuard(false);
    state.abort = null;
  }
}

// ---------- B-roll (P13/P15: optional extra cutaway clips) ----------

function renderBrollList() {
  const container = $("broll-list");
  container.replaceChildren();
  state.broll.forEach((item, i) => {
    const row = document.createElement("p");
    row.className = "hint";
    row.textContent = t.brollItem(i + 1, clock(item.duration_sec));
    container.append(row);
  });
  const atCap = state.broll.length >= MAX_BROLL;
  $("broll-add-btn").disabled = atCap;
  $("broll-add-btn").textContent = atCap ? t.brollCapReached : t.brollAdd;
}

function renderBroll() {
  $("broll-title").textContent = t.brollTitle;
  $("broll-hint").textContent = t.brollHint;
  $("broll-continue-btn").textContent = t.brollContinue;
  $("broll-status").hidden = true;
  renderBrollList();
  show("broll");
}

async function addBroll(file) {
  if (!ALLOWED_EXTENSIONS.includes(extensionOf(file.name))) return showError(t.unsupportedFile, renderBroll);
  if (file.size === 0) return showError(t.emptyFile, renderBroll);
  $("broll-add-btn").disabled = true;
  $("broll-continue-btn").disabled = true;
  $("broll-status").hidden = false;
  $("broll-status").textContent = t.brollUploading(0);
  try {
    const init = await api.post("/api/uploads/init", {
      filename: file.name,
      size_bytes: file.size,
      content_type: file.type || "video/mp4",
    });
    await uploadFile({
      file,
      uploadUrl: init.upload_url,
      blockSize: init.block_size,
      maxParallel: init.max_parallel,
      getUploadedBlockIds: async () =>
        (await api.get(`/api/uploads/${init.upload_id}/blocks`)).uploaded_block_ids,
      refreshUploadUrl: async () => (await api.post(`/api/uploads/${init.upload_id}/resume`)).upload_url,
      onProgress: (snapshot) => {
        $("broll-status").textContent = t.brollUploading(snapshot.percent);
      },
    });
    $("broll-status").textContent = t.verifying;
    const attached = await api.post(`/api/uploads/${init.upload_id}/attach`, { job_id: state.job.job_id });
    state.broll.push(attached);
    state.job.units_cost = attached.units_cost;
    if (!state.job.is_trial) state.job.balance_after = state.me.balance_units - attached.units_cost;
    $("broll-status").hidden = true;
    renderBrollList();
  } catch (error) {
    showError(error instanceof ApiError ? error : t.genericError, renderBroll);
  } finally {
    $("broll-continue-btn").disabled = false;
  }
}

async function confirmJob() {
  $("start-btn").disabled = true;
  try {
    await api.post(`/api/jobs/${state.job.job_id}/confirm`, {
      aspect: state.format,
      style_preset: state.style,
      brief: $("brief").value.trim(),
    });
    show("done");
    setTimeout(() => tg.close(), 1500);
  } catch (error) {
    showError(error, renderConfirm);
  }
}

// ---------- boot ----------

async function boot() {
  show("loading");
  if (!getInitData()) return showError(t.openViaBot, null);
  try {
    state.me = await api.get("/api/me");
    renderHome();
  } catch (error) {
    showError(error, boot);
  }
}

function applyStaticText() {
  $("loading-text").textContent = t.loading;
  $("pick-btn").textContent = t.pickVideo;
  $("upload-cancel").textContent = t.cancel;
  $("verifying-text").textContent = t.verifying;
  $("broll-title").textContent = t.brollTitle;
  $("broll-hint").textContent = t.brollHint;
  $("broll-add-btn").textContent = t.brollAdd;
  $("broll-continue-btn").textContent = t.brollContinue;
  $("format-title").textContent = t.formatTitle;
  $("style-title").textContent = t.styleTitle;
  $("brief-label").textContent = t.briefLabel;
  $("start-btn").textContent = t.start;
  $("tariffs-btn").textContent = t.tariffs;
  $("done-text").textContent = t.done;
  $("error-tariffs").textContent = t.tariffs;
  $("error-retry").textContent = t.retry;
  $("error-close").textContent = t.close;
  $("footer").textContent = t.footer;
}

function bindEvents() {
  $("pick-btn").onclick = () => $("file-input").click();
  $("file-input").onchange = (event) => {
    const [file] = event.target.files;
    event.target.value = "";
    if (file) startUpload(file);
  };
  $("upload-cancel").onclick = () => state.abort?.abort();
  $("broll-add-btn").onclick = () => $("broll-file-input").click();
  $("broll-file-input").onchange = (event) => {
    const [file] = event.target.files;
    event.target.value = "";
    if (file) addBroll(file);
  };
  $("broll-continue-btn").onclick = renderConfirm;
  $("start-btn").onclick = confirmJob;
  $("tariffs-btn").onclick = () => tg.close();
  $("error-tariffs").onclick = () => tg.close();
  $("error-close").onclick = () => tg.close();
}

tg.ready();
tg.expand?.();
applyStaticText();
bindEvents();
boot();
