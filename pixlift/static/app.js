// PixLift 前端 — 原生 JS，无构建
// 功能：单/批处理、模式切换（倍率/长边）、JPG/WebP quality、预设按钮、对比滑块

const $ = (id) => document.getElementById(id);
const dropZone = $("drop-zone");
const fileInput = $("file-input");
const queueSection = $("queue");
const queueList = $("queue-list");
const queueCount = $("queue-count");
const configSection = $("config");
const fileSummary = $("file-summary");
const modelSel = $("model");
const scaleSel = $("scale");
const longEdgeSel = $("long-edge");
const formatSel = $("format");
const qualityInput = $("quality");
const qualityVal = $("quality-val");
const qualityRow = $("row-quality");
const submitBtn = $("submit");
const cancelBtn = $("cancel");
const progressSection = $("progress");
const barFill = $("bar-fill");
const progressText = $("progress-text");
const progressDetail = $("progress-detail");
const errorSection = $("error");
const resultSection = $("result");
const imgBefore = $("img-before");
const imgAfter = $("img-after");
const layerAfter = document.querySelector(".layer-after");
const divider = $("divider");
const downloadLink = $("download");
const resetBtn = $("reset");
const resultMeta = $("result-meta");
const presetButtons = document.querySelectorAll(".preset");
const modeButtons = document.querySelectorAll(".mode-btn");
const rowScale = $("row-scale");
const rowLongEdge = $("row-long-edge");

const SYNC_THRESHOLD = 2 * 1024 * 1024; // 2 MB

// 阶段名 → 中文标签
function phaseToLabel(phase) {
  if (!phase) return "";
  return ({
    load_model: "加载模型",
    preprocess: "预处理",
    infer: "AI 推理",
    save: "保存文件",
    done: "完成",
  })[phase] || phase;
}

// URL 跟踪 + 手动 revoke（避免 blob 泄漏）
const blobUrls = { before: null, after: null };
function setBlobUrl(kind, url) {
  if (blobUrls[kind] && blobUrls[kind] !== url) {
    URL.revokeObjectURL(blobUrls[kind]);
  }
  blobUrls[kind] = url;
}
function clearBlobUrls() {
  if (blobUrls.before) { URL.revokeObjectURL(blobUrls.before); blobUrls.before = null; }
  if (blobUrls.after) { URL.revokeObjectURL(blobUrls.after); blobUrls.after = null; }
}
let currentFiles = [];      // 待处理文件数组
let currentMode = "scale";  // "scale" | "long_edge"
let pollTimer = null;
let abortController = null;
let activeJobId = null;
let activeBatchId = null;

// === Presets ===

const PRESET_CONFIG = {
  photo: { model: "realesrgan-x4plus", mode: "scale", scale: "4", long_edge: "2048", format: "png", quality: "90" },
  anime: { model: "realesrgan-x4plus-anime", mode: "scale", scale: "4", long_edge: "2048", format: "png", quality: "90" },
  web: { model: "realesrgan-x4plus", mode: "long_edge", scale: "2", long_edge: "2048", format: "jpg", quality: "85" },
};

function applyPreset(name) {
  const cfg = PRESET_CONFIG[name];
  if (!cfg) return;
  modelSel.value = cfg.model;
  formatSel.value = cfg.format;
  qualityInput.value = cfg.quality;
  qualityVal.textContent = cfg.quality;
  if (cfg.mode === "long_edge") {
    setMode("long_edge");
    longEdgeSel.value = cfg.long_edge;
  } else {
    setMode("scale");
    scaleSel.value = cfg.scale;
  }
  updateQualityVisibility();
  presetButtons.forEach(b => b.classList.toggle("active", b.dataset.preset === name));
}

presetButtons.forEach(b => {
  b.addEventListener("click", () => applyPreset(b.dataset.preset));
});

// === Mode toggle ===

function setMode(mode) {
  currentMode = mode;
  rowScale.hidden = mode !== "scale";
  rowLongEdge.hidden = mode !== "long_edge";
  modeButtons.forEach(b => b.classList.toggle("active", b.dataset.mode === mode));
}

