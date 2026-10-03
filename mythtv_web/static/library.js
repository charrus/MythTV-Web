const pageSize = 40;
const loadedPrograms = [];
let nextStart = 0;
let totalPrograms = 0;
let activeFilter = "all";
let loading = false;

const grid = document.querySelector("#program-grid");
const searchInput = document.querySelector("#search");
const loadButton = document.querySelector("#load-more");
const emptyState = document.querySelector("#empty-state");
const syncNote = document.querySelector("#sync-note");
const resultNote = document.querySelector("#result-note");

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

function matchesFilter(program) {
  if (activeFilter === "watched") return isWatched(program);
  if (activeFilter === "unwatched") return !isWatched(program);
  return true;
}

function makeProgramCard(program, index) {
  const card = document.createElement("article");
  card.className = "program-card";
  card.style.animationDelay = `${Math.min(index % pageSize, 8) * 22}ms`;

  const poster = document.createElement("div");
  poster.className = "poster";
  const fallback = document.createElement("span");
  fallback.className = "poster-fallback";
  fallback.textContent = "M";
  poster.append(fallback);

  const imageSource = artworkUrl(program);
  if (imageSource) {
    const image = document.createElement("img");
    image.src = imageSource;
    image.alt = "";
    image.loading = "lazy";
    image.addEventListener("load", () => fallback.remove(), { once: true });
    poster.prepend(image);
  }

  if (isWatched(program)) {
    const badge = document.createElement("span");
    badge.className = "poster-badge watched";
    badge.textContent = "Watched";
    poster.append(badge);
  }

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

function renderPrograms() {
  const query = searchInput.value.trim().toLocaleLowerCase();
  const visiblePrograms = loadedPrograms.filter((program) => {
    const searchable = [
      program.Title,
      program.SubTitle,
      program.Category,
      program.Channel?.ChannelName,
    ]
      .filter(Boolean)
      .join(" ")
      .toLocaleLowerCase();
    return matchesFilter(program) && (!query || searchable.includes(query));
  });

  grid.replaceChildren(
    ...visiblePrograms.map((program, index) => makeProgramCard(program, index)),
  );
  emptyState.hidden = visiblePrograms.length > 0;
  document.querySelector("#empty-title").textContent = loadedPrograms.length
    ? "No matching recordings"
    : "No recordings found";
  document.querySelector("#empty-description").textContent = loadedPrograms.length
    ? "Try another search or change the watched filter."
    : "There are no recordings available from the MythTV backend.";
  resultNote.textContent = query
    ? `Showing ${visiblePrograms.length} matches in the ${loadedPrograms.length} loaded programs`
    : `Showing ${visiblePrograms.length} programs`;
  document.querySelector("#loaded-count").textContent =
    `${loadedPrograms.length.toLocaleString()} loaded`;
  loadButton.hidden = nextStart >= totalPrograms;
}

async function loadPrograms(append = true) {
  if (loading || (append && nextStart >= totalPrograms && loadedPrograms.length)) return;
  loading = true;
  loadButton.disabled = true;
  loadButton.textContent = "Loading…";
  grid.setAttribute("aria-busy", "true");
  if (!loadedPrograms.length) syncNote.textContent = "Loading recordings…";

  try {
    const response = await fetch(`/api/programs?start=${nextStart}&count=${pageSize}`);
    if (!response.ok) throw new Error(`Request failed (${response.status})`);
    const result = await response.json();
    loadedPrograms.push(...result.programs);
    nextStart = result.start + result.count;
    totalPrograms = result.total;
    document.querySelector("#total-count").textContent = totalPrograms.toLocaleString();
    syncNote.textContent = result.as_of
      ? `Updated ${new Intl.DateTimeFormat(undefined, {
          hour: "numeric",
          minute: "2-digit",
        }).format(new Date(result.as_of))}`
      : "Connected to MythTV";
    renderPrograms();
  } catch (error) {
    syncNote.textContent = "MythTV backend unavailable";
    emptyState.hidden = false;
    document.querySelector("#empty-title").textContent = "Can’t reach MythTV";
    document.querySelector("#empty-description").textContent =
      "Check that the local backend is running, then reload this page.";
    resultNote.textContent = error.message;
  } finally {
    loading = false;
    loadButton.disabled = false;
    loadButton.textContent = "Load more";
    loadButton.hidden = nextStart >= totalPrograms;
    grid.setAttribute("aria-busy", "false");
  }
}

searchInput.addEventListener("input", renderPrograms);
document.querySelectorAll(".filter-button").forEach((button) => {
  button.addEventListener("click", () => {
    activeFilter = button.dataset.filter;
    document.querySelectorAll(".filter-button").forEach((item) => {
      const selected = item === button;
      item.classList.toggle("selected", selected);
      item.setAttribute("aria-pressed", String(selected));
    });
    renderPrograms();
  });
});
loadButton.addEventListener("click", () => loadPrograms());
document.addEventListener("keydown", (event) => {
  if (event.key === "/" && document.activeElement !== searchInput) {
    event.preventDefault();
    searchInput.focus();
  }
});

loadPrograms(false);
