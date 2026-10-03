import { CORE_URL, FFMessageType } from "./const.js";
import {
  ERROR_IMPORT_FAILURE,
  ERROR_NOT_LOADED,
  ERROR_UNKNOWN_MESSAGE_TYPE,
} from "./errors.js";

let ffmpeg;
let pipeControl;
let pipeBytes;
let pipeReadBuffer;
let pipeReadOffset = 0;
let pipeReadLength = 0;
let outputControl;
let outputBytes;
let outputBatch;
let outputBatchLength = 0;
const PIPE_READ_BATCH_BYTES = 64 * 1024;
const PIPE_WRITE_BATCH_BYTES = 8 * 1024;

function readPipeByte() {
  if (pipeReadOffset < pipeReadLength) {
    return pipeReadBuffer[pipeReadOffset++];
  }

    while (true) {
    const readIndex = Atomics.load(pipeControl, 0);
    const writeIndex = Atomics.load(pipeControl, 1);
    if (readIndex !== writeIndex) {
      const available = (writeIndex - readIndex + pipeBytes.length) % pipeBytes.length;
      pipeReadLength = Math.min(available, pipeBytes.length - readIndex, pipeReadBuffer.length);
      pipeReadBuffer.set(pipeBytes.subarray(readIndex, readIndex + pipeReadLength));
      Atomics.store(pipeControl, 0, (readIndex + pipeReadLength) % pipeBytes.length);
      Atomics.notify(pipeControl, 0);
      pipeReadOffset = 1;
      return pipeReadBuffer[0];
    }
    if (Atomics.load(pipeControl, 2) !== 0) return null;
    Atomics.wait(pipeControl, 1, writeIndex);
  }
}

function writePipeByte(byte) {
  if (Atomics.load(outputControl, 3) !== 0) return;
  outputBatch[outputBatchLength++] = byte;
  if (outputBatchLength === outputBatch.length) flushOutputBatch();
}

function flushOutputBatch() {
  let offset = 0;
  while (offset < outputBatchLength) {
    if (Atomics.load(outputControl, 3) !== 0) {
      outputBatchLength = 0;
      return;
    }
  while (true) {
      const readIndex = Atomics.load(outputControl, 0);
      const writeIndex = Atomics.load(outputControl, 1);
      const available = (readIndex - writeIndex - 1 + outputBytes.length) % outputBytes.length;
      if (available) {
        const count = Math.min(
          available,
          outputBatchLength - offset,
          outputBytes.length - writeIndex,
        );
        outputBytes.set(outputBatch.subarray(offset, offset + count), writeIndex);
        offset += count;
        Atomics.store(outputControl, 1, (writeIndex + count) % outputBytes.length);
        Atomics.notify(outputControl, 1);
        break;
      }
      if (Atomics.load(outputControl, 3) !== 0) {
        outputBatchLength = 0;
        return;
      }
      Atomics.wait(outputControl, 0, readIndex);
    }
  }
  outputBatchLength = 0;
}

async function load({
  coreURL: requestedCoreURL,
  wasmURL: requestedWasmURL,
  workerURL: requestedWorkerURL,
  stdinBuffer,
  stdoutBuffer,
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
  pipeReadBuffer = stdinBuffer ? new Uint8Array(PIPE_READ_BATCH_BYTES) : null;
  pipeReadOffset = 0;
  pipeReadLength = 0;
  outputControl = stdoutBuffer ? new Int32Array(stdoutBuffer, 0, 4) : null;
  outputBytes = stdoutBuffer ? new Uint8Array(stdoutBuffer, 16) : null;
  outputBatch = stdoutBuffer ? new Uint8Array(PIPE_WRITE_BATCH_BYTES) : null;
  outputBatchLength = 0;
  ffmpeg = await self.createFFmpegCore({
    mainScriptUrlOrBlob: `${coreURL}#${btoa(JSON.stringify({ wasmURL, workerURL }))}`,
    stdin: stdinBuffer ? readPipeByte : undefined,
    stdout: stdoutBuffer ? writePipeByte : undefined,
  });
  ffmpeg.setLogger((data) => self.postMessage({ type: FFMessageType.LOG, data }));
  ffmpeg.setProgress((data) => self.postMessage({ type: FFMessageType.PROGRESS, data }));
  return first;
}

function execute({ args, timeout = -1 }) {
  ffmpeg.setTimeout(timeout);
  try {
    ffmpeg.exec(...args);
    return ffmpeg.ret;
  } finally {
    if (outputControl) flushOutputBatch();
    ffmpeg.reset();
    if (outputControl) {
      Atomics.store(outputControl, 2, 1);
      Atomics.notify(outputControl, 1);
    }
  }
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
    if (type === FFMessageType.EXEC && outputControl) {
      Atomics.store(outputControl, 2, 1);
      Atomics.notify(outputControl, 1);
    }
    respondError(id, error);
  }
};