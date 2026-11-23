"use strict";

const $ = (id) => document.getElementById(id);

const state = {
  config: null,
  file: null,
  jobId: null,
  timer: null,
  sampleFiles: new Map(),
};

const STATUS_TEXT = {
  queued: "排队中",
  converting: "复制并重建 GPT",
  verifying: "复验成品",
  done: "完成",
  failed: "失败",
};

function formatBytes(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  let value = Number(n);
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i += 1;
  }
  const digits = value >= 100 || i === 0 ? 0 : value >= 10 ? 1 : 2;
  return `${value.toFixed(digits)} ${units[i]}`;
}

function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else node.setAttribute(key, value);
  }
  for (const child of Array.isArray(children) ? children : [children]) {
    if (child === null || child === undefined) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function setBanner(kind, text, bullets = []) {
  const box = $("banner");
  box.className = `banner ${kind}`;
  box.replaceChildren(document.createTextNode(text));
  if (bullets.length) {
    const list = el("ul");
    for (const item of bullets) list.append(el("li", { text: item }));
    box.append(list);
  }
  box.hidden = false;
}

function hideBanner() {
  $("banner").hidden = true;
}

function setBusy(busy) {
  $("start-btn").disabled = busy || !state.file;
  $("start-btn").textContent = busy ? "转换中…" : "开始转换";
  for (const btn of document.querySelectorAll(".sample-card")) btn.disabled = busy;
  $("reset-btn").hidden = $("report-card").hidden;
}

function resetUI() {
  if (state.timer) clearInterval(state.timer);
  state.timer = null;
  state.jobId = null;
  $("report-card").hidden = true;
  $("progress-wrap").hidden = true;
  $("checks").hidden = true;
  $("partitions").hidden = true;
  $("download-wrap").hidden = true;
  $("warnings").hidden = true;
  $("copies").replaceChildren();
  $("meta").replaceChildren();
  hideBanner();
}

async function loadConfig() {
  try {
    const res = await fetch("/api/config");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    state.config = await res.json();
    $("file-hint").textContent = `支持 512 字节逻辑扇区的 RAW 镜像（服务端上限 ${formatBytes(
      state.config.max_upload_bytes
    )}）`;
  } catch (err) {
    $("file-hint").textContent = "无法读取服务端配置，仍可尝试上传。";
  }
}

function chooseFile(file) {
  state.file = file;
  const zone = $("dropzone");
  zone.classList.toggle("has-file", Boolean(file));
  $("file-hint").textContent = file
    ? `${file.name} · ${formatBytes(file.size)}`
    : "点击选择或把 RAW 镜像拖到这里";
  $("start-btn").disabled = !file;
}

function uploadWithProgress(file) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/jobs");
    xhr.responseType = "json";
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    // encodeURIComponent keeps non-ASCII names byte-exact through the
    // ISO-8859-1-only HTTP header; the server decodes it symmetrically.
    xhr.setRequestHeader("X-Filename", encodeURIComponent(file.name));
    xhr.upload.addEventListener("progress", (event) => {
      if (!event.lengthComputable) return;
      const pct = (event.loaded / event.total) * 100;
      const bar = $("progress-bar");
      if (bar) bar.style.width = `${pct}%`;
      const text = $("progress-text");
      if (text) text.textContent = `上传 ${formatBytes(event.loaded)} / ${formatBytes(event.total)}`;
    });
    xhr.addEventListener("load", () => {
      resolve({ status: xhr.status, body: xhr.response });
    });
    xhr.addEventListener("error", () => reject(new Error("网络错误")));
    xhr.addEventListener("abort", () => reject(new Error("上传已取消")));
    xhr.send(file);
  });
}

function showUploadProgress() {
  $("report-card").hidden = false;
  $("progress-wrap").hidden = false;
  $("progress-bar").style.width = "0%";
  $("progress-text").textContent = "准备上传…";
  hideBanner();
}

async function startConversion(event) {
  event.preventDefault();
  if (!state.file) return;
  resetUI();
  showUploadProgress();
  setBusy(true);

  try {
    // Raw streaming body: no multipart spooling on the server, and the
    // filename rides in a header.  Memory and disk use are both bounded.
    const { status, body } = await uploadWithProgress(state.file);
    if (status !== 202) {
      const error = body && body.error ? body.error : { message: `上传失败（HTTP ${status}）` };
      renderFailure(error);
      return;
    }
    state.jobId = body.job.id;
    renderJob(body.job);
    poll();
  } catch (err) {
    renderFailure({ code: "NETWORK", message: `${err.message}，请重试。`, details: {} });
  } finally {
    setBusy(false);
  }
}

