import { FFmpeg } from "@ffmpeg/ffmpeg";

const MAX_SAMPLE_BYTES = 16 * 1024 * 1024;
const SAMPLE_SECONDS = 15;
const ffmpeg = new FFmpeg();
const pipeFFmpeg = new FFmpeg();
const PIPE_BUFFER_BYTES = 1024 * 1024;
const PIPE_HEADER_BYTES = 16;
let loadPromise = null;
let pipeLoadPromise = null;
let statusSink = () => {};
let lastError = "";

ffmpeg.on("log", ({ message }) => {
  if (message) lastError = message;
});
ffmpeg.on("progress", ({ progress }) => {
  statusSink(`Browser conversion: ${Math.round(progress * 100)}%`);
});
pipeFFmpeg.on("log", ({ message }) => {
  if (message) lastError = message;
});
pipeFFmpeg.on("progress", ({ progress }) => {
  statusSink(`Browser conversion: ${Math.round(progress * 100)}%`);
});

export async function convertSample({
  sourceURL,
  sourceSize,
  duration,
  coreURL,
  wasmURL,
  workerURL,
  subtitleIndex,
  onStatus,
}) {
  const estimatedBytes = Math.ceil(
    (sourceSize / duration) * SAMPLE_SECONDS * 1.5 + 1024 * 1024,
  );
  const rangeBytes = Math.min(
    MAX_SAMPLE_BYTES,
    Math.max(1024 * 1024, estimatedBytes),
  );
  onStatus(`Requesting a bounded ${Math.round(rangeBytes / 1024 / 1024)} MB sample…`);
  const response = await fetch(sourceURL, {
    headers: { Range: `bytes=0-${rangeBytes - 1}` },
    cache: "no-store",
  });
  if (response.status !== 206) {
    throw new Error("MythTV did not honor the bounded byte-range request");
  }

  const input = new Uint8Array(await response.arrayBuffer());
  if (!input.byteLength || input.byteLength > MAX_SAMPLE_BYTES) {
    throw new Error("The browser sample exceeded its configured size limit");
  }

  statusSink = onStatus;
  if (!ffmpeg.loaded) {
    loadPromise ??= ffmpeg.load({ classWorkerURL: workerURL, coreURL, wasmURL })
      .catch((error) => {
        loadPromise = null;
        throw error;
      });
    onStatus("Loading the FFmpeg.wasm worker and core…");
    await loadPromise;
  }

  onStatus(`Converting up to ${SAMPLE_SECONDS} seconds on this device…`);
  await ffmpeg.writeFile("recording.media", input);
  lastError = "";
  const exitCode = await ffmpeg.exec([
      "-hide_banner",
      "-loglevel",
      "error",
      "-i",
      "recording.media",
      "-t",
      String(SAMPLE_SECONDS),
      "-map",
      "0:v:0?",
      "-map",
      "0:a:0?",
      "-c:v",
      "libx264",
      "-preset",
      "ultrafast",
      "-crf",
      "28",
      "-pix_fmt",
      "yuv420p",
      "-c:a",
      "aac",
      "-b:a",
      "160k",
      "-movflags",
      "+faststart",
      "sample.mp4",
  ]);
  if (exitCode !== 0) {
    throw new Error(lastError || `FFmpeg exited with status ${exitCode}`);
  }
  const output = await ffmpeg.readFile("sample.mp4");
  let subtitles = null;
  let subtitleStatus = "No subtitle track was included";
  if (Number.isInteger(subtitleIndex)) {
    onStatus("Trying browser-side subtitle extraction…");
    lastError = "";
    const subtitleExitCode = await ffmpeg.exec([
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "recording.media",
        "-t",
        String(SAMPLE_SECONDS),
        "-map",
        `0:${subtitleIndex}`,
        "-c:s",
        "webvtt",
        "sample.vtt",
    ]);
    if (subtitleExitCode === 0) {
      const subtitleData = await ffmpeg.readFile("sample.vtt");
      if (subtitleData.byteLength > 0) {
        subtitles = new Blob([subtitleData], { type: "text/vtt" });
        subtitleStatus = "Subtitles converted to WebVTT";
      } else {
        subtitleStatus = lastError || "No subtitle cues were present in the sample";
      }
    } else {
      subtitleStatus = lastError || "The subtitle codec could not be converted";
    }
  }
  onStatus(subtitles
    ? "Browser video and subtitle conversion complete"
    : `Browser video converted; subtitles: ${subtitleStatus}`);
  return { video: new Blob([output], { type: "video/mp4" }), subtitles, subtitleStatus };
}

