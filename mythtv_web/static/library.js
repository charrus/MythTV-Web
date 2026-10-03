let programGroups = [];
let currentPrograms = [];
let selectedGroupTitle = null;
let totalRecordings = 0;
let activeFilter = "all";
let loading = false;

const grid = document.querySelector("#program-grid");
const searchInput = document.querySelector("#search");
const emptyState = document.querySelector("#empty-state");
const syncNote = document.querySelector("#sync-note");
const resultNote = document.querySelector("#result-note");
const backButton = document.querySelector("#back-to-groups");

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

function isWatched(program) {
  return (program.ProgramFlagNames || "").split("|").includes("WATCHED");
}

function artworkUrl(program) {
  const artwork = program.Artwork?.ArtworkInfos?.find(
    (item) => item.Type === "coverart" || item.Type === "fanart",
  );
  return artwork?.URL
    ? `/api/artwork?url=${encodeURIComponent(artwork.URL)}`
    : "";
}

function makePoster(sourceUrl, badgeText = "") {
  const poster = document.createElement("div");
  poster.className = "poster";
  const fallback = document.createElement("span");
  fallback.className = "poster-fallback";
  fallback.textContent = "M";
  poster.append(fallback);

  if (sourceUrl) {
    const image = document.createElement("img");
    image.src = sourceUrl.startsWith("/")
      ? `/api/artwork?url=${encodeURIComponent(sourceUrl)}`
      : sourceUrl;
    image.alt = "";
    image.loading = "lazy";
    image.addEventListener("load", () => fallback.remove(), { once: true });
    poster.prepend(image);
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
  openButton.append(makePoster(group.artwork_url, badge));

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
  openButton.addEventListener("click", () => loadGroup(group.title));
  card.append(openButton);
  return card;
}

function makeProgramCard(program, index) {
  const card = document.createElement("article");
  card.className = "program-card";
  card.style.animationDelay = `${Math.min(index % 8, 8) * 22}ms`;
  const poster = makePoster(
    artworkUrl(program),
    isWatched(program) ? "Watched" : "",
  );

  const info = document.createElement("div");
  info.className = "program-info";
  const channel = document.createElement("p");
  channel.className = "program-channel";
  channel.textContent = program.Channel?.ChannelName || program.Category || "Recording";
  const title = document.createElement("h2");
  title.className = "program-title";
  title.textContent = program.Title || "Untitled recording";
  const subtitle = document.createElement("p");
  subtitle.className = "program-subtitle";
  subtitle.textContent = program.SubTitle || program.Category || " ";
  const meta = document.createElement("div");
  meta.className = "program-meta";
  const date = document.createElement("time");
  date.dateTime = program.StartTime || "";
  date.textContent = formatDate(program.StartTime);
  const size = document.createElement("span");
  size.className = "program-size";
  size.textContent = formatSize(program.FileSize);
  meta.append(date, size);
  info.append(channel, title, subtitle, meta);
  card.append(poster, info);
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
    const response = await fetch("/api/program-groups");
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

async function loadGroup(title) {
  if (loading) return;
  loading = true;
  grid.setAttribute("aria-busy", "true");
  syncNote.textContent = "Loading recordings…";
  try {
    const response = await fetch(
      `/api/program-group?title=${encodeURIComponent(title)}`,
    );
    if (!response.ok) throw new Error(`Request failed (${response.status})`);
    const result = await response.json();
    selectedGroupTitle = result.title;
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
document.addEventListener("keydown", (event) => {
  if (event.key === "/" && document.activeElement !== searchInput) {
    event.preventDefault();
    searchInput.focus();
  } else if (event.key === "Escape" && selectedGroupTitle) {
    returnToGroups();
  }
});

loadGroups();
