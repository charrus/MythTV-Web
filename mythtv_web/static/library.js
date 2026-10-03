let programGroups = [];
let currentPrograms = [];
let selectedGroupTitle = null;
let selectedGroupArtwork = null;
let totalRecordings = 0;
let activeFilter = "all";
let loading = false;

const grid = document.querySelector("#program-grid");
const appRoot = document.querySelector('meta[name="app-root"]')?.content.replace(/\/$/, "") || "";
const searchInput = document.querySelector("#search");
const emptyState = document.querySelector("#empty-state");
const syncNote = document.querySelector("#sync-note");
const resultNote = document.querySelector("#result-note");
const backButton = document.querySelector("#back-to-groups");
const playerDialog = document.querySelector("#player-dialog");
const playerVideo = document.querySelector("#recording-video");
const playerStatus = document.querySelector("#player-status");
const playbackSeek = document.querySelector("#playback-seek");
const smartSkip = document.querySelector("#smart-skip");
const skipAdButton = document.querySelector("#skip-ad");
let activeRecording = null;
let playbackInfo = null;
let commercialBreaks = [];
let usingTranscode = false;
let transcodeBase = 0;
let transcodeToken = null;
let subtitleElement = null;
let lastSavedPosition = -1;
let smartSkipBusy = false;

function appPath(path) {
  return `${appRoot}${path}`;
}

function formatDate(value) {
  if (!value) return "Date unknown";
  const date = new Date(value);
  if (Number.isNaN(date.getTime()) || date.getUTCFullYear() < 1980) {
    return "Date unknown";
  }
  return new Intl.DateTimeFormat(undefined, {
    day: "numeric",
    month: "short",
    year: "numeric",
    timeZone: "UTC",
  }).format(date);
}

function formatSize(bytes) {
  if (!bytes) return "";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let size = bytes;
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024;
    unitIndex += 1;
  }
  return `${size.toFixed(unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`;
}

function formatPlaybackTime(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return "0:00";
  const wholeSeconds = Math.floor(seconds);
  const hours = Math.floor(wholeSeconds / 3600);
  const minutes = Math.floor((wholeSeconds % 3600) / 60);
  const remainder = wholeSeconds % 60;
  return hours
    ? `${hours}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`
    : `${minutes}:${String(remainder).padStart(2, "0")}`;
}

function playbackDuration(program) {
  const start = Date.parse(program.StartTime || "");
  const end = Date.parse(program.EndTime || "");
  return Number.isFinite(start) && Number.isFinite(end) && end > start
    ? (end - start) / 1000
    : 0;
}

function playbackPosition() {
  return usingTranscode ? transcodeBase + playerVideo.currentTime : playerVideo.currentTime;
}

function seekTo(seconds) {
  const duration = playbackDuration(activeRecording || {});
  const position = Math.max(0, Math.min(seconds, duration || Number.MAX_SAFE_INTEGER));
  if (usingTranscode) {
    startTranscode(position);
  } else {
    playerVideo.currentTime = position;
  }
}

function secondsFromMarker(value) {
  const numeric = Number(value);
  if (Number.isFinite(numeric)) return numeric / 1000;
  const parts = String(value || "").split(":").map(Number);
  if (parts.some((part) => !Number.isFinite(part))) return NaN;
  return parts.reduce((total, part) => total * 60 + part, 0);
}

function updatePlaybackProgress() {
  const position = playbackPosition();
  const duration = playbackDuration(activeRecording || {}) || playerVideo.duration || 0;
  document.querySelector("#playback-elapsed").textContent = formatPlaybackTime(position);
  document.querySelector("#playback-duration").textContent = formatPlaybackTime(duration);
  playbackSeek.value = duration > 0 ? String(Math.round((position / duration) * 1000)) : "0";
  if (duration > 0 && Math.abs(position - lastSavedPosition) >= 15) {
    savePlaybackPosition();
  }
}

async function savePlaybackPosition() {
  if (!activeRecording) return;
  const position = playbackPosition();
  if (!Number.isFinite(position) || position < 0) return;
  lastSavedPosition = position;
  try {
    await fetch(appPath(`/api/playback/${activeRecording.Recording.RecordedId}/position`), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ position }),
    });
  } catch {
    // Resume position is best-effort; playback should continue if MythTV is offline.
  }
}

