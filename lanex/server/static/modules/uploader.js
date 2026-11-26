// uploader.js — one file to the project's upload folder, with progress and
// a way to stop it. Same route and headers as api.upload (POST /api/upload,
// the raw file as the body, the LanEx key as the cookie), on XMLHttpRequest
// because fetch can't report how much of a request body has gone up.
//
//   const up = uploadFile(file, designDir, { onProgress: (sent, total) => … });
//   up.cancel();            // stops it; the server drops the part it got
//   const { upload_id } = await up.done;

import { state } from "./state.js";
import { UPLOAD_MAX_BYTES } from "./api.js";

const UPLOAD = { path: "/api/upload", method: "POST" };

export function uploadFile(file, designDir, { onProgress } = {}) {
  const xhr = new XMLHttpRequest();
  let cancelled = false;
  const done = new Promise((resolve, reject) => {
    if (file.size > UPLOAD_MAX_BYTES) {
      const err = new Error(file.name + " is " + Math.round(file.size / 1048576) + " MiB; LanEx takes files up to " + (UPLOAD_MAX_BYTES / 1073741824) + " GiB.");
      err.status = 413;
      reject(err);
      return;
    }
    xhr.open(UPLOAD.method, UPLOAD.path + "?filename=" + encodeURIComponent(file.name) + (designDir ? "&design_dir=" + encodeURIComponent(designDir) : ""));
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.setRequestHeader("X-Requested-With", "XMLHttpRequest");
    const shown = state.designMissing || state.designDir || "";
    if (shown) xhr.setRequestHeader("X-LanEx-Design", encodeURIComponent(shown));
    xhr.upload.onprogress = (e) => { if (onProgress) onProgress(e.loaded, e.lengthComputable ? e.total : file.size); };
    xhr.onload = () => {
      let body = {};
      try { body = JSON.parse(xhr.responseText || "{}"); } catch (_e) { body = { error: xhr.responseText }; }
      if (xhr.status >= 200 && xhr.status < 300 && body.ok !== false) { resolve(body.data !== undefined ? body.data : body); return; }
      const err = new Error(body.error || ("Upload of " + file.name + " failed (HTTP " + xhr.status + ")."));
      err.status = xhr.status;
      reject(err);
    };
    xhr.onerror = () => reject(Object.assign(new Error("The upload of " + file.name + " lost its connection to LanEx. Check that LanEx is still running, then add the file again."), { status: 0 }));
    xhr.onabort = () => reject(Object.assign(new Error("Upload of " + file.name + " stopped."), { status: 0, cancelled: true }));
    xhr.send(file);
  });
  return {
    done,
    cancel() { if (!cancelled) { cancelled = true; try { xhr.abort(); } catch (_e) {} } },
  };
}

export function fmtBytes(n) {
  if (!Number.isFinite(n)) return "–";
  if (n < 1000) return n + " B";
  if (n < 1e6) return (n / 1e3).toFixed(n < 1e4 ? 1 : 0) + " kB";
  if (n < 1e9) return (n / 1e6).toFixed(n < 1e8 ? 1 : 0) + " MB";
  return (n / 1e9).toFixed(2) + " GB";
}