function poll() {
  if (state.timer) clearInterval(state.timer);
  const tick = async () => {
    if (!state.jobId) return;
    try {
      const res = await fetch(`/api/jobs/${state.jobId}`);
      const body = await res.json();
      if (!res.ok) {
        renderFailure(body.error || { message: `HTTP ${res.status}` });
        clearInterval(state.timer);
        state.timer = null;
        return;
      }
      renderJob(body);
      if (body.status === "done" || body.status === "failed") {
        clearInterval(state.timer);
        state.timer = null;
      }
    } catch (err) {
      // transient network error: keep polling
    }
  };
  state.timer = setInterval(tick, 700);
  tick();
}

function renderFailure(error) {
  $("report-card").hidden = false;
  $("progress-wrap").hidden = true;
  setBanner("err", error.message || "转换失败。", failureBullets(error.details));
  if (error.code) {
    $("banner").append(el("div", { class: "hint", text: `错误代码：${error.code}` }));
  }
  $("download-wrap").hidden = true;
  $("reset-btn").hidden = false;
}

function failureBullets(details) {
  if (!details) return [];
  const bullets = [];
  const pushAll = (value) => {
    if (!value) return;
    if (Array.isArray(value)) {
      for (const item of value) {
        if (typeof item === "string") bullets.push(item);
        else if (Array.isArray(item)) pushAll(item);
      }
    }
  };
  for (const key of ["problems", "conflicts", "reasons"]) pushAll(details[key]);
  if (details.copies) {
    for (const copy of details.copies) pushAll(copy);
  }
  if (details.remainder !== undefined) {
    bullets.push(`余数 ${details.remainder} 字节`);
  }
  if (details.max_bytes) {
    bullets.push(`服务端上限 ${formatBytes(details.max_bytes)}`);
  }
  if (details.new_reserved_head_bytes !== undefined) {
    bullets.push(
      `4 KiB 主 GPT 保留区：0 .. ${formatBytes(details.new_reserved_head_bytes)}；` +
      `备 GPT 保留区：盘尾 ${formatBytes(details.new_reserved_tail_bytes)}`
    );
  }
  return bullets;
}

function renderJob(job) {
  $("report-card").hidden = false;
  $("reset-btn").hidden = false;
  const summary = job.summary;

  if (job.status === "queued" || job.status === "converting" || job.status === "verifying") {
    $("progress-wrap").hidden = false;
    const total = job.total_bytes || 1;
    let pct = Math.min(100, (job.copied_bytes / total) * 100);
    let label = `${STATUS_TEXT[job.status] || job.status} · ${formatBytes(job.copied_bytes)} / ${formatBytes(job.total_bytes)}`;
    if (job.status === "verifying") {
      pct = 100;
      label = "正在逐区复验成品…";
    }
    $("progress-bar").style.width = `${pct}%`;
    $("progress-text").textContent = label;
  } else {
    $("progress-wrap").hidden = true;
  }

  if (job.status === "failed" && job.error) {
    renderFailure(job.error);
    if (summary) renderSummary(summary);
    return;
  }

  if (summary) renderSummary(summary);

  const checks = job.checks || [];
  if (job.status === "done" && checks.length) {
    $("checks").hidden = false;
    const list = $("checks-list");
    list.replaceChildren();
    for (const check of checks) list.append(el("li", { text: check }));
  }

  if (job.status === "done" && job.download_url) {
    $("download-wrap").hidden = false;
    const link = $("download-link");
    link.href = job.download_url;
    link.setAttribute("download", (job.source_name || "disk.img").replace(/\.img$/i, "") + ".4k.img");
    $("download-note").textContent = `成品 ${formatBytes(job.output_size)} · 复验已通过`;
    setBanner("ok", "转换完成，成品已通过独立复验，可下载。");
  }
}