async function cancelTranscode() {
  const token = transcodeToken;
  if (!token || !activeRecording) return;
  transcodeToken = null;
  const recordedId = activeRecording.Recording.RecordedId;
  for (let attempt = 0; attempt < 4; attempt += 1) {
    try {
      const response = await fetch(
        appPath(`/api/playback/${recordedId}/transcode/cancel`),
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ token }),
          keepalive: true,
        },
      );
      if (response.ok || response.status !== 404) return;
    } catch {
      return;
    }
    await new Promise((resolve) => window.setTimeout(resolve, 100));
  }
}

function startTranscode(seconds = 0) {
  cancelTranscode();
  usingTranscode = true;
  transcodeBase = seconds;
  const playbackRate = Number(document.querySelector("#playback-speed").value) || 1;
  transcodeToken = crypto.randomUUID().replace(/-/g, "");
  playerStatus.textContent = "Preparing a browser-compatible stream…";
  playerVideo.addEventListener("loadedmetadata", function restorePlaybackRate() {
    playerVideo.removeEventListener("loadedmetadata", restorePlaybackRate);
    playerVideo.playbackRate = playbackRate;
    if ("preservesPitch" in playerVideo) playerVideo.preservesPitch = true;
  });
  playerVideo.src = appPath(`/api/playback/${activeRecording.Recording.RecordedId}/transcode?start=${encodeURIComponent(seconds)}&token=${transcodeToken}`);
  playerVideo.load();
  playerVideo.play().catch(() => {
    playerStatus.textContent = "Stream ready. Press Play to begin.";
  });
}

function handleVideoError() {
  if (!activeRecording) return;
  if (!usingTranscode) {
    startTranscode(playbackPosition());
  } else {
    playerStatus.textContent = "This recording could not be decoded by the browser.";
  }
}

async function selectSubtitle(streamIndex) {
  subtitleElement?.remove();
  subtitleElement = null;
  if (streamIndex === "" || !activeRecording) return;
  const trackInfo = playbackInfo.media.subtitles.find(
    (track) => String(track.index) === streamIndex,
  );
  if (!trackInfo) return;
  const track = document.createElement("track");
  track.kind = "subtitles";
  track.label = trackInfo.title || (
    trackInfo.language && trackInfo.language !== "und"
      ? trackInfo.language
      : `Track ${streamIndex}`
  );
  track.srclang = /^[a-z]{2,3}$/i.test(trackInfo.language) ? trackInfo.language : "und";
  track.src = appPath(`/api/playback/${activeRecording.Recording.RecordedId}/subtitles/${streamIndex}`);
  track.addEventListener("load", () => {
    track.track.mode = "showing";
    playerStatus.textContent = `Subtitles: ${track.label}`;
  });
  playerVideo.append(track);
  subtitleElement = track;
  track.track.mode = "showing";
}

async function openPlayer(program) {
  const recordedId = program.Recording?.RecordedId;
  if (!recordedId) return;
  activeRecording = program;
  playbackInfo = null;
  commercialBreaks = [];
  usingTranscode = false;
  transcodeBase = 0;
  lastSavedPosition = -1;
  document.querySelector("#player-title").textContent = program.SubTitle || program.Title;
  document.querySelector("#player-subtitle").textContent = [
    program.Title,
    episodeLabel(program),
    formatDate(program.StartTime),
  ].filter(Boolean).join(" · ");
  document.querySelector("#player-description").textContent =
    program.Description || "No program description is available.";
  document.querySelector("#subtitle-track").replaceChildren(new Option("Off", ""));
  document.querySelector("#player-details").open = false;
  playerStatus.textContent = "Loading MythTV playback details…";
  playerDialog.showModal();

  try {
    const response = await fetch(appPath(`/api/playback/${recordedId}`));
    if (!response.ok) throw new Error(`Request failed (${response.status})`);
    playbackInfo = await response.json();
    commercialBreaks = playbackInfo.comm_breaks.map((marker) => ({
      start: secondsFromMarker(marker.Start ?? marker.start),
      end: secondsFromMarker(marker.End ?? marker.end),
    })).filter((marker) => Number.isFinite(marker.start) && Number.isFinite(marker.end) && marker.end > marker.start);
    skipAdButton.disabled = commercialBreaks.length === 0;
    const subtitles = document.querySelector("#subtitle-track");
    playbackInfo.media.subtitles.forEach((track) => {
      const label = track.title || (
        track.language && track.language !== "und"
          ? track.language
          : `Track ${track.index}`
      );
      const option = new Option(
        label,
        String(track.index),
      );
      subtitles.add(option);
    });
    const resumeMilliseconds = playbackInfo.last_position || playbackInfo.bookmark || 0;
    const resumePosition = Math.max(0, resumeMilliseconds / 1000);
    const requiresTranscode = ["hevc", "eac3"].some((codec) =>
      [playbackInfo.media.video_codec, playbackInfo.media.audio_codec].includes(codec),
    );
    if (requiresTranscode) {
      startTranscode(resumePosition);
    } else {
      playerVideo.src = appPath(`/api/playback/${recordedId}/stream`);
      playerVideo.load();
      playerVideo.addEventListener("loadedmetadata", function restorePosition() {
        playerVideo.removeEventListener("loadedmetadata", restorePosition);
        if (resumePosition > 5) playerVideo.currentTime = resumePosition;
        updatePlaybackProgress();
      });
    }
    if (!requiresTranscode) playerVideo.play().then(() => {
      playerStatus.textContent = commercialBreaks.length
        ? `${commercialBreaks.length} commercial break markers loaded`
        : "No commercial break markers are available for this recording.";
    }).catch(() => {
      playerStatus.textContent = "Stream ready. Press Play to begin.";
    });
  } catch (error) {
    playerStatus.textContent = error.message;
  }
}

