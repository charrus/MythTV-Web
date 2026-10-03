import { FFmpeg } from "@ffmpeg/ffmpeg";

const INPUT_BUFFER_BYTES = 1024 * 1024;
const OUTPUT_BUFFER_BYTES = 4 * 1024 * 1024;
const BUFFER_HEADER_BYTES = 16;
const OUTPUT_CHUNK_BYTES = 256 * 1024;
const OUTPUT_MIME = 'video/mp4; codecs="avc1.42C028, mp4a.40.2"';

function waitForSourceOpen(mediaSource) {
  if (mediaSource.readyState === "open") return Promise.resolve();
  return new Promise((resolve, reject) => {
    mediaSource.addEventListener("sourceopen", resolve, { once: true });
    mediaSource.addEventListener("sourceclose", () => reject(new Error("MediaSource closed before playback started")), { once: true });
  });
}

function appendBuffer(sourceBuffer, bytes) {
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      sourceBuffer.removeEventListener("updateend", onDone);
      sourceBuffer.removeEventListener("error", onError);
    };
    const onDone = () => {
      cleanup();
      resolve();
    };
    const onError = () => {
      cleanup();
      reject(new Error("The browser rejected a converted media segment"));
    };
    sourceBuffer.addEventListener("updateend", onDone, { once: true });
    sourceBuffer.addEventListener("error", onError, { once: true });
    try {
      sourceBuffer.appendBuffer(bytes);
    } catch (error) {
      cleanup();
      reject(error);
    }
  });
}

function removeOldBuffer(sourceBuffer, video) {
  if (sourceBuffer.updating || !sourceBuffer.buffered.length || video.currentTime < 90) {
    return Promise.resolve();
  }
  const removeUntil = video.currentTime - 60;
  const start = sourceBuffer.buffered.start(0);
  if (removeUntil <= start) return Promise.resolve();
  return new Promise((resolve) => {
    sourceBuffer.addEventListener("updateend", resolve, { once: true });
    try {
      sourceBuffer.remove(start, removeUntil);
    } catch {
      sourceBuffer.removeEventListener("updateend", resolve);
      resolve();
    }
  });
}