modeButtons.forEach(b => {
  b.addEventListener("click", () => {
    setMode(b.dataset.mode);
    presetButtons.forEach(x => x.classList.remove("active")); // 自定义模式取消预设高亮
  });
});

// === Quality 可见性 ===

function updateQualityVisibility() {
  const show = formatSel.value !== "png";
  qualityRow.hidden = !show;
}
formatSel.addEventListener("change", updateQualityVisibility);
qualityInput.addEventListener("input", () => {
  qualityVal.textContent = qualityInput.value;
});

// === 文件处理 ===

function showError(msg) {
  errorSection.textContent = msg;
  errorSection.title = String(msg);
  errorSection.hidden = false;
  // 调试：把错误也打到 console，方便查看完整堆栈
  console.error("[PixLift]", msg);
}
function hideError() {
  errorSection.hidden = true;
  errorSection.textContent = "";
}
function fmtSize(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / (1024 * 1024)).toFixed(2) + " MB";
}

dropZone.addEventListener("click", () => fileInput.click());
dropZone.addEventListener("dragover", (e) => {
  e.preventDefault();
  dropZone.classList.add("dragover");
});
dropZone.addEventListener("dragleave", () => dropZone.classList.remove("dragover"));
dropZone.addEventListener("drop", (e) => {
  e.preventDefault();
  dropZone.classList.remove("dragover");
  const files = Array.from(e.dataTransfer.files);
  if (files.length) handleFiles(files);
});
fileInput.addEventListener("change", (e) => {
  const files = Array.from(e.target.files);
  if (files.length) handleFiles(files);
});

function handleFiles(files) {
  // 过滤非图片
  const images = files.filter(f => f.type.startsWith("image/"));
  if (images.length === 0) {
    showError("拖入的文件都不是图片");
    return;
  }
  if (images.length < files.length) {
    showError(`已忽略 ${files.length - images.length} 个非图片文件`);
  } else {
    hideError();
  }

  currentFiles = images;
  configSection.hidden = false;
  resultSection.hidden = true;
  submitBtn.disabled = false;

  // 文件摘要
  const total = images.reduce((s, f) => s + f.size, 0);
  fileSummary.textContent = `${images.length} 张 · 共 ${fmtSize(total)}`;

  // 队列显示（仅多文件时）
  if (images.length > 1) {
    queueSection.hidden = false;
    queueCount.textContent = images.length;
    queueList.innerHTML = "";
    images.forEach((f, i) => {
      const li = document.createElement("li");
      li.id = `qi-${i}`;
      li.innerHTML = `<span class="qi-name">${i + 1}. ${escapeHtml(f.name)}</span><span class="qi-status">等待</span>`;
      queueList.appendChild(li);
    });
  } else {
    queueSection.hidden = true;
    // 单张：显示缩略图
    setBlobUrl("before", URL.createObjectURL(images[0]));
    imgBefore.src = blobUrls.before;
    imgAfter.src = "";
    setSplitPct(50);
  }
}