function closePlayer() {
  if (!activeRecording) return;
  savePlaybackPosition();
  cancelTranscode();
  playerVideo.pause();
  playerVideo.removeAttribute("src");
  playerVideo.load();
  subtitleElement?.remove();
  subtitleElement = null;
  activeRecording = null;
  playbackInfo = null;
  commercialBreaks = [];
  usingTranscode = false;
  if (playerDialog.open) playerDialog.close();
}

function episodeLabel(program) {
  const season = Number(program.Season);
  const episode = Number(program.Episode);
  if (season > 0 && episode > 0) {
    return `S${String(season).padStart(2, "0")} E${String(episode).padStart(2, "0")}`;
  }
  const match = (program.SubTitle || "").match(/\bS(\d{1,2})E(\d{1,2})\b/i);
  return match ? `S${match[1].padStart(2, "0")} E${match[2].padStart(2, "0")}` : "";
}

function isWatched(program) {
  return (program.ProgramFlagNames || "").split("|").includes("WATCHED");
}

function artworkUrl(program) {
  const artwork = program.Artwork?.ArtworkInfos?.find(
    (item) => item.Type === "coverart" || item.Type === "fanart",
  );
  return artwork?.URL
    ? appPath(`/api/artwork?url=${encodeURIComponent(artwork.URL)}`)
    : "";
}

function previewUrl(program) {
  const recordedId = program.Recording?.RecordedId;
  return Number.isInteger(recordedId) && recordedId > 0
    ? appPath(`/api/preview?recorded_id=${recordedId}`)
    : "";
}

function makePoster(sourceUrl, badgeText = "", fallbackUrl = "") {
  sourceUrl = sourceUrl || "";
  const poster = document.createElement("div");
  poster.className = "poster";
  const fallback = document.createElement("span");
  fallback.className = "poster-fallback";
  fallback.textContent = "M";
  poster.append(fallback);

  if (sourceUrl || fallbackUrl) {
    const imageSource = sourceUrl.startsWith("/api/artwork?")
      ? appPath(sourceUrl)
      : sourceUrl.startsWith("/Content/GetImageFile?")
        ? appPath(`/api/artwork?url=${encodeURIComponent(sourceUrl)}`)
        : fallbackUrl
          ? appPath(fallbackUrl)
          : "";
    if (imageSource) {
      const image = document.createElement("img");
      image.src = imageSource;
      image.alt = "";
      image.loading = "lazy";
      image.addEventListener("load", () => fallback.remove(), { once: true });
      image.addEventListener("error", () => {
        if (fallbackUrl && image.src !== new URL(fallbackUrl, location.href).href) {
          image.src = fallbackUrl;
        } else {
          image.remove();
        }
      });
      poster.prepend(image);
    }
  }

  if (badgeText) {
    const badge = document.createElement("span");
    badge.className = "poster-badge";
    badge.textContent = badgeText;
    poster.append(badge);
  }
  return poster;
}

