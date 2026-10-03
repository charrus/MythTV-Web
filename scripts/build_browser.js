const fs = require("node:fs");
const path = require("node:path");
const { build } = require("esbuild");

const root = path.resolve(__dirname, "..");
const staticRoot = path.join(root, "mythtv_web", "static");
const vendorRoot = path.join(staticRoot, "vendor");
const ffmpegRoot = path.join(
  root,
  "node_modules",
  "@ffmpeg",
  "ffmpeg",
  "dist",
  "esm",
);
const coreRoot = path.join(
  root,
  "node_modules",
  "@ffmpeg",
  "core",
  "dist",
  "esm",
);

async function main() {
  fs.mkdirSync(vendorRoot, { recursive: true });
  for (const [sourceRoot, files] of [
    [ffmpegRoot, ["worker.js", "const.js", "errors.js"]],
    [coreRoot, ["ffmpeg-core.js", "ffmpeg-core.wasm"]],
  ]) {
    for (const filename of files) {
      fs.copyFileSync(
        path.join(sourceRoot, filename),
        path.join(vendorRoot, filename),
      );
    }
  }
  fs.copyFileSync(
    path.join(__dirname, "browser_ffmpeg_worker.js"),
    path.join(vendorRoot, "worker.js"),
  );

  await build({
    entryPoints: [path.join(staticRoot, "library.js")],
    bundle: true,
    format: "esm",
    splitting: true,
    outdir: path.join(staticRoot, "build"),
    entryNames: "library",
    chunkNames: "chunks/[name]-[hash]",
    platform: "browser",
    target: ["es2020"],
  });
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});