export async function convertPipeSample({
  sourceURL,
  coreURL,
  wasmURL,
  workerURL,
  onStatus,
}) {
  if (!globalThis.crossOriginIsolated || typeof SharedArrayBuffer === "undefined") {
    throw new Error("The page must be cross-origin isolated to enable the streaming input pipe");
  }
  const sharedBuffer = new SharedArrayBuffer(PIPE_HEADER_BYTES + PIPE_BUFFER_BYTES);
  const control = new Int32Array(sharedBuffer, 0, 4);
  const ring = new Uint8Array(sharedBuffer, PIPE_HEADER_BYTES);

  statusSink = onStatus;
  if (!pipeFFmpeg.loaded) {
    pipeLoadPromise ??= pipeFFmpeg.load({
      classWorkerURL: workerURL,
      coreURL,
      wasmURL,
      stdinBuffer: sharedBuffer,
    })
      .catch((error) => {
        pipeLoadPromise = null;
        throw error;
      });
    onStatus("Loading the FFmpeg.wasm worker and core…");
    await pipeLoadPromise;
  }

  onStatus("Streaming recording bytes into the FFmpeg.wasm input pipe…");
  const response = await fetch(sourceURL, { cache: "no-store" });
  if (!response.ok || !response.body) {
    throw new Error(`Recording stream request failed (${response.status})`);
  }
  const reader = response.body.getReader();
  let receivedBytes = 0;
  let producerError = null;
  let stopPump = false;
  const pumpInput = async () => {
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        let offset = 0;
        while (offset < value.length) {
          if (stopPump) return;
          const readIndex = Atomics.load(control, 0);
          const writeIndex = Atomics.load(control, 1);
          const available = (readIndex - writeIndex - 1 + ring.length) % ring.length;
          if (available === 0) {
            await new Promise((resolve) => setTimeout(resolve, 4));
            continue;
          }
          const count = Math.min(available, value.length - offset, ring.length - writeIndex);
          ring.set(value.subarray(offset, offset + count), writeIndex);
          offset += count;
          receivedBytes += count;
          Atomics.store(control, 1, (writeIndex + count) % ring.length);
          Atomics.notify(control, 1);
          onStatus(`Streaming into WASM: ${(receivedBytes / 1024 / 1024).toFixed(1)} MB`);
        }
      }
    } catch (error) {
      producerError = error;
    } finally {
      Atomics.store(control, 2, producerError ? 2 : 1);
      Atomics.notify(control, 1);
    }
  };
  const pumpPromise = pumpInput();
  await pipeFFmpeg.deleteFile("pipe-sample.mp4").catch(() => {});
  lastError = "";
  let exitCode;
  try {
    onStatus("Converting the streamed input to a 15-second browser sample…");
    exitCode = await pipeFFmpeg.exec([
      "-hide_banner",
      "-loglevel",
      "error",
      "-i",
      "pipe:0",
      "-t",
      String(SAMPLE_SECONDS),
      "-map",
      "0:v:0?",
      "-map",
      "0:a:0?",
      "-c:v",
      "libx264",
      "-preset",
      "ultrafast",
      "-crf",
      "28",
      "-pix_fmt",
      "yuv420p",
      "-c:a",
      "aac",
      "-b:a",
      "160k",
      "-movflags",
      "+faststart",
      "pipe-sample.mp4",
    ]);
  } finally {
    stopPump = true;
    Atomics.store(control, 2, 1);
    Atomics.notify(control, 1);
    await reader.cancel().catch(() => {});
    await pumpPromise;
  }
  if (producerError) {
    throw new Error(`Recording stream failed: ${producerError.message}`);
  }
  if (exitCode !== 0) {
    throw new Error(lastError || `FFmpeg exited with status ${exitCode}`);
  }
  const output = await pipeFFmpeg.readFile("pipe-sample.mp4");
  onStatus(`WASM pipe conversion complete; streamed ${(receivedBytes / 1024 / 1024).toFixed(1)} MB`);
  return { video: new Blob([output], { type: "video/mp4" }), receivedBytes };
}