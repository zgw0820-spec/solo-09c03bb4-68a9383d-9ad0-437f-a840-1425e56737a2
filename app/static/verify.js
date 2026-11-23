"use strict";

/*
 * Offline/local block verifier for 4Kn migration images.
 *
 * The page never uploads anything: the manifest is parsed with JSON.parse and
 * the image is read through File.slice() one block at a time.  It is embedded
 * verbatim into the downloadable single-file copy, so it must be a classic
 * script (no modules, no imports).
 */
(function () {
  const FORMAT = "gpt-4kn-block-manifest";
  const VERSION = 1;
  const HEX = /^[0-9a-f]{64}$/;
  const MAX_MANIFEST_BYTES = 64 * 1024 * 1024;
  const MAX_BLOCK_BYTES = 64 * 1024 * 1024;
  const MAX_BLOCK_COUNT = 10000000;

  const $ = (id) => document.getElementById(id);
  const state = {
    manifestFile: null,
    manifest: null,
    imageFile: null,
    mismatches: [],
    running: false,
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

  function isInt(value) {
    return Number.isInteger(value) && typeof value === "number";
  }

  /** Strict, all-problems-at-once validation; mirrors app/manifest.py. */
  function validateManifest(doc) {
    const problems = [];
    if (!doc || typeof doc !== "object" || Array.isArray(doc)) {
      return { errors: ["清单根节点必须是 JSON 对象"] };
    }
    if (doc.format !== FORMAT) problems.push(`清单格式标识必须是 ${FORMAT}`);
    if (doc.version !== VERSION) problems.push(`清单版本 ${doc.version} 不受支持`);
    const algorithm = doc.algorithm === undefined ? "sha256" : doc.algorithm;
    if (algorithm !== "sha256") problems.push("清单算法必须是 sha256");
    if (!isInt(doc.image_size) || doc.image_size <= 0) {
      problems.push("清单字段 image_size 缺失或不是正整数");
    }
    if (!isInt(doc.block_bytes) || doc.block_bytes <= 0) {
      problems.push("清单字段 block_bytes 缺失或不是正整数");
    } else if (doc.block_bytes > MAX_BLOCK_BYTES) {
      problems.push(`块大小 ${doc.block_bytes} 超过 ${MAX_BLOCK_BYTES} 字节上限`);
    }
    if (!isInt(doc.block_count) || doc.block_count < 0) {
      problems.push("清单字段 block_count 缺失或不是非负整数");
    } else if (doc.block_count > MAX_BLOCK_COUNT) {
      problems.push(`块数量 ${doc.block_count} 超过 ${MAX_BLOCK_COUNT} 上限`);
    }
    if (typeof doc.image_name !== "string" || !doc.image_name.trim()) {
      problems.push("清单缺少镜像文件名 image_name");
    }
    if (!Array.isArray(doc.blocks)) problems.push("清单缺少 blocks 数组");
    if (problems.length || !Array.isArray(doc.blocks)) {
      return { errors: problems };
    }

    const imageSize = doc.image_size;
    const blockBytes = doc.block_bytes;
    const expectedCount = Math.ceil(imageSize / blockBytes);
    if (doc.block_count !== expectedCount) {
      problems.push(
        `清单声明 ${doc.block_count} 块，按镜像长度 ${imageSize} 与块大小 ${blockBytes} 应为 ${expectedCount} 块`
      );
    }
    if (doc.blocks.length !== doc.block_count) {
      problems.push(
        `清单 blocks 实际 ${doc.blocks.length} 项，与声明的 ${doc.block_count} 块不符`
      );
    }

    const blocks = [];
    const seen = new Set();
    let cursor = 0;
    for (let i = 0; i < doc.blocks.length; i += 1) {
      const entry = doc.blocks[i];
      if (!entry || typeof entry !== "object") {
        problems.push(`第 ${i + 1} 个块不是对象`);
        continue;
      }
      if (!isInt(entry.index) || entry.index < 0) {
        problems.push(`第 ${i + 1} 个块 index 非法`);
        continue;
      }
      if (seen.has(entry.index)) problems.push(`块索引 ${entry.index} 重复`);
      seen.add(entry.index);
      if (!isInt(entry.offset) || !isInt(entry.length) || entry.length <= 0) {
        problems.push(`块 ${entry.index} 的 offset/length 非法`);
        continue;
      }
      if (entry.offset !== cursor) {
        problems.push(
          `块 ${entry.index} 起始字节 ${entry.offset} 与上一块结束字节 ${cursor} 不连续`
        );
      }
      const expectedLength = Math.min(blockBytes, imageSize - entry.offset);
      if (entry.length !== expectedLength) {
        problems.push(
          `块 ${entry.index} 长度 ${entry.length} 字节，按位置应为 ${expectedLength} 字节`
        );
      }
      if (typeof entry.sha256 !== "string" || !HEX.test(entry.sha256)) {
        problems.push(`块 ${entry.index} 的 sha256 不是 64 位小写十六进制`);
        blocks.push({ index: entry.index, offset: entry.offset, length: entry.length, sha256: String(entry.sha256 || ""), invalid: true });
      } else {
        blocks.push({ index: entry.index, offset: entry.offset, length: entry.length, sha256: entry.sha256 });
      }
      cursor = entry.offset + entry.length;
    }
    if (cursor !== imageSize) {
      problems.push(
        `清单块只覆盖到字节 ${cursor}，镜像共 ${imageSize} 字节，清单不完整`
      );
    }

    const regions = [];
    if (doc.regions !== undefined && !Array.isArray(doc.regions)) {
      problems.push("regions 必须是数组");
    } else if (Array.isArray(doc.regions)) {
      let regionCursor = 0;
      for (let i = 0; i < doc.regions.length; i += 1) {
        const region = doc.regions[i];
        if (!region || typeof region !== "object") {
          problems.push(`第 ${i + 1} 个区域不是对象`);
          continue;
        }
        if (!["gpt-head", "gpt-tail", "partition", "unallocated"].includes(region.kind)) {
          problems.push(`第 ${i + 1} 个区域类型 ${JSON.stringify(region.kind)} 未知`);
        }
        if (!isInt(region.start) || !isInt(region.end) || region.end <= region.start) {
          problems.push(`第 ${i + 1} 个区域范围非法`);
          continue;
        }
        if (region.start !== regionCursor) {
          problems.push(
            `区域 [${region.start}, ${region.end}) 与上一区域结束字节 ${regionCursor} 不连续`
          );
        }
        regionCursor = region.end;
        regions.push(region);
      }
      if (regions.length && regionCursor !== imageSize) {
        problems.push(
          `区域只覆盖到字节 ${regionCursor}，镜像共 ${imageSize} 字节`
        );
      }
    }

    return {
      errors: problems,
      blocks,
      regions,
      image_size: imageSize,
      block_bytes: blockBytes,
      block_count: expectedCount,
      image_name: doc.image_name,
      disk_guid: typeof doc.disk_guid === "string" ? doc.disk_guid : "",
      source_name: typeof doc.source_name === "string" ? doc.source_name : "",
    };
  }

  // ---- SHA-256 -----------------------------------------------------------
  // crypto.subtle is only exposed in secure contexts.  A saved HTML file
  // opened via file:// qualifies, but http://<lan-ip>:8000 does not, so a
  // compact pure-JS implementation keeps that case working too.
  const SHA_K = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ]);

  function sha256Fallback(bytes) {
    const bitLen = bytes.length * 8;
    const total = ((bytes.length + 9 + 63) >> 6) << 6;
    const padded = new Uint8Array(total);
    padded.set(bytes);
    padded[bytes.length] = 0x80;
    const view = new DataView(padded.buffer);
    view.setUint32(total - 8, Math.floor(bitLen / 0x100000000));
    view.setUint32(total - 4, bitLen >>> 0);
    const h = new Uint32Array([
      0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a,
      0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
    ]);
    const w = new Uint32Array(64);
    for (let off = 0; off < total; off += 64) {
      for (let i = 0; i < 16; i += 1) w[i] = view.getUint32(off + i * 4);
      for (let i = 16; i < 64; i += 1) {
        const s0 = ((w[i - 15] >>> 7) | (w[i - 15] << 25)) ^ ((w[i - 15] >>> 18) | (w[i - 15] << 14)) ^ (w[i - 15] >>> 3);
        const s1 = ((w[i - 2] >>> 17) | (w[i - 2] << 15)) ^ ((w[i - 2] >>> 19) | (w[i - 2] << 13)) ^ (w[i - 2] >>> 10);
        w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
      }
      let [a, b, c, d, e, f, g, hh] = h;
      for (let i = 0; i < 64; i += 1) {
        const S1 = ((e >>> 6) | (e << 26)) ^ ((e >>> 11) | (e << 21)) ^ ((e >>> 25) | (e << 7));
        const ch = (e & f) ^ (~e & g);
        const t1 = (hh + S1 + ch + SHA_K[i] + w[i]) >>> 0;
        const S0 = ((a >>> 2) | (a << 30)) ^ ((a >>> 13) | (a << 19)) ^ ((a >>> 22) | (a << 10));
        const maj = (a & b) ^ (a & c) ^ (b & c);
        const t2 = (S0 + maj) >>> 0;
        hh = g; g = f; f = e; e = (d + t1) >>> 0;
        d = c; c = b; b = a; a = (t1 + t2) >>> 0;
      }
      h[0] = (h[0] + a) >>> 0; h[1] = (h[1] + b) >>> 0;
      h[2] = (h[2] + c) >>> 0; h[3] = (h[3] + d) >>> 0;
      h[4] = (h[4] + e) >>> 0; h[5] = (h[5] + f) >>> 0;
      h[6] = (h[6] + g) >>> 0; h[7] = (h[7] + hh) >>> 0;
    }
    let out = "";
    for (const value of h) out += value.toString(16).padStart(8, "0");
    return out;
  }

  const subtle = typeof crypto !== "undefined" && crypto.subtle ? crypto.subtle : null;
  let usingFallback = false;

  async function sha256Hex(bytes) {
    if (subtle) {
      const digest = await subtle.digest("SHA-256", bytes);
      return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
    }
    usingFallback = true;
    return sha256Fallback(bytes);
  }

  // ---- UI ----------------------------------------------------------------
  function pickManifest(file) {
    state.manifestFile = file;
    state.manifest = null;
    $("manifest-drop").classList.toggle("has-file", Boolean(file));
    $("manifest-hint").textContent = file ? `${file.name} · ${formatBytes(file.size)}` : "尚未选择清单";
    updateStart();
  }

  function pickImage(file) {
    state.imageFile = file;
    $("image-drop").classList.toggle("has-file", Boolean(file));
    $("image-hint").textContent = file ? `${file.name} · ${formatBytes(file.size)}` : "尚未选择镜像";
    updateStart();
  }

  function updateStart() {
    $("verify-btn").disabled = state.running || !state.manifestFile || !state.imageFile;
  }

  function reset() {
    if (state.running) return;
    state.manifest = null;
    state.mismatches = [];
    pickManifest(null);
    pickImage(null);
    $("manifest-input").value = "";
    $("image-input").value = "";
    $("result-card").hidden = true;
    $("banner").hidden = true;
    $("progress-wrap").hidden = true;
    $("mismatch-wrap").hidden = true;
    $("regions-wrap").hidden = true;
    $("mismatch-body").replaceChildren();
    $("regions-body").replaceChildren();
    $("meta").replaceChildren();
  }

  async function readManifest(file) {
    if (file.size > MAX_MANIFEST_BYTES) {
      throw new Error(`清单文件 ${formatBytes(file.size)} 超过 ${formatBytes(MAX_MANIFEST_BYTES)} 上限`);
    }
    const text = await file.text();
    let doc;
    try {
      doc = JSON.parse(text);
    } catch (err) {
      throw new Error(`清单不是合法的 JSON：${err.message}`);
    }
    const checked = validateManifest(doc);
    if (checked.errors.length) {
      const error = new Error("清单不完整或格式不符，已拒绝核对。");
      error.details = checked.errors;
      throw error;
    }
    return checked;
  }

  function renderMeta(manifest, imageFile, checkedHashCount) {
    const meta = $("meta");
    meta.replaceChildren();
    const rows = [
      ["清单文件", state.manifestFile.name],
      ["镜像文件", imageFile.name],
      ["清单声明的镜像长度", `${manifest.image_size.toLocaleString()} 字节（${formatBytes(manifest.image_size)}）`],
      ["本地镜像长度", `${imageFile.size.toLocaleString()} 字节（${formatBytes(imageFile.size)}）`],
      ["块大小", `${manifest.block_bytes.toLocaleString()} 字节（${formatBytes(manifest.block_bytes)}）`],
      ["块数量", String(manifest.block_count)],
      ["已核对块", String(checkedHashCount)],
      ["盘尾不足一块", manifest.image_size % manifest.block_bytes === 0 ? "无（长度是块大小整数倍）" : `${manifest.image_size % manifest.block_bytes} 字节`],
      ["哈希实现", usingFallback ? "内置纯 JS SHA-256（页面未处于安全上下文）" : "浏览器 Web Crypto（SHA-256）"],
    ];
    if (manifest.disk_guid) rows.push(["磁盘 GUID", manifest.disk_guid]);
    if (manifest.regions && manifest.regions.length) rows.push(["区域数量", String(manifest.regions.length)]);
    for (const [key, value] of rows) {
      meta.append(el("div", {}, [el("dt", { text: key }), el("dd", { text: value })]));
    }
  }

  function renderRegions(regions) {
    if (!regions || !regions.length) {
      $("regions-wrap").hidden = true;
      return;
    }
    $("regions-wrap").hidden = false;
    const body = $("regions-body");
    body.replaceChildren();
    for (const region of regions) {
      body.append(
        el("tr", {}, [
          el("td", { text: region.label ? `${region.label}（${region.kind}）` : region.kind }),
          el("td", { class: "mono", text: String(region.start) }),
          el("td", { class: "mono", text: String(region.end) }),
        ])
      );
    }
  }

  function renderMismatches(mismatches) {
    const body = $("mismatch-body");
    body.replaceChildren();
    if (!mismatches.length) {
      $("mismatch-wrap").hidden = true;
      return;
    }
    $("mismatch-wrap").hidden = false;
    for (const item of mismatches) {
      body.append(
        el("tr", {}, [
          el("td", { text: String(item.index) }),
          el("td", { class: "mono", text: String(item.offset) }),
          el("td", { class: "mono", text: String(item.length) }),
          el("td", { class: "mono", text: item.expected }),
          el("td", { class: "mono", text: item.actual }),
        ])
      );
    }
    const csv = ["block_index,start_byte,length_bytes,manifest_sha256,local_sha256"]
      .concat(
        mismatches.map((m) => [m.index, m.offset, m.length, m.expected, m.actual].join(","))
      )
      .join("\n") + "\n";
    const url = URL.createObjectURL(new Blob([csv], { type: "text/csv" }));
    const link = $("mismatch-download");
    if (link.dataset.url) URL.revokeObjectURL(link.dataset.url);
    link.href = url;
    link.dataset.url = url;
    link.download = `mismatch-${state.manifestFile.name.replace(/\.json$/i, "")}.csv`;
  }

  async function verify() {
    if (state.running || !state.manifestFile || !state.imageFile) return;
    state.running = true;
    updateStart();
    $("result-card").hidden = false;
    $("banner").hidden = true;
    $("mismatch-wrap").hidden = true;
    $("regions-wrap").hidden = true;
    $("mismatch-body").replaceChildren();
    $("regions-body").replaceChildren();
    $("meta").replaceChildren();
    $("progress-wrap").hidden = true;

    try {
      const manifest = await readManifest(state.manifestFile);
      const image = state.imageFile;

      // Refuse before hashing anything when the manifest does not describe
      // exactly this image length: a partial check would be misleading.
      if (image.size !== manifest.image_size) {
        setBanner(
          "err",
          `镜像长度不符，拒绝核对：清单声明 ${manifest.image_size.toLocaleString()} 字节，` +
            `所选镜像为 ${image.size.toLocaleString()} 字节（相差 ${Math.abs(image.size - manifest.image_size).toLocaleString()} 字节）。` +
            "请确认选择的是同一份成品镜像与清单。"
        );
        renderMeta(manifest, image, 0);
        renderRegions(manifest.regions);
        return;
      }

      state.manifest = manifest;
      $("progress-wrap").hidden = false;
      $("progress-bar").style.width = "0%";
      $("progress-text").textContent = "正在逐块读取…";

      const mismatches = [];
      let done = 0;
      for (const block of manifest.blocks) {
        const buffer = await image.slice(block.offset, block.offset + block.length).arrayBuffer();
        const actual = await sha256Hex(new Uint8Array(buffer));
        if (actual !== block.sha256) {
          mismatches.push({
            index: block.index,
            offset: block.offset,
            length: block.length,
            expected: block.sha256,
            actual,
          });
        }
        done += 1;
        if (done % 8 === 0 || done === manifest.blocks.length) {
          const pct = (done / manifest.blocks.length) * 100;
          $("progress-bar").style.width = `${pct}%`;
          $("progress-text").textContent =
            `已核对 ${done} / ${manifest.blocks.length} 块 · 不匹配 ${mismatches.length}`;
          // Yield so the progress bar paints on large images.
          await new Promise((resolve) => setTimeout(resolve, 0));
        }
      }

      state.mismatches = mismatches;
      renderMeta(manifest, image, done);
      renderRegions(manifest.regions);
      renderMismatches(mismatches);
      $("progress-wrap").hidden = true;

      if (!mismatches.length) {
        setBanner(
          "ok",
          `逐块核对通过：${manifest.blocks.length} 块全部与清单一致（镜像 ${formatBytes(manifest.image_size)}，` +
            `含 GPT、未分配区与盘尾）。`
        );
      } else {
        const bytes = mismatches.reduce((sum, m) => sum + m.length, 0);
        const bullets = mismatches
          .slice(0, 50)
          .map((m) => `块 ${m.index}：起始字节 ${m.offset}，长度 ${m.length} 字节`);
        if (mismatches.length > bullets.length) {
          bullets.push(`……另有 ${mismatches.length - bullets.length} 个不匹配块，见下表或 CSV。`);
        }
        setBanner(
          "err",
          `发现 ${mismatches.length} 个不匹配块，涉及 ${formatBytes(bytes)}，需要重拷这些区间。`,
          bullets
        );
      }
    } catch (err) {
      const bullets = Array.isArray(err.details) ? err.details : [];
      setBanner("err", err.message || String(err), bullets);
    } finally {
      state.running = false;
      updateStart();
      $("progress-wrap").hidden = true;
    }
  }

  function bindDrop(zoneId, inputId, handler) {
    const zone = $(zoneId);
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
      const file = event.dataTransfer && event.dataTransfer.files && event.dataTransfer.files[0];
      if (file) handler(file);
    });
    $(inputId).addEventListener("change", (event) => {
      const file = event.target.files && event.target.files[0];
      if (file) handler(file);
    });
  }

  bindDrop("manifest-drop", "manifest-input", pickManifest);
  bindDrop("image-drop", "image-input", pickImage);
  $("verify-btn").addEventListener("click", verify);
  $("reset-btn").addEventListener("click", reset);
  $("result-card").hidden = true;
})();