function renderSummary(summary) {
  const meta = $("meta");
  meta.replaceChildren();
  const rows = [
    ["源盘长度", `${formatBytes(summary.disk_size)}（${summary.disk_size.toLocaleString()} 字节）`],
    ["源 / 目标逻辑扇区", `${summary.source_sector_size} B → ${summary.target_sector_size} B`],
    ["磁盘 GUID", summary.disk_guid],
    ["采用副本", summary.used_gpt_copy === "primary" ? "主 GPT" : "备 GPT"],
    ["分区数量", String(summary.partitions.length)],
  ];
  for (const [key, value] of rows) {
    meta.append(el("div", {}, [el("dt", { text: key }), el("dd", { text: value })]));
  }

  const warnBox = $("warnings");
  if (summary.warnings && summary.warnings.length) {
    warnBox.hidden = false;
    warnBox.replaceChildren(el("strong", { text: "提示：" }));
    const list = el("ul");
    for (const item of summary.warnings) list.append(el("li", { text: item }));
    warnBox.append(list);
  } else {
    warnBox.hidden = true;
  }

  const copies = $("copies");
  copies.replaceChildren();
  for (const copy of summary.copies) {
    const title = copy.which === "primary" ? "主 GPT（LBA 1）" : "备 GPT（盘尾）";
    const head = el("div", { class: "copy-head" }, [
      el("span", { class: "copy-title", text: title }),
      el("span", {
        class: `copy-state ${copy.valid ? "ok" : "bad"}`,
        text: copy.valid ? "校验有效" : "校验失败",
      }),
    ]);
    if (copy.used) head.append(el("span", { class: "copy-state used", text: "本次采用" }));
    const node = el("div", { class: `copy ${copy.valid ? "ok" : "bad"}` }, [head]);
    if (copy.problems && copy.problems.length) {
      const list = el("ul");
      for (const problem of copy.problems) list.append(el("li", { text: problem }));
      node.append(list);
    }
    if (copy.detail && Object.keys(copy.detail).length) {
      const dl = el("dl");
      const fields = [
        ["磁盘 GUID", copy.detail.disk_guid],
        ["MyLBA / AltLBA", `${copy.detail.my_lba} / ${copy.detail.alternate_lba}`],
        ["分区项位置", `LBA ${copy.detail.entries_lba}`],
        ["可用区", `${copy.detail.first_usable_lba}..${copy.detail.last_usable_lba}`],
        ["表头 CRC32", copy.detail.header_crc],
        ["分区项 CRC32", copy.detail.entries_crc],
      ];
      for (const [key, value] of fields) {
        dl.append(el("div", {}, [el("dt", { text: key }), el("dd", { text: String(value) })]));
      }
      node.append(dl);
    }
    copies.append(node);
  }

  const wrap = $("partitions");
  const body = $("partitions-body");
  body.replaceChildren();
  if (!summary.partitions.length) {
    wrap.hidden = true;
    return;
  }
  wrap.hidden = false;
  for (const part of summary.partitions) {
    body.append(
      el("tr", {}, [
        el("td", { text: String(part.index) }),
        el("td", { text: part.name || "（无名称）" }),
        el("td", { class: "mono", text: part.type_guid }),
        el("td", { class: "mono", text: part.unique_guid }),
        el("td", { class: "mono", text: String(part.start_lba_4k) }),
        el("td", { class: "mono", text: String(part.end_lba_4k) }),
        el("td", { class: "mono", text: formatBytes(part.byte_end_exclusive - part.byte_start) }),
      ])
    );
  }
}

async function loadSample(btn) {
  const key = btn.dataset.sampleKey;
  const label = btn.querySelector(".sample-title");
  const original = label ? label.textContent : btn.textContent;
  btn.disabled = true;
  if (label) label.textContent = "正在载入样盘…";
  try {
    let file = state.sampleFiles.get(key);
    if (!file) {
      const res = await fetch(`/api/samples/${encodeURIComponent(key)}`);
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body.error?.message || `HTTP ${res.status}`);
      }
      const blob = await res.blob();
      const name = res.headers.get("content-disposition")?.match(/filename="([^"]+)"/)?.[1]
        || btn.dataset.sampleName
        || `${key}.img`;
      file = new File([blob], name, { type: "application/octet-stream" });
      state.sampleFiles.set(key, file);
    }
    chooseFile(file);
    hideBanner();
  } catch (err) {
    setBanner("err", `样盘载入失败：${err.message}`);
  } finally {
    btn.disabled = false;
    if (label) label.textContent = original;
  }
}

function bindEvents() {
  $("upload-form").addEventListener("submit", startConversion);
  $("file-input").addEventListener("change", (event) => {
    const file = event.target.files && event.target.files[0];
    if (file) chooseFile(file);
  });

  const zone = $("dropzone");
  for (const type of ["dragenter", "dragover"]) {
    zone.addEventListener(type, (event) => {
      event.preventDefault();
      zone.classList.add("dragover");
    });
  }
  for (const type of ["dragleave", "drop"]) {
    zone.addEventListener(type, (event) => {
      event.preventDefault();
      zone.classList.remove("dragover");
    });
  }
  zone.addEventListener("drop", (event) => {
    const file = event.dataTransfer?.files?.[0];
    if (file) chooseFile(file);
  });

  for (const btn of document.querySelectorAll(".sample-card")) {
    btn.addEventListener("click", () => loadSample(btn));
  }

  $("reset-btn").addEventListener("click", () => {
    if (state.jobId) fetch(`/api/jobs/${state.jobId}`, { method: "DELETE" }).catch(() => {});
    resetUI();
    chooseFile(null);
    $("file-input").value = "";
    setBusy(false);
  });
}

bindEvents();
loadConfig();