function matchesProgramFilter(program) {
  if (activeFilter === "watched") return isWatched(program);
  if (activeFilter === "unwatched") return !isWatched(program);
  return true;
}

function matchesGroupFilter(group) {
  if (activeFilter === "watched") return group.watched_count > 0;
  if (activeFilter === "unwatched") {
    return group.watched_count < group.recording_count;
  }
  return true;
}

function makeGroupCard(group, index) {
  const card = document.createElement("article");
  card.className = "program-card group-card";
  card.style.animationDelay = `${Math.min(index % 8, 8) * 22}ms`;
  const openButton = document.createElement("button");
  openButton.className = "group-open";
  openButton.type = "button";
  openButton.setAttribute(
    "aria-label",
    `Open ${group.title}, ${group.recording_count} ${group.recording_count === 1 ? "recording" : "recordings"}`,
  );
  const badge = group.watched_count === group.recording_count ? "Watched" : "";
  openButton.append(makePoster(group.artwork_url, badge, group.preview_url));

  const info = document.createElement("div");
  info.className = "program-info";
  const channel = document.createElement("p");
  channel.className = "program-channel";
  channel.textContent = group.channel_name || group.category || "Program";
  const title = document.createElement("h2");
  title.className = "program-title";
  title.textContent = group.title;
  const subtitle = document.createElement("p");
  subtitle.className = "group-count";
  subtitle.textContent = `${group.recording_count} ${group.recording_count === 1 ? "recording" : "recordings"}`;
  const meta = document.createElement("div");
  meta.className = "program-meta";
  const latest = document.createElement("time");
  latest.dateTime = group.latest_start || "";
  latest.textContent = `Latest ${formatDate(group.latest_start)}`;
  meta.append(latest);
  info.append(channel, title, subtitle, meta);
  openButton.append(info);
  openButton.addEventListener("click", () => loadGroup(group));
  card.append(openButton);
  return card;
}

function makeProgramCard(program, index) {
  const card = document.createElement("article");
  card.className = "program-card";
  card.style.animationDelay = `${Math.min(index % 8, 8) * 22}ms`;
  const poster = makePoster(
    artworkUrl(program) || selectedGroupArtwork,
    isWatched(program) ? "Watched" : "",
    previewUrl(program),
  );

  const info = document.createElement("div");
  info.className = "program-info";
  const channel = document.createElement("p");
  channel.className = "program-channel";
  channel.textContent = program.Channel?.ChannelName || program.Category || "Recording";
  const title = document.createElement("h2");
  title.className = "program-title";
  title.textContent = program.SubTitle || program.Title || "Untitled recording";
  const subtitle = document.createElement("p");
  subtitle.className = "program-subtitle";
  const episode = episodeLabel(program);
  subtitle.textContent = episode || program.Category || " ";
  const description = document.createElement("p");
  description.className = "program-description";
  description.textContent = program.Description || "";
  const meta = document.createElement("div");
  meta.className = "program-meta";
  const date = document.createElement("time");
  date.dateTime = program.StartTime || "";
  date.textContent = formatDate(program.StartTime);
  const size = document.createElement("span");
  size.className = "program-size";
  size.textContent = formatSize(program.FileSize);
  meta.append(date, size);
  info.append(channel, title, subtitle, description, meta);
  const playButton = document.createElement("button");
  playButton.className = "recording-play";
  playButton.type = "button";
  playButton.textContent = "Play";
  playButton.setAttribute("aria-label", `Play ${program.SubTitle || program.Title}`);
  playButton.addEventListener("click", () => openPlayer(program));
  card.append(poster, info, playButton);
  return card;
}