function escapeHtml(s) {
  return s.replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// === 提交 ===

submitBtn.addEventListener("click", async () => {
  if (currentFiles.length === 0) return;
  hideError();
  submitBtn.disabled = true;
  cancelBtn.hidden = false;
  progressSection.hidden = false;
  barFill.style.width = "0%";
  barFill.classList.remove("indeterminate");
  const isSingle = currentFiles.length === 1;
  const isSyncSingle = isSingle && currentFiles[0].size < SYNC_THRESHOLD;
  const willBeSync = isSyncSingle;
  // 同步路径（小图）没有 job_id 可轮询，用 indeterminate 动画表示在处理
  if (willBeSync) {
    barFill.classList.add("indeterminate");
    progressText.textContent = "处理中…";
  } else {
    progressText.textContent = "上传中…";
  }
  progressDetail.textContent = "";

  abortController = new AbortController();
  const formData = new FormData();
  // 单图走 /api/upscale（字段名 "image"），多图走 /api/upscale/batch（字段名 "images"）
  const imageField = isSingle ? "image" : "images";
  currentFiles.forEach(f => formData.append(imageField, f));
  formData.append("model", modelSel.value);
  formData.append("format", formatSel.value);
  if (currentMode === "long_edge") {
    formData.append("long_edge", longEdgeSel.value);
  } else {
    formData.append("scale", scaleSel.value);
  }
  if (formatSel.value !== "png") {
    formData.append("quality", qualityInput.value);
  }

  try {
    const url = isSingle ? "/api/upscale" : "/api/upscale/batch";
    // 单张 < 2MB：走 /api/upscale 同步路径；否则单张走 /api/upscale 异步；多张走 batch
    const resp = await fetch(url, {
      method: "POST",
      body: formData,
      signal: abortController.signal,
    });

    if (!resp.ok) {
      const err = await safeJson(resp);
      throw new Error(formatApiError(err, resp.status));
    }

    // 单张同步：直接 blob
    if (isSyncSingle) {
      const blob = await resp.blob();
      const jobId = resp.headers.get("X-Job-Id") || "";
      const w = parseInt(resp.headers.get("X-Output-Width") || "0", 10);
      const h = parseInt(resp.headers.get("X-Output-Height") || "0", 10);
      barFill.classList.remove("indeterminate");
      barFill.style.width = "100%";
      progressText.textContent = "完成 · 100%";
      showResult(blob, w, h, jobId);
      return;
    }

    // 异步：job_id 或 batch_id
    const body = await resp.json();
    if (body.batch_id) {
      activeBatchId = body.batch_id;
      pollBatch();
    } else if (body.job_id) {
      activeJobId = body.job_id;
      pollProgress();
    }
  } catch (e) {
    if (e.name !== "AbortError") {
      const m = (e && typeof e.message === "string") ? e.message : String(e);
      showError("失败：" + m);
      console.error("[PixLift submit]", e);
      resetProgress();
    }
  }
});

cancelBtn.addEventListener("click", () => {
  if (abortController) abortController.abort();
  resetProgress();
});

function resetProgress() {
  progressSection.hidden = true;
  submitBtn.disabled = false;
  cancelBtn.hidden = true;
  if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
}

async function safeJson(resp) {
  try {
    return await resp.json();
  } catch (e) {
    // 响应不是 JSON（可能是 binary blob）。用 content-type 区分
    const ct = resp.headers.get("content-type") || "";
    let body = "";
    try { body = await resp.text(); } catch {}
    return { detail: `响应解析失败: ${ct} (${body.slice(0, 100)})` };
  }
}

// 把 FastAPI / 后端的错误对象转成可读字符串
function formatApiError(err, status) {
  // 兜底：err 本身不是对象
  if (err == null) return `HTTP ${status}`;
  if (typeof err === "string") return err;
  // 顶层 error 字段
  if (typeof err.error === "string" && err.error) return err.error;
  // detail：可能是 string | string[] | {msg, loc}[] | object
  const d = err.detail;
  if (typeof d === "string" && d) return d;
  if (Array.isArray(d) && d.length) {
    const msg = d.map(x => {
      if (typeof x === "string") return x;
      if (x && typeof x === "object") {
        const loc = Array.isArray(x.loc) ? x.loc.join(".") : (x.loc || "");
        return `${loc ? loc + ": " : ""}${x.msg || JSON.stringify(x)}`;
      }
      return String(x);
    }).join("; ");
    return msg || `HTTP ${status}`;
  }
  if (d && typeof d === "object") {
    try {
      const s = JSON.stringify(d);
      if (s && s !== "{}" && s !== "[]") return s;
    } catch {}
    return `HTTP ${status}`;
  }
  if (d) return String(d);
  // 最后兜底：把整个 err 序列化
  try {
    const s = JSON.stringify(err);
    return s && s !== "{}" ? s : `HTTP ${status}`;
  } catch {
    return `HTTP ${status}`;
  }
}

// === 单张轮询 ===

function pollProgress() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    if (!activeJobId) return;
    try {
      const r = await fetch(`/api/jobs/${activeJobId}`);
      if (!r.ok) {
        // 404 = job 已不存在（重启 / LRU 淘汰 / batch 被收）
        // 永久停掉轮询，提示用户
        clearInterval(pollTimer);
        pollTimer = null;
        activeJobId = null;
        const msg = r.status === 404 ? "任务不存在（可能已被清理）" : `轮询失败：HTTP ${r.status}`;
        showError(msg);
        resetProgress();
        return;
      }
      const j = await r.json();
      barFill.style.width = j.progress + "%";
      const phaseLabel = phaseToLabel(j.phase);
      progressText.textContent = phaseLabel ? `${phaseLabel} · ${j.progress}%` : `${j.status} · ${j.progress}%`;
      progressDetail.textContent = `任务 ${activeJobId}`;

      if (j.status === "done") {
        clearInterval(pollTimer);
        pollTimer = null;
        downloadResult();
      } else if (j.status === "failed") {
        clearInterval(pollTimer);
        pollTimer = null;
        showError("处理失败：" + (j.error || "unknown"));
        resetProgress();
      }
    } catch (e) {
      console.warn("poll failed:", e);
    }
  }, 500);
}