export async function startWasmPlayback({
  sourceURL,
  coreURL,
  wasmURL,
  workerURL,
  video,
  startSeconds = 0,
  sourceVideoCodec,
  sourceVideoProfile,
  sourceVideoLevel,
  sourceVideoPixelFormat,
  onStatus,
}) {
  if (!globalThis.crossOriginIsolated || typeof SharedArrayBuffer === "undefined") {
    throw new Error("The browser page is not cross-origin isolated");
  }
  const sourceH264Mime = `video/mp4; codecs="avc1.6400${Number(sourceVideoLevel).toString(16).padStart(2, "0")}, mp4a.40.2"`;
  const copyH264Video = sourceVideoCodec === "h264"
    && sourceVideoProfile === "High"
    && sourceVideoPixelFormat === "yuv420p"
    && Number.isInteger(sourceVideoLevel)
    && MediaSource.isTypeSupported(sourceH264Mime);
  const outputMime = copyH264Video ? sourceH264Mime : OUTPUT_MIME;
  if (!MediaSource.isTypeSupported(outputMime)) {
    throw new Error("This browser cannot play the WASM output format");
  }

  const inputBuffer = new SharedArrayBuffer(BUFFER_HEADER_BYTES + INPUT_BUFFER_BYTES);
  const outputBuffer = new SharedArrayBuffer(BUFFER_HEADER_BYTES + OUTPUT_BUFFER_BYTES);
  const inputControl = new Int32Array(inputBuffer, 0, 4);
  const inputBytes = new Uint8Array(inputBuffer, BUFFER_HEADER_BYTES);
  const outputControl = new Int32Array(outputBuffer, 0, 4);
  const outputBytes = new Uint8Array(outputBuffer, BUFFER_HEADER_BYTES);
  const ffmpeg = new FFmpeg();
  const mediaSource = new MediaSource();
  const objectURL = URL.createObjectURL(mediaSource);
  const abortController = new AbortController();
  let reader = null;
  let cancelled = false;
  let producerError = null;
  let receivedBytes = 0;
  let sentBytes = 0;

  ffmpeg.on("log", ({ message }) => {
    if (message) onStatus(message);
  });
  ffmpeg.on("progress", ({ progress }) => {
    if (Number.isFinite(progress) && progress >= 0 && progress <= 1) {
      onStatus(`WASM is converting the live stream (${Math.round(progress * 100)}%)`);
    }
  });

  video.src = objectURL;
  video.load();
  try {
    await waitForSourceOpen(mediaSource);
    const sourceBuffer = mediaSource.addSourceBuffer(outputMime);
    onStatus("Loading browser video converter…");
    await ffmpeg.load({
      classWorkerURL: workerURL,
      coreURL,
      wasmURL,
      stdinBuffer: inputBuffer,
      stdoutBuffer: outputBuffer,
    });

    const response = await fetch(sourceURL, {
      cache: "no-store",
      signal: abortController.signal,
    });
    if (!response.ok || !response.body) {
      throw new Error(`Recording stream request failed (${response.status})`);
    }
    reader = response.body.getReader();

    const pumpInput = async () => {
      try {
        while (!cancelled) {
          const { done, value } = await reader.read();
          if (done) break;
          let offset = 0;
          while (offset < value.length && !cancelled) {
            const readIndex = Atomics.load(inputControl, 0);
            const writeIndex = Atomics.load(inputControl, 1);
            const available = (readIndex - writeIndex - 1 + inputBytes.length) % inputBytes.length;
            if (!available) {
              await new Promise((resolve) => setTimeout(resolve, 4));
              continue;
            }
            const count = Math.min(available, value.length - offset, inputBytes.length - writeIndex);
            inputBytes.set(value.subarray(offset, offset + count), writeIndex);
            offset += count;
            receivedBytes += count;
            Atomics.store(inputControl, 1, (writeIndex + count) % inputBytes.length);
            Atomics.notify(inputControl, 1);
            if (receivedBytes % (4 * 1024 * 1024) < count) {
              onStatus(`Reading recording: ${(receivedBytes / 1024 / 1024).toFixed(0)} MB`);
            }
          }
        }
      } catch (error) {
        if (!cancelled) producerError = error;
      } finally {
        Atomics.store(inputControl, 2, 1);
        Atomics.notify(inputControl, 1);
      }
    };

    const consumeOutput = async () => {
      while (true) {
        const readIndex = Atomics.load(outputControl, 0);
        const writeIndex = Atomics.load(outputControl, 1);
        if (readIndex === writeIndex) {
          if (Atomics.load(outputControl, 2) !== 0) break;
          await new Promise((resolve) => setTimeout(resolve, 4));
          continue;
        }
        const available = (writeIndex - readIndex + outputBytes.length) % outputBytes.length;
        const count = Math.min(available, OUTPUT_CHUNK_BYTES, outputBytes.length - readIndex);
        const chunk = outputBytes.slice(readIndex, readIndex + count);
        Atomics.store(outputControl, 0, (readIndex + count) % outputBytes.length);
        Atomics.notify(outputControl, 0);
        await appendBuffer(sourceBuffer, chunk);
        sentBytes += count;
        await removeOldBuffer(sourceBuffer, video);
      }
    };

    const pumpPromise = pumpInput();
    const outputPromise = consumeOutput();
    const videoArgs = copyH264Video
      ? ["-c:v", "copy"]
      : [
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "28",
        "-profile:v",
        "baseline",
        "-level:v",
        "4.0",
        "-pix_fmt",
        "yuv420p",
        "-force_key_frames",
        "expr:gte(t,n_forced*2)",
      ];
    const conversionPromise = ffmpeg.exec([
      "-hide_banner",
      "-loglevel",
      "error",
      "-i",
      "pipe:0",
      "-ss",
      String(Math.max(0, startSeconds)),
      "-map",
      "0:v:0?",
      "-map",
      "0:a:0?",
      ...videoArgs,
      "-c:a",
      "aac",
      "-b:a",
      "160k",
      "-movflags",
      "+frag_keyframe+empty_moov+default_base_moof",
      "-f",
      "mp4",
      "pipe:1",
    ]);

    const done = Promise.all([pumpPromise, outputPromise, conversionPromise])
      .then(([, , exitCode]) => {
        if (producerError) throw new Error(`Recording stream failed: ${producerError.message}`);
        if (exitCode !== 0) throw new Error(`WASM conversion exited with status ${exitCode}`);
        if (mediaSource.readyState === "open") mediaSource.endOfStream();
        onStatus(`Browser conversion complete (${(receivedBytes / 1024 / 1024).toFixed(1)} MB read)`);
      })
      .catch((error) => {
        if (!cancelled) onStatus(`Browser conversion failed: ${error.message}`);
        throw error;
      });
    done.catch(() => {});

    return {
      objectURL,
      done,
      get receivedBytes() { return receivedBytes; },
      get sentBytes() { return sentBytes; },
      cancel() {
        if (cancelled) return;
        cancelled = true;
        abortController.abort();
        reader?.cancel().catch(() => {});
        Atomics.store(inputControl, 2, 1);
        Atomics.notify(inputControl, 1);
        Atomics.store(outputControl, 3, 1);
        Atomics.store(outputControl, 2, 1);
        Atomics.notify(outputControl, 0);
        Atomics.notify(outputControl, 1);
        ffmpeg.terminate();
        URL.revokeObjectURL(objectURL);
      },
    };
  } catch (error) {
    abortController.abort();
    reader?.cancel().catch(() => {});
    ffmpeg.terminate();
    URL.revokeObjectURL(objectURL);
    if (video.src === objectURL) {
      video.removeAttribute("src");
      video.load();
    }
    throw error;
  }
}