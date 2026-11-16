/* Web Worker host for Standalone iPadOS Companion Engine. */
"use strict";

let _workerImportError = null;
try {
  importScripts("default_pack.js", "companion_engine.js");
} catch (err) {
  _workerImportError = err;
}

let runtime = null;

const workerClient = {
  readyState: 1,
  onRawMessage(obj) {
    if (!obj || typeof obj !== "object") return;
    if (obj.type === "calib_snapshot" && obj.snapRgba && obj.snapRgba.length) {
      const payload = Object.assign({}, obj);
      delete payload.snapRgba;
      let u8 = obj.snapRgba;
      if (!(u8 instanceof Uint8Array) || u8.byteOffset !== 0 || u8.byteLength !== u8.buffer.byteLength) {
        u8 = new Uint8Array(obj.snapRgba);
      }
      self.postMessage({ type: "ws_message_snapshot", payload, snapRgba: u8 }, [u8.buffer]);
      return;
    }
    self.postMessage({ type: "ws_message_str", data: JSON.stringify(obj) });
  },
  onmessage(ev) {
    if (!ev) return;
    self.postMessage({ type: "ws_message_str", data: ev.data });
  },
};

self.onmessage = function (ev) {
  const msg = ev && ev.data;
  if (!msg || !msg.type) return;

  if (msg.type === "init") {
    if (_workerImportError || typeof IpadCompanionEngine === "undefined") {
      self.postMessage({
        type: "worker_fallback",
        reason: _workerImportError
          ? "import_error:" + (_workerImportError.message || String(_workerImportError))
          : "engine_undefined",
      });
      return;
    }
    try {
      if (runtime) {
        runtime.stopNativePolling();
        runtime.clients.delete(workerClient);
      }
      runtime = new IpadCompanionEngine.IpadCompanionRuntime({
        savedPack: msg.savedPack,
        isNativeHost: msg.isNativeHost,
        frameUrlBase: msg.frameUrlBase,
        preserveDesktopAnchor: !!msg.preserveDesktopAnchor,
        installGlobalHooks: false,
        onSavePack(raw) {
          self.postMessage({ type: "worker_save_pack", packJson: raw });
        },
        onNativeAction(payload) {
          self.postMessage({ type: "worker_native_action", payload });
        },
        onXhrBlocked() {
          self.postMessage({ type: "worker_fallback", reason: "xhr_blocked" });
        },
      });
      runtime.clients.add(workerClient);
      runtime.startNativePolling();
      self.postMessage({
        type: "worker_ready",
        status: runtime.buildStatusPayload(),
      });
    } catch (err) {
      self.postMessage({
        type: "worker_fallback",
        reason: "init_error:" + (err && err.message ? err.message : String(err)),
      });
    }
    return;
  }

  if (!runtime) return;

  if (msg.type === "ws_send") {
    runtime.handleClientMessage(workerClient, msg.data);
  } else if (msg.type === "ingest_rgba") {
    runtime.ingestRGBA(msg.rgba, msg.w, msg.h);
  } else if (msg.type === "set_ext_connected") {
    runtime.extConnected = !!msg.connected;
    runtime.broadcast(runtime.buildStatusPayload());
  } else if (msg.type === "set_ext_status") {
    if (msg.status && typeof msg.status === "object") {
      runtime.extDiag = Object.assign({}, runtime.extDiag || {}, msg.status);
      if (typeof msg.status.connected === "boolean") {
        runtime.extConnected = msg.status.connected;
      }
      runtime.broadcast(runtime.buildStatusPayload());
    }
  } else if (msg.type === "set_host_info") {
    if (msg.info && typeof msg.info === "object") {
      runtime.hostInfo = Object.assign({}, runtime.hostInfo || {}, msg.info);
      runtime.broadcast(runtime.buildStatusPayload());
    }
  } else if (msg.type === "shutdown") {
    runtime.stopNativePolling();
    runtime.clients.delete(workerClient);
    runtime = null;
    self.close();
  }
};