async function downloadResult() {
  try {
    const r = await fetch(`/api/jobs/${activeJobId}/download`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const blob = await r.blob();
    const fname = filenameFromResponse(r) || `pixlift.${formatSel.value}`;
    const w = parseInt(r.headers.get("X-Output-Width") || "0", 10);
    const h = parseInt(r.headers.get("X-Output-Height") || "0", 10);
    showResult(blob, w, h, activeJobId, fname);
  } catch (e) {
    showError("下载失败：" + e.message);
    resetProgress();
  }
}

// === 批量轮询 ===

function pollBatch() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    if (!activeBatchId) return;
    try {
      const r = await fetch(`/api/batches/${activeBatchId}`);
      if (!r.ok) {
        clearInterval(pollTimer);
        pollTimer = null;
        activeBatchId = null;
        const msg = r.status === 404 ? "批量任务不存在（可能已被清理）" : `轮询失败：HTTP ${r.status}`;
        showError(msg);
        resetProgress();
        return;
      }
      const b = await r.json();
      const done = b.done + b.failed;
      const pct = Math.round((done / b.total) * 100);
      barFill.style.width = pct + "%";
      progressText.textContent = `批量处理 · ${done}/${b.total}`;
      progressDetail.textContent = `已完成 ${b.done} · 失败 ${b.failed}`;

      // 更新队列状态
      b.jobs.forEach((j, i) => {
        const li = document.getElementById(`qi-${i}`);
        if (!li) return;
        li.className = j.status;
        const st = li.querySelector(".qi-status");
        if (st) st.textContent = j.status === "done" ? "✓" : (j.status === "failed" ? "✗" : "处理中");
      });

      if (done === b.total) {
        clearInterval(pollTimer);
        pollTimer = null;
        if (b.failed === b.total) {
          showError("全部失败");
          resetProgress();
          return;
        }
        downloadBatchZip(b);
      }
    } catch (e) {
      console.warn("batch poll failed:", e);
    }
  }, 800);
}

async function downloadBatchZip(batch) {
  try {
    progressText.textContent = "打包 ZIP…";
    const r = await fetch(`/api/batches/${activeBatchId}/zip`);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const blob = await r.blob();
    const fname = filenameFromResponse(r) || `pixlift_batch_${activeBatchId.slice(-6)}.zip`;
    const url = URL.createObjectURL(blob);
    // 触发下载
    const a = document.createElement("a");
    a.href = url;
    a.download = fname;
    document.body.appendChild(a);
    a.click();
    a.remove();
    // 浏览器接住下载是异步的；用 requestIdleCallback 兜底 revoke，
    // 避免 5s 兜底期间用户立刻点 "处理下一张" 导致重复 revoke 报错
    if (window.requestIdleCallback) {
      requestIdleCallback(() => URL.revokeObjectURL(url), { timeout: 1500 });
    } else {
      setTimeout(() => URL.revokeObjectURL(url), 100);
    }

    progressText.textContent = `完成 · ${batch.done}/${batch.total} 张已处理`;
    progressDetail.textContent = "ZIP 已下载";
    submitBtn.disabled = false;
    cancelBtn.hidden = true;
    pollTimer = null;
  } catch (e) {
    showError("打包失败：" + e.message);
    resetProgress();
  }
}