function renderGroups() {
  const query = searchInput.value.trim().toLocaleLowerCase();
  const visibleGroups = programGroups.filter((group) => {
    const searchable = [
      group.title,
      group.category,
      group.channel_name,
    ]
      .filter(Boolean)
      .join(" ")
      .toLocaleLowerCase();
    return matchesGroupFilter(group) && (!query || searchable.includes(query));
  });

  grid.replaceChildren(
    ...visibleGroups.map((group, index) => makeGroupCard(group, index)),
  );
  emptyState.hidden = visibleGroups.length > 0;
  document.querySelector("#empty-title").textContent = "No matching programs";
  document.querySelector("#empty-description").textContent = programGroups.length
    ? "Try another search or change the watched filter."
    : "There are no recordings available from the MythTV backend.";
  const recordingsShown = visibleGroups.reduce(
    (sum, group) => sum + group.recording_count,
    0,
  );
  resultNote.textContent = query || activeFilter !== "all"
    ? `Showing ${visibleGroups.length} groups · ${recordingsShown.toLocaleString()} recordings`
    : `Showing ${visibleGroups.length} program groups`;
  document.querySelector("#loaded-count").textContent =
    `${programGroups.length.toLocaleString()} groups`;
  document.querySelector("#total-count").textContent = totalRecordings.toLocaleString();
  document.querySelector("#total-label").textContent =
    `recordings across ${programGroups.length.toLocaleString()} program groups`;
  document.querySelector("#page-title").textContent = "Recordings";
  document.querySelector("#back-to-groups").hidden = true;
  searchInput.placeholder = "Search program groups";
}

function renderGroupPrograms() {
  const query = searchInput.value.trim().toLocaleLowerCase();
  const visiblePrograms = currentPrograms.filter((program) => {
    const searchable = [
      program.Title,
      program.SubTitle,
      program.Description,
      program.Category,
      program.Channel?.ChannelName,
    ]
      .filter(Boolean)
      .join(" ")
      .toLocaleLowerCase();
    return matchesProgramFilter(program) && (!query || searchable.includes(query));
  });

  grid.replaceChildren(
    ...visiblePrograms.map((program, index) => makeProgramCard(program, index)),
  );
  emptyState.hidden = visiblePrograms.length > 0;
  document.querySelector("#empty-title").textContent = "No matching recordings";
  document.querySelector("#empty-description").textContent =
    "Try another search or change the watched filter.";
  resultNote.textContent = `Showing ${visiblePrograms.length} of ${currentPrograms.length} recordings`;
  document.querySelector("#loaded-count").textContent =
    `${currentPrograms.length.toLocaleString()} recordings in this group`;
  document.querySelector("#total-count").textContent = currentPrograms.length.toLocaleString();
  document.querySelector("#total-label").textContent = "recordings in this group";
  document.querySelector("#page-title").textContent = selectedGroupTitle;
  backButton.hidden = false;
  searchInput.placeholder = "Search recordings in this group";
}

function renderCurrentView() {
  if (selectedGroupTitle) renderGroupPrograms();
  else renderGroups();
}

async function loadGroups() {
  if (loading) return;
  loading = true;
  grid.setAttribute("aria-busy", "true");
  syncNote.textContent = "Loading program groups…";

  try {
    const response = await fetch(appPath("/api/program-groups"));
    if (!response.ok) throw new Error(`Request failed (${response.status})`);
    const result = await response.json();
    programGroups = result.groups;
    totalRecordings = result.total;
    syncNote.textContent = "Connected to MythTV";
    renderGroups();
  } catch (error) {
    syncNote.textContent = "MythTV backend unavailable";
    emptyState.hidden = false;
    document.querySelector("#empty-title").textContent = "Can’t reach MythTV";
    document.querySelector("#empty-description").textContent =
      "Check that the local backend is running, then reload this page.";
    resultNote.textContent = error.message;
  } finally {
    loading = false;
    grid.setAttribute("aria-busy", "false");
  }
}

async function loadGroup(group) {
  if (loading) return;
  loading = true;
  grid.setAttribute("aria-busy", "true");
  syncNote.textContent = "Loading recordings…";
  try {
    const response = await fetch(
      appPath(`/api/program-group?title=${encodeURIComponent(group.title)}`),
    );
    if (!response.ok) throw new Error(`Request failed (${response.status})`);
    const result = await response.json();
    selectedGroupTitle = result.title;
    selectedGroupArtwork = group.artwork_url;
    currentPrograms = result.programs;
    searchInput.value = "";
    syncNote.textContent = "Complete group";
    renderGroupPrograms();
  } catch (error) {
    syncNote.textContent = "Could not load this program group";
    resultNote.textContent = error.message;
  } finally {
    loading = false;
    grid.setAttribute("aria-busy", "false");
  }
}

function returnToGroups() {
  selectedGroupTitle = null;
  selectedGroupArtwork = null;
  currentPrograms = [];
  searchInput.value = "";
  renderGroups();
}

