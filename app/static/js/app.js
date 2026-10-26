// ShardVault dashboard: polls /api/status and keeps the page in sync.

const $ = (id) => document.getElementById(id);
const POLL_MS = 1200;
const IMAGE_RE = /\.(jpe?g|png|gif|webp|avif|bmp|svg)$/i;
const WORDS = ["Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight"];

let previous = null;
const failedAt = {}; // disk number -> time it failed, to report rebuild time
const destroyedByUser = new Set();

// ------------------------------------------------------------------ helpers
function bytes(n) {
  if (n === null || n === undefined) return "–";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i += 1;
  }
  return `${i === 0 ? n : n.toFixed(n < 10 ? 1 : 0)} ${units[i]}`;
}

function colorClass(key) {
  let h = 0;
  for (const ch of key) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return `c${h % 8}`;
}

// Put `nodes` into `parent` in order, moving only nodes that are out of place.
// (Re-inserting a node restarts its CSS animation, so untouched nodes stay put.)
function placeInOrder(parent, nodes) {
  nodes.forEach((node, i) => {
    if (parent.children[i] !== node) parent.insertBefore(node, parent.children[i] || null);
  });
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function clock() {
  return new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
}

function log(message, tone = "") {
  const li = el("li");
  if (tone) li.dataset.tone = tone;
  const time = el("time", "", clock());
  li.append(time, el("span", "", message));
  $("log").prepend(li);
  while ($("log").children.length > 40) $("log").lastChild.remove();
}

async function api(path, options = {}) {
  const res = await fetch(path, options);
  let data = {};
  try {
    data = await res.json();
  } catch {
    /* non-JSON response */
  }
  if (!res.ok) throw new Error(data.message || `Request failed (${res.status})`);
  return data;
}

// --------------------------------------------------------------------- bays
const STATE_TEXT = { online: "Online", rebuilding: "Rebuilding", failed: "Failed", unknown: "Unknown" };

function buildBays(count) {
  const bays = $("bays");
  for (let n = 1; n <= count; n += 1) {
    const bay = el("article", "bay");
    bay.dataset.disk = String(n);
    bay.dataset.state = "unknown";
    bay.setAttribute("aria-label", `Disk ${n}`);

    const head = el("header", "bay-head");
    head.append(el("span", "led"), el("h3", "bay-name", `Disk ${n}`), el("span", "bay-state", "Unknown"));

    const well = el("div", "bay-well");
    const foot = el("footer", "bay-foot");
    const used = el("span", "bay-used", "–");
    const destroy = el("button", "btn btn-small btn-destroy", "Destroy");
    destroy.type = "button";
    destroy.setAttribute("aria-label", `Destroy disk ${n}`);
    destroy.addEventListener("click", () => askDestroy(n));
    foot.append(used, destroy);

    bay.append(head, well, foot);
    bays.append(bay);
  }
}

function renderBay(bay, disk, files) {
  bay.dataset.state = disk.state;
  bay.querySelector(".bay-state").textContent = STATE_TEXT[disk.state] || disk.state;
  const well = bay.querySelector(".bay-well");

  if (disk.state === "failed") {
    if (!well.querySelector(".bay-message.failed")) {
      well.replaceChildren(el("p", "bay-message failed", "Disk erased. Its pieces are gone, but every file is still readable from the other disks."));
    }
    bay.querySelector(".bay-used").textContent = "0 B";
    return;
  }

  if (disk.pieces === null) {
    well.replaceChildren(el("p", "bay-message", "Piece view needs STORAGE_DIR (run with Docker Compose)."));
    bay.querySelector(".bay-used").textContent = "–";
    return;
  }

  if (files.length) well.querySelector(".bay-message")?.remove();
  const wanted = new Map(files.map((f) => [f.key, f]));
  const existing = new Map([...well.querySelectorAll(".piece")].map((p) => [p.dataset.key, p]));

  for (const [key, node] of existing) if (!wanted.has(key)) node.remove();

  const ordered = files.map((file) => {
    const present = file.key in disk.pieces;
    let node = existing.get(file.key);
    const wasMissing = node?.classList.contains("is-missing");
    if (!node || (wasMissing && present)) {
      const fresh = el("div", `piece ${colorClass(file.key)}`);
      fresh.dataset.key = file.key;
      fresh.append(el("span", "piece-name", file.key), el("span", "piece-size"));
      if (node) node.replaceWith(fresh);
      node = fresh;
    }
    node.classList.toggle("is-missing", !present);
    node.querySelector(".piece-size").textContent = present ? bytes(disk.pieces[file.key]) : "missing";
    return node;
  });
  placeInOrder(well, ordered);

  if (!files.length && !well.querySelector(".bay-message")) {
    well.append(el("p", "bay-message", "Empty. Upload a file to fill the disks."));
  }
  const count = Object.keys(disk.pieces).length;
  bay.querySelector(".bay-used").textContent = `${bytes(disk.bytes)} in ${count} ${count === 1 ? "piece" : "pieces"}`;
}

// -------------------------------------------------------------------- files
function renderFiles(status) {
  const list = $("file-list");
  const files = status.files;
  $("file-count").textContent = files.length ? `${files.length} ${files.length === 1 ? "file" : "files"}` : "";
  $("files-empty").hidden = files.length > 0 || Boolean(status.files_error);
  $("files-error").hidden = !status.files_error;
  $("files-error").textContent = status.files_error || "";

  const rows = new Map([...list.children].map((li) => [li.dataset.key, li]));
  for (const [key, li] of rows) if (!files.some((f) => f.key === key)) li.remove();

  const ordered = files.map((file) => {
    let li = rows.get(file.key);
    if (!li) li = buildFileRow(file);
    const spread = li.querySelector(".spread");
    spread.replaceChildren(
      ...status.disks.map((d) => {
        const on = file.on_disks.includes(d.number);
        const s = el("span", on ? "" : "off");
        s.title = `Disk ${d.number}: ${on ? "piece present" : "piece missing"}`;
        return s;
      })
    );
    li.querySelector(".pieces-text").textContent = `${file.on_disks.length} of ${status.disks.length} pieces`;
    return li;
  });
  placeInOrder(list, ordered);
}

function buildFileRow(file) {
  const li = el("li", `file ${colorClass(file.key)}`);
  li.dataset.key = file.key;
  const url = `/api/files/${encodeURIComponent(file.key)}`;

  let thumb;
  if (IMAGE_RE.test(file.key)) {
    thumb = el("img", "thumb");
    thumb.alt = "";
    thumb.loading = "lazy";
    thumb.src = `${url}?inline=1`;
    thumb.addEventListener("error", () => thumb.replaceWith(letterThumb(file.key)), { once: true });
  } else {
    thumb = letterThumb(file.key);
  }

  const info = el("div", "file-info");
  const meta = el("div", "file-meta");
  meta.append(el("span", "", bytes(file.size)), el("span", "spread"), el("span", "pieces-text"));
  info.append(el("p", "file-name", file.key), meta);

  const actions = el("div", "file-actions");
  const download = el("a", "btn btn-small btn-quiet", "Download");
  download.href = url;
  const verify = el("button", "btn btn-small btn-quiet", "Verify");
  verify.type = "button";
  verify.addEventListener("click", () => verifyFile(file.key, li, verify));
  const remove = el("button", "btn btn-small btn-quiet", "Delete");
  remove.type = "button";
  remove.addEventListener("click", () => deleteFile(file.key, remove));
  actions.append(download, verify, remove);

  const verdict = el("p", "verdict");
  verdict.setAttribute("role", "status");
  li.append(thumb, info, actions, verdict);
  return li;
}

function letterThumb(key) {
  const ext = (key.split(".").pop() || "file").slice(0, 4).toUpperCase();
  return el("span", "thumb", ext);
}

async function verifyFile(key, li, button) {
  const verdict = li.querySelector(".verdict");
  button.disabled = true;
  verdict.dataset.tone = "";
  verdict.textContent = "Reading every byte back and computing SHA-256";
  const online = previous ? previous.erasure.online : null;
  try {
    const r = await api(`/api/files/${encodeURIComponent(key)}/verify`, { method: "POST" });
    const when = online !== null ? ` with ${online} of ${previous.erasure.total} disks online` : "";
    if (r.intact) {
      verdict.dataset.tone = "ok";
      verdict.textContent = `Intact. SHA-256 matches the original upload (read in ${r.seconds.toFixed(2)} s${when}).`;
      log(`Verified ${key}: identical to the original${when}.`, "ok");
    } else if (r.intact === null) {
      verdict.textContent = "Read back successfully. This file has no stored checksum to compare with.";
    } else {
      verdict.dataset.tone = "error";
      verdict.textContent = "Checksum mismatch: this file does not match what was uploaded.";
      log(`Verification failed for ${key}.`, "fail");
    }
  } catch (err) {
    verdict.dataset.tone = "error";
    verdict.textContent = err.message;
    log(`Could not read ${key}: ${err.message}`, "fail");
  } finally {
    button.disabled = false;
  }
}

async function deleteFile(key, button) {
  if (!window.confirm(`Delete ${key}? This removes its pieces from every disk.`)) return;
  button.disabled = true;
  try {
    await api(`/api/files/${encodeURIComponent(key)}`, { method: "DELETE" });
    log(`Deleted ${key}.`);
    refresh();
  } catch (err) {
    log(`Could not delete ${key}: ${err.message}`, "fail");
    button.disabled = false;
  }
}

// ------------------------------------------------------------------ uploads
async function uploadFiles(fileList) {
  const status = $("upload-status");
  const maxBytes = Number(document.body.dataset.maxMb) * 1024 * 1024;
  for (const file of fileList) {
    if (file.size > maxBytes) {
      status.dataset.tone = "error";
      status.textContent = `${file.name} is larger than ${document.body.dataset.maxMb} MB.`;
      continue;
    }
    status.dataset.tone = "";
    status.textContent = `Uploading ${file.name} (${bytes(file.size)})`;
    const form = new FormData();
    form.append("file", file);
    try {
      const r = await api("/api/files", { method: "POST", body: form });
      status.textContent = `Stored ${r.key}. MinIO split it into pieces across the disks.`;
      log(`Uploaded ${r.key} (${bytes(r.size)}).`);
    } catch (err) {
      status.dataset.tone = "error";
      status.textContent = err.message;
      log(`Upload of ${file.name} refused: ${err.message}`, "fail");
    }
  }
  $("file-input").value = "";
  refresh();
}

function setupUploads() {
  const zone = $("dropzone");
  $("file-input").addEventListener("change", (e) => uploadFiles(e.target.files));
  ["dragenter", "dragover"].forEach((t) =>
    zone.addEventListener(t, (e) => {
      e.preventDefault();
      zone.classList.add("is-over");
    })
  );
  ["dragleave", "drop"].forEach((t) => zone.addEventListener(t, () => zone.classList.remove("is-over")));
  zone.addEventListener("drop", (e) => {
    e.preventDefault();
    if (e.dataTransfer.files.length) uploadFiles(e.dataTransfer.files);
  });
}

// ------------------------------------------------------------ disk actions
let pendingDestroy = null;

function askDestroy(n) {
  pendingDestroy = n;
  $("destroy-title").textContent = `Destroy disk ${n}?`;
  $("destroy-body").textContent =
    `This permanently erases every piece stored on disk ${n}, like a drive dying. ` +
    "Files stay readable from the other disks, and MinIO rebuilds this one automatically.";
  $("destroy-confirm").textContent = `Destroy disk ${n}`;
  $("destroy-dialog").showModal();
}

function setupDialogs() {
  $("destroy-dialog").addEventListener("close", async () => {
    if ($("destroy-dialog").returnValue !== "confirm" || pendingDestroy === null) return;
    const n = pendingDestroy;
    pendingDestroy = null;
    try {
      await api(`/api/disks/${n}/destroy`, { method: "POST" });
      destroyedByUser.add(n);
      failedAt[n] = Date.now();
      log(`You destroyed disk ${n}. All of its data was deleted.`, "fail");
      refresh();
    } catch (err) {
      log(`Could not destroy disk ${n}: ${err.message}`, "fail");
    }
  });

  $("repair").addEventListener("click", async () => {
    const btn = $("repair");
    btn.disabled = true;
    try {
      await api("/api/repair", { method: "POST" });
      log("Repair started: MinIO is checking every disk and rebuilding missing pieces.");
      refresh();
    } catch (err) {
      log(`Could not start repair: ${err.message}`, "fail");
    } finally {
      setTimeout(() => (btn.disabled = false), 1500);
    }
  });
}

// ------------------------------------------------------------------ status
function renderHealth(status) {
  const health = $("health");
  const text = $("health-text");
  const e = status.erasure;
  if (!status.reachable) {
    health.dataset.tone = previous && previous.reachable ? "fail" : "wait";
    text.textContent = previous && previous.reachable ? "MinIO is not responding" : "Connecting to MinIO";
    return;
  }
  const states = status.disks.map((d) => d.state);
  const of = `${e.online} of ${e.total} disks online`;
  if (states.every((s) => s === "online")) {
    health.dataset.tone = "ok";
    text.textContent = `Healthy: ${of}`;
  } else if (e.online === e.total) {
    health.dataset.tone = "warn";
    text.textContent = `Rebuilding: ${of}`;
  } else if (e.can_write) {
    health.dataset.tone = "warn";
    text.textContent = `Degraded: ${of}, fully working`;
  } else if (e.can_read) {
    health.dataset.tone = "warn";
    text.textContent = `Degraded: ${of}, read-only`;
  } else {
    health.dataset.tone = "fail";
    text.textContent = `Data unavailable: ${of}`;
  }
}

function renderIntro(status) {
  const e = status.erasure;
  if (!status.reachable) return;
  $("headline").textContent = `${WORDS[e.total] || e.total} disks. Any ${(WORDS[e.can_lose] || e.can_lose).toLowerCase()} can fail.`;
  $("explainer").textContent =
    `Every file is split into ${e.data_pieces} data pieces and ${e.parity_pieces} parity pieces, one on each disk. ` +
    `Any ${e.data_pieces} pieces rebuild the whole file. Destroy a disk below and watch MinIO repair it.`;

  const usage = $("usage");
  if (status.files.length && status.inspectable) {
    usage.hidden = false;
    usage.innerHTML = "";
    usage.append(
      "Storing ",
      el("strong", "", bytes(status.usage.logical)),
      " of files on ",
      el("strong", "", bytes(status.usage.raw)),
      ` of disk. That ${e.overhead}× overhead is what lets any ${e.can_lose} disks fail without losing data.`
    );
  } else {
    usage.hidden = true;
  }
}

function detectEvents(status) {
  if (!previous || !previous.reachable || !status.reachable) {
    if (status.reachable && (!previous || !previous.reachable)) log("Connected to MinIO.", "ok");
    return;
  }
  status.disks.forEach((disk) => {
    const before = previous.disks.find((d) => d.number === disk.number)?.state;
    const now = disk.state;
    if (before === now) return;
    if (now === "failed") {
      failedAt[disk.number] ??= Date.now();
      if (!destroyedByUser.has(disk.number)) log(`Disk ${disk.number} failed.`, "fail");
      destroyedByUser.delete(disk.number);
    } else if (now === "rebuilding" && before === "failed") {
      log(`MinIO detected disk ${disk.number} as a fresh drive and is rebuilding its pieces.`);
    } else if (now === "online" && (before === "failed" || before === "rebuilding")) {
      const since = failedAt[disk.number];
      const took = since ? ` in ${Math.max(1, Math.round((Date.now() - since) / 1000))} s` : "";
      delete failedAt[disk.number];
      log(`Disk ${disk.number} rebuilt${took}. Every piece is back.`, "ok");
    }
  });
  const was = previous.erasure;
  const is = status.erasure;
  if (was.can_write && !is.can_write && is.can_read) {
    log(`Uploads paused: writing safely needs ${is.write_quorum} disks. Downloads still work.`, "fail");
  }
  if (!was.can_write && is.can_write) log("Uploads available again.", "ok");
  if (was.can_read && !is.can_read) log("Data unavailable: more disks failed than the parity can cover.", "fail");
  if (!was.can_read && is.can_read) log("Data readable again.", "ok");
}

let timer;
async function refresh() {
  clearTimeout(timer);
  try {
    const status = await api("/api/status");
    detectEvents(status);
    renderHealth(status);
    renderIntro(status);
    const bays = [...$("bays").children];
    status.disks.forEach((disk, i) => bays[i] && renderBay(bays[i], disk, status.files));
    renderFiles(status);
    previous = status;
  } catch (err) {
    $("health").dataset.tone = "fail";
    $("health-text").textContent = "Dashboard cannot reach the app server";
  }
  timer = setTimeout(refresh, POLL_MS);
}

buildBays(Number(document.body.dataset.disks) || 4);
setupUploads();
setupDialogs();
refresh();