function filenameFromResponse(r) {
  const cd = r.headers.get("Content-Disposition") || "";
  const m = cd.match(/filename="?([^"]+)"?/);
  return m ? m[1] : null;
}

// === 单张结果展示 ===

function showResult(blob, w, h, jobId, suggestedName) {
  setBlobUrl("after", URL.createObjectURL(blob));
  const url = blobUrls.after;
  imgAfter.src = url;
  const onAfterLoad = () => {
    initCompareViewport();
    imgAfter.removeEventListener("load", onAfterLoad);
  };
  if (imgAfter.complete && imgAfter.naturalWidth) onAfterLoad();
  else imgAfter.addEventListener("load", onAfterLoad);
  setSplitPct(50);
  resetCompareMode();
  const inputDims = currentFiles[0] ? `${imgBefore.naturalWidth}×${imgBefore.naturalHeight}` : "?";
  const outputDims = w && h ? `${w}×${h}` : `${imgAfter.naturalWidth}×${imgAfter.naturalHeight}`;
  resultMeta.textContent = `原图 ${inputDims} → 输出 ${outputDims} · 任务 ${jobId || "-"}`;
  downloadLink.href = url;
  downloadLink.download = suggestedName || `pixlift.${formatSel.value}`;
  resultSection.hidden = false;
  resetProgress();
  resultSection.scrollIntoView({ behavior: "smooth", block: "start" });
}

// === 对比增强 ===

const compareViewport = $("compare-viewport");
const compareEl = $("compare");
const layerBefore = document.querySelector(".layer-before");
const loupe = $("loupe");
const zoomLabel = $("zoom-label");
const quickBtns = document.querySelectorAll(".btn-quick");
const zoomBtns = document.querySelectorAll(".btn-zoom");
const flipBtns = document.querySelectorAll(".btn-flip");

let compareState = {
  splitPct: 50,          // 0-100
  mode: "split",         // "split" | "before" | "after"
  flipped: false,
  zoom: 1,
  panX: 0,
  panY: 0,
  naturalW: 0,
  naturalH: 0,
};

function setSplitPct(pct) {
  compareState.splitPct = Math.max(0, Math.min(100, pct));
  layerAfter.style.width = compareState.splitPct + "%";
  divider.style.left = compareState.splitPct + "%";
  divider.setAttribute("aria-valuenow", String(Math.round(compareState.splitPct)));
}

function setCompareMode(mode) {
  compareState.mode = mode;
  compareEl.classList.toggle("full-before", mode === "before");
  compareEl.classList.toggle("full-after", mode === "after");
  quickBtns.forEach(b => b.classList.toggle("active", b.dataset.quick === mode));
  if (mode !== "split") {
    loupe.classList.remove("visible");
    loupe.hidden = true;
  }
}

function resetCompareMode() {
  compareState.flipped = false;
  compareEl.classList.remove("flipped");
  flipBtns.forEach(b => b.classList.toggle("active", b.dataset.flip === "0"));
  setCompareMode("split");
}