searchInput.addEventListener("input", renderCurrentView);
document.querySelectorAll(".filter-button").forEach((button) => {
  button.addEventListener("click", () => {
    activeFilter = button.dataset.filter;
    document.querySelectorAll(".filter-button").forEach((item) => {
      const selected = item === button;
      item.classList.toggle("selected", selected);
      item.setAttribute("aria-pressed", String(selected));
    });
    renderCurrentView();
  });
});
backButton.addEventListener("click", returnToGroups);
document.querySelector("#player-close").addEventListener("click", closePlayer);
playerDialog.addEventListener("cancel", (event) => {
  event.preventDefault();
  closePlayer();
});
document.querySelector("#player-toggle").addEventListener("click", () => {
  if (playerVideo.paused) {
    playerVideo.play().catch(() => {
      playerStatus.textContent = "Unable to start playback.";
    });
  } else {
    playerVideo.pause();
  }
});
playerVideo.addEventListener("play", () => {
  document.querySelector("#player-toggle").textContent = "Pause";
  document.querySelector("#player-toggle").setAttribute("aria-label", "Pause");
});
playerVideo.addEventListener("pause", () => {
  document.querySelector("#player-toggle").textContent = "Play";
  document.querySelector("#player-toggle").setAttribute("aria-label", "Play");
});
playerVideo.addEventListener("playing", () => {
  playerStatus.textContent = usingTranscode
    ? "Playing a browser-compatible transcode"
    : "Playing MythTV recording";
});
playerVideo.addEventListener("error", handleVideoError);
playerVideo.addEventListener("timeupdate", () => {
  updatePlaybackProgress();
  if (!smartSkip.checked || smartSkipBusy) return;
  const position = playbackPosition();
  const marker = commercialBreaks.find(
    (item) => position >= item.start && position < item.end,
  );
  if (marker) {
    smartSkipBusy = true;
    playerStatus.textContent = "Smart skip: commercial break skipped.";
    seekTo(marker.end + 0.25);
    window.setTimeout(() => { smartSkipBusy = false; }, 1500);
  }
});
playerVideo.addEventListener("ended", savePlaybackPosition);
document.querySelector("#jump-back").addEventListener("click", () => {
  seekTo(playbackPosition() - 10);
});
document.querySelector("#jump-forward").addEventListener("click", () => {
  seekTo(playbackPosition() + 30);
});
skipAdButton.addEventListener("click", () => {
  const position = playbackPosition();
  const marker = commercialBreaks.find(
    (item) => item.end > position && item.start < position + 120,
  );
  if (marker) seekTo(marker.end + 0.25);
});
playbackSeek.addEventListener("input", () => {
  const duration = playbackDuration(activeRecording || {}) || playerVideo.duration || 0;
  document.querySelector("#playback-elapsed").textContent = formatPlaybackTime(
    (Number(playbackSeek.value) / 1000) * duration,
  );
});
playbackSeek.addEventListener("change", () => {
  const duration = playbackDuration(activeRecording || {}) || playerVideo.duration || 0;
  seekTo((Number(playbackSeek.value) / 1000) * duration);
});
document.querySelector("#playback-speed").addEventListener("change", (event) => {
  playerVideo.playbackRate = Number(event.currentTarget.value);
  if ("preservesPitch" in playerVideo) playerVideo.preservesPitch = true;
});
document.querySelector("#subtitle-track").addEventListener("change", (event) => {
  selectSubtitle(event.currentTarget.value);
});
document.querySelector("#player-fullscreen").addEventListener("click", () => {
  if (document.fullscreenElement) document.exitFullscreen();
  else playerVideo.requestFullscreen?.();
});
document.addEventListener("keydown", (event) => {
  if (playerDialog.open && !["INPUT", "SELECT", "BUTTON"].includes(document.activeElement.tagName)) {
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      seekTo(playbackPosition() - 10);
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      seekTo(playbackPosition() + 30);
    } else if (event.key === " ") {
      event.preventDefault();
      document.querySelector("#player-toggle").click();
    }
  } else if (event.key === "/" && document.activeElement !== searchInput) {
    event.preventDefault();
    searchInput.focus();
  } else if (event.key === "Escape" && selectedGroupTitle) {
    returnToGroups();
  }
});

loadGroups();
