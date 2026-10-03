import { CORE_URL, FFMessageType } from "./const.js";
import {
  ERROR_IMPORT_FAILURE,
  ERROR_NOT_LOADED,
  ERROR_UNKNOWN_MESSAGE_TYPE,
} from "./errors.js";

let ffmpeg;
let pipeControl;
let pipeBytes;

function readPipeByte() {
  while (true) {
    const readIndex = Atomics.load(pipeControl, 0);
    const writeIndex = Atomics.load(pipeControl, 1);
    if (readIndex !== writeIndex) {
      const byte = pipeBytes[readIndex];
      Atomics.store(pipeControl, 0, (readIndex + 1) % pipeBytes.length);
      Atomics.notify(pipeControl, 0);
      return byte;
    }
    if (Atomics.load(pipeControl, 2) !== 0) return null;
    Atomics.wait(pipeControl, 1, writeIndex);
  }
}

async function load({
  coreURL: requestedCoreURL,
  wasmURL: requestedWasmURL,
  workerURL: requestedWorkerURL,
  stdinBuffer,
}) {
  const first = !ffmpeg;
  let coreURL = requestedCoreURL || CORE_URL;
  try {
    self.createFFmpegCore = (await import(/* @vite-ignore */ coreURL)).default;
    if (!self.createFFmpegCore) throw ERROR_IMPORT_FAILURE;
  } catch (error) {
    throw error || ERROR_IMPORT_FAILURE;
  }
  const wasmURL = requestedWasmURL || coreURL.replace(/.js$/g, ".wasm");
  const workerURL = requestedWorkerURL || coreURL.replace(/.js$/g, ".worker.js");
  pipeControl = stdinBuffer ? new Int32Array(stdinBuffer, 0, 4) : null;
  pipeBytes = stdinBuffer ? new Uint8Array(stdinBuffer, 16) : null;
  ffmpeg = await self.createFFmpegCore({
    mainScriptUrlOrBlob: `${coreURL}#${btoa(JSON.stringify({ wasmURL, workerURL }))}`,
    stdin: stdinBuffer ? readPipeByte : undefined,
  });
  ffmpeg.setLogger((data) => self.postMessage({ type: FFMessageType.LOG, data }));
  ffmpeg.setProgress((data) => self.postMessage({ type: FFMessageType.PROGRESS, data }));
  return first;
}

function execute({ args, timeout = -1 }) {
  ffmpeg.setTimeout(timeout);
  ffmpeg.exec(...args);
  const result = ffmpeg.ret;
  ffmpeg.reset();
  return result;
}

function respondError(id, error) {
  self.postMessage({ id, type: FFMessageType.ERROR, data: String(error) });
}

self.onmessage = async ({ data: { id, type, data } }) => {
  try {
    if (type !== FFMessageType.LOAD && !ffmpeg) throw ERROR_NOT_LOADED;
    let result;
    switch (type) {
      case FFMessageType.LOAD:
        result = await load(data);
        break;
      case FFMessageType.EXEC:
        result = execute(data);
        break;
      case FFMessageType.WRITE_FILE:
        ffmpeg.FS.writeFile(data.path, data.data);
        result = true;
        break;
      case FFMessageType.READ_FILE:
        result = ffmpeg.FS.readFile(data.path, { encoding: data.encoding });
        break;
      case FFMessageType.DELETE_FILE:
        ffmpeg.FS.unlink(data.path);
        result = true;
        break;
      default:
        throw ERROR_UNKNOWN_MESSAGE_TYPE;
    }
    const transfer = result instanceof Uint8Array ? [result.buffer] : [];
    self.postMessage({ id, type, data: result }, transfer);
  } catch (error) {
    respondError(id, error);
  }
};