function initCompareViewport() {
  // 计算适合 viewport 的初始 zoom
  const vw = compareViewport.clientWidth;
  const vh = compareViewport.clientHeight;
  const natW = imgAfter.naturalWidth || imgBefore.naturalWidth || 1;
  const natH = imgAfter.naturalHeight || imgBefore.naturalHeight || 1;
  compareState.naturalW = natW;
  compareState.naturalH = natH;
  // compare 容器大小 = 图片实际像素
  compareEl.style.width = natW + "px";
  compareEl.style.height = natH + "px";
  // 缩放比例按长边
  const fit = Math.min(vw / natW, vh / natH) * 0.95;
  compareState.zoom = Math.max(0.05, fit);
  compareState.panX = 0;
  compareState.panY = 0;
  applyCompareTransform();
  centerCompare();
  // 监听 viewport 尺寸变化（窗口 resize / 旋转 / DevTools 面板切换）
  if (compareState._resizeObs) compareState._resizeObs.disconnect();
  compareState._resizeObs = new ResizeObserver(() => {
    if (compareState.naturalW > 1) {
      const newFit = Math.min(
        compareViewport.clientWidth / compareState.naturalW,
        compareViewport.clientHeight / compareState.naturalH
      ) * 0.95;
      compareState.zoom = Math.max(0.05, newFit);
      clampPan();
      applyCompareTransform();
    }
  });
  compareState._resizeObs.observe(compareViewport);
}

function applyCompareTransform() {
  const t = `translate(${-compareState.panX}px, ${-compareState.panY}px) scale(${compareState.zoom})`;
  compareEl.style.transform = t;
  zoomLabel.textContent = Math.round(compareState.zoom * 100) + "%";
}

function centerCompare() {
  const vw = compareViewport.clientWidth;
  const vh = compareViewport.clientHeight;
  const natW = compareState.naturalW;
  const natH = compareState.naturalH;
  const w = natW * compareState.zoom;
  const h = natH * compareState.zoom;
  compareState.panX = Math.max(0, (w - vw) / 2);
  compareState.panY = Math.max(0, (h - vh) / 2);
  applyCompareTransform();
}

function clampPan() {
  // pan 必须 ≥ 0，且不能超过 (nat * zoom - viewport)
  const vw = compareViewport.clientWidth;
  const vh = compareViewport.clientHeight;
  const maxX = Math.max(0, compareState.naturalW * compareState.zoom - vw);
  const maxY = Math.max(0, compareState.naturalH * compareState.zoom - vh);
  compareState.panX = Math.max(0, Math.min(maxX, compareState.panX));
  compareState.panY = Math.max(0, Math.min(maxY, compareState.panY));
}

function setZoom(newZoom, anchorX, anchorY) {
  const oldZoom = compareState.zoom;
  newZoom = Math.max(0.05, Math.min(8, newZoom));
  if (anchorX !== undefined) {
    // 让 anchor 点在缩放前后保持屏幕位置不变
    const vw = compareViewport.clientWidth;
    const vh = compareViewport.clientHeight;
    const ax = anchorX - vw / 2 + compareState.panX;
    const ay = anchorY - vh / 2 + compareState.panY;
    compareState.panX = ax * (newZoom / oldZoom) - (anchorX - vw / 2);
    compareState.panY = ay * (newZoom / oldZoom) - (anchorY - vh / 2);
  } else {
    centerCompare();
  }
  compareState.zoom = newZoom;
  clampPan();
  applyCompareTransform();
}

quickBtns.forEach(b => b.addEventListener("click", () => setCompareMode(b.dataset.quick)));
zoomBtns.forEach(b => b.addEventListener("click", () => {
  const op = b.dataset.zoom;
  if (op === "in") setZoom(compareState.zoom * 1.25);
  else if (op === "out") setZoom(compareState.zoom / 1.25);
  else if (op === "fit") { compareState.zoom = 1; centerCompare(); applyCompareTransform(); }
  else if (op === "100") { compareState.zoom = Math.min(1, Math.max(0.1, Math.min(compareViewport.clientWidth / compareState.naturalW, compareViewport.clientHeight / compareState.naturalH))); centerCompare(); applyCompareTransform(); }
}));
flipBtns.forEach(b => b.addEventListener("click", () => {
  compareState.flipped = b.dataset.flip === "1";
  compareEl.classList.toggle("flipped", compareState.flipped);
  flipBtns.forEach(x => x.classList.toggle("active", x.dataset.flip === b.dataset.flip));
}));

// === 拖拽分隔线（鼠标 / 触屏） ===

let dragging = false;
let dragMode = "divider"; // "divider" | "pan"

divider.addEventListener("mousedown", (e) => { e.preventDefault(); dragging = true; dragMode = "divider"; });
divider.addEventListener("touchstart", (e) => { e.preventDefault(); dragging = true; dragMode = "divider"; }, { passive: false });
divider.addEventListener("keydown", (e) => {
  const step = e.shiftKey ? 10 : 2;
  if (e.key === "ArrowLeft") { setSplitPct(compareState.splitPct - step); e.preventDefault(); }
  else if (e.key === "ArrowRight") { setSplitPct(compareState.splitPct + step); e.preventDefault(); }
  else if (e.key === "Home") { setSplitPct(0); e.preventDefault(); }
  else if (e.key === "End") { setSplitPct(100); e.preventDefault(); }
  else if (e.key === "Enter" || e.key === " ") { setCompareMode(compareState.mode === "split" ? "after" : "split"); e.preventDefault(); }
});

// 在 viewport 内任意位置点击 → 移动分隔线
compareViewport.addEventListener("mousedown", (e) => {
  // Alt+鼠标 → 启用放大镜；普通点击 → 移动分隔线；中键 / 直接拖 → 平移
  if (e.button === 1 || e.button === 2) {
    dragging = true; dragMode = "pan"; lastPanX = e.clientX; lastPanY = e.clientY;
    return;
  }
  if (e.altKey) return; // 放大镜模式另外处理
  // 不是从 divider 来的 → 设分隔线
  if (e.target === divider || divider.contains(e.target)) return;
  dragging = true; dragMode = "divider";
  updateSplitFromEvent(e);
});
compareViewport.addEventListener("touchstart", (e) => {
  if (e.touches.length === 1) {
    dragging = true; dragMode = "divider";
    updateSplitFromEvent(e.touches[0]);
  }
}, { passive: false });
compareViewport.addEventListener("contextmenu", (e) => e.preventDefault());

let lastPanX = 0, lastPanY = 0;
window.addEventListener("mousemove", onDrag);
window.addEventListener("touchmove", onDrag, { passive: false });
window.addEventListener("mouseup", endDrag);
window.addEventListener("touchend", endDrag);

function updateSplitFromEvent(e) {
  const rect = compareViewport.getBoundingClientRect();
  const x = e.clientX - rect.left;
  const pct = Math.max(0, Math.min(100, (x / rect.width) * 100));
  setSplitPct(pct);
  if (compareState.mode !== "split") setCompareMode("split");
}

function onDrag(e) {
  if (!dragging) return;
  if (dragMode === "divider") {
    e.preventDefault();
    const pt = e.touches ? e.touches[0] : e;
    const rect = compareViewport.getBoundingClientRect();
    const x = pt.clientX - rect.left;
    const pct = Math.max(0, Math.min(100, (x / rect.width) * 100));
    setSplitPct(pct);
  } else if (dragMode === "pan") {
    // 仅当确实在拖动时 preventDefault；避免阻止页面缩放手势
    if (e.cancelable) e.preventDefault();
    const dx = e.clientX - lastPanX;
    const dy = e.clientY - lastPanY;
    lastPanX = e.clientX;
    lastPanY = e.clientY;
    compareState.panX -= dx;
    compareState.panY -= dy;
    clampPan();
    applyCompareTransform();
  }
}
function endDrag() {
  dragging = false;
  dragMode = "divider";
  compareViewport.classList.remove("dragging");
}

// === 滚轮缩放（以光标为锚点） ===
compareViewport.addEventListener("wheel", (e) => {
  e.preventDefault();
  const rect = compareViewport.getBoundingClientRect();
  const ax = e.clientX - rect.left;
  const ay = e.clientY - rect.top;
  const factor = e.deltaY < 0 ? 1.15 : 1 / 1.15;
  setZoom(compareState.zoom * factor, ax, ay);
}, { passive: false });

// === Alt+悬停 → 放大镜（rAF 节流） ===
let _loupePending = false;
let _loupeLast = null;
compareViewport.addEventListener("mousemove", (e) => {
  if (compareState.mode !== "split") return;
  // 没按住 alt → 立即关（不进 rAF，避免延迟隐藏造成错觉）
  if (!e.altKey) {
    if (loupe.classList.contains("visible")) {
      loupe.classList.remove("visible");
      loupe.hidden = true;
      compareViewport.classList.remove("alt-mode");
    }
    return;
  }
  _loupeLast = e;
  if (_loupePending) return;
  _loupePending = true;
  requestAnimationFrame(() => {
    _loupePending = false;
    const ev = _loupeLast;
    if (!ev || !ev.altKey) return;
    compareViewport.classList.add("alt-mode");
    loupe.hidden = false;
    loupe.classList.add("visible");
    const rect = compareViewport.getBoundingClientRect();
    const x = ev.clientX - rect.left;
    const y = ev.clientY - rect.top;
    const natW = compareState.naturalW;
    const natH = compareState.naturalH;
    const zoom = compareState.zoom;
    const cx = (x - (rect.width - natW * zoom) / 2 + compareState.panX) / zoom;
    const cy = (y - (rect.height - natH * zoom) / 2 + compareState.panY) / zoom;
    const pct = compareState.splitPct;
    const showBefore = (cx / natW) * 100 < pct;
    const srcImg = showBefore ? imgBefore.src : imgAfter.src;
    if (!srcImg) return;
    const magScale = 3;
    const magSize = 180;
    const bgX = -(cx * magScale - magSize / 2);
    const bgY = -(cy * magScale - magSize / 2);
    // 安全地写 backgroundImage（避免引号注入）
    loupe.style.setProperty(
      "background-image",
      `url("${CSS.escape ? srcImg : srcImg.replace(/"/g, '%22')}")`
    );
    loupe.style.backgroundSize = `${natW * magScale}px ${natH * magScale}px`;
    loupe.style.backgroundPosition = `${bgX}px ${bgY}px`;
    loupe.style.left = x + "px";
    loupe.style.top = y + "px";
  });
});

// === 双击切换对比方向 ===
compareEl.addEventListener("dblclick", (e) => {
  compareState.flipped = !compareState.flipped;
  compareEl.classList.toggle("flipped", compareState.flipped);
  flipBtns.forEach(b => b.classList.toggle("active", (b.dataset.flip === "1") === compareState.flipped));
  e.preventDefault();
});

// === 重置 ===

resetBtn.addEventListener("click", () => {
  currentFiles = [];
  activeJobId = null;
  activeBatchId = null;
  fileInput.value = "";
  configSection.hidden = true;
  resultSection.hidden = true;
  progressSection.hidden = true;
  queueSection.hidden = true;
  hideError();
  presetButtons.forEach(b => b.classList.remove("active"));
  loupe.classList.remove("visible");
  loupe.hidden = true;
  clearBlobUrls();
});

// === 初始化 ===

updateQualityVisibility();
setMode("scale");
qualityVal.textContent = qualityInput.value;

// 根据 /api/health 标记缺失的模型（避免选了之后 500）
fetch("/api/health").then(r => r.json()).then(j => {
  // 后端返回的 j.models 是逻辑模型名（如 realesrgan-x4plus-anime），
  // 不是带 .pth 后缀的文件名。直接比对 opt.value。
  const available = new Set(j.models || []);
  Array.from(modelSel.options).forEach(opt => {
    if (!available.has(opt.value)) {
      opt.disabled = true;
      const note = opt.textContent.includes("（缺）") ? "" : "（缺）";
      if (note) opt.textContent = opt.textContent + note;
    }
  });
  // 若当前选中的模型不可用，切到第一个可用的
  if (modelSel.selectedOptions[0]?.disabled) {
    const first = Array.from(modelSel.options).find(o => !o.disabled);
    if (first) modelSel.value = first.value;
  }
}).catch(() => {});