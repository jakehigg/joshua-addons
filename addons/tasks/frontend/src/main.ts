import Sortable from "sortablejs";
import {
  api,
  clientId,
  TASK_SIZES,
  type Group,
  type Project,
  type ProjectStatus,
  type SearchResult,
  type Task,
  type TaskPatch,
  type TaskSize,
} from "./api";
import {
  dashboardMessage,
  dashboardProjects,
  finishedProjects,
  homeHref,
  parseRoute,
  projectCardView,
  projectHref,
  searchView,
  taskLinksView,
  type CardView,
  type Route,
  type SearchTaskView,
  type TaskLink,
} from "./dashboard";

const projectSelect = document.getElementById("project-select") as HTMLSelectElement;
const searchLink = document.getElementById("search-link") as HTMLAnchorElement;
const addProjectBtn = document.getElementById("add-project") as HTMLButtonElement;
const createFirstBtn = document.getElementById("create-first-project") as HTMLButtonElement;
const newProjectForm = document.getElementById("new-project-form") as HTMLFormElement;
const newProjectInput = document.getElementById("new-project-input") as HTMLInputElement;
const newProjectGoal = document.getElementById("new-project-goal") as HTMLInputElement;
const projectGoal = document.getElementById("project-goal") as HTMLSpanElement;
const progressFill = document.getElementById("progress-fill") as HTMLDivElement;
const progressText = document.getElementById("progress-text") as HTMLSpanElement;
const projectHealth = document.getElementById("project-health") as HTMLSpanElement;
const projectTarget = document.getElementById("project-target") as HTMLInputElement;
const projectStatus = document.getElementById("project-status") as HTMLSelectElement;
const completeBtn = document.getElementById("complete-project") as HTMLButtonElement;
const backlogHeader = document.querySelector(".backlog-header") as HTMLElement;
const newProjectCancel = document.getElementById("new-project-cancel") as HTMLButtonElement;
const main = document.getElementById("main") as HTMLElement;
const emptyProjects = document.getElementById("empty-projects") as HTMLElement;
const emptyTasks = document.getElementById("empty-tasks") as HTMLElement;
const addTaskForm = document.getElementById("add-task-form") as HTMLFormElement;
const addTaskInput = document.getElementById("add-task-input") as HTMLInputElement;
const taskList = document.getElementById("task-list") as HTMLUListElement;
const groupList = document.getElementById("group-list") as HTMLDivElement;
const addGroupBtn = document.getElementById("add-group") as HTMLButtonElement;
const newGroupForm = document.getElementById("new-group-form") as HTMLFormElement;
const newGroupInput = document.getElementById("new-group-input") as HTMLInputElement;
const newGroupCancel = document.getElementById("new-group-cancel") as HTMLButtonElement;
const dashboard = document.getElementById("dashboard") as HTMLElement;
const dashboardProjectList = document.getElementById("dashboard-projects") as HTMLElement;
const projectCards = document.getElementById("project-cards") as HTMLElement;
const dashboardNote = document.getElementById("dashboard-message") as HTMLParagraphElement;
const finishedSection = document.getElementById("finished-projects") as HTMLElement;
const finishedCards = document.getElementById("finished-project-cards") as HTMLElement;
const searchForm = document.getElementById("search-form") as HTMLFormElement;
const searchInput = document.getElementById("search-input") as HTMLInputElement;
const searchDone = document.getElementById("search-done") as HTMLInputElement;
const searchResults = document.getElementById("search-results") as HTMLElement;
const searchNote = document.getElementById("search-message") as HTMLParagraphElement;
const foundProjects = document.getElementById("found-projects") as HTMLElement;
const foundProjectCards = document.getElementById("found-project-cards") as HTMLElement;
const foundTasks = document.getElementById("found-tasks") as HTMLElement;
const foundTaskList = document.getElementById("found-task-list") as HTMLUListElement;

let projects: Project[] = [];
let currentProjectId: string | null = null;
// "/" is the dashboard, "/p/<slug>" is one project; see dashboard.ts.
let route: Route = parseRoute(location.pathname + location.search);

function currentProject(): Project | undefined {
  return projects.find((p) => p.id === currentProjectId);
}

// The server computes percent, health, and the next task. After a change
// this tab made (a done toggle, an add, a delete) the summary is stale, so
// refetch the project list only, without rerendering the tasks.
let summaryTimer: number | undefined;
function scheduleSummaryRefresh() {
  clearTimeout(summaryTimer);
  summaryTimer = window.setTimeout(async () => {
    try {
      projects = await api.listProjects();
      renderProjects();
      renderProjectHeader();
    } catch {
      /* the next event or visibility change retries */
    }
  }, 300);
}

// --- live updates -------------------------------------------------------------
// Changes made elsewhere (Joshua over MCP, another device) arrive as SSE
// events; this tab refetches only when the event is for what it's showing,
// debounced, and skips events it caused itself (origin === clientId).
// Mobile browsers kill SSE in background tabs, so returning to the tab
// (visibilitychange) and every SSE reconnect also trigger a catch-up fetch.

let dragging = false;
let pendingWhileDragging = false;
let refreshTimer: number | undefined;
let refreshProjects = false;

function scheduleRefresh(includeProjects: boolean) {
  refreshProjects = refreshProjects || includeProjects;
  clearTimeout(refreshTimer);
  refreshTimer = window.setTimeout(runRefresh, 250);
}

async function runRefresh() {
  if (dragging) {
    pendingWhileDragging = true;
    return;
  }
  const withProjects = refreshProjects;
  refreshProjects = false;
  try {
    // The dashboard is one view of the project list, so any change refetches it.
    if (!currentProjectId) {
      await loadProjects();
      await runSearch();
    } else if (withProjects) await loadProjects();
    else await loadTasks();
  } catch {
    /* transient — the next event or visibility change retries */
  }
}

function dragDone() {
  dragging = false;
  if (pendingWhileDragging) {
    pendingWhileDragging = false;
    scheduleRefresh(false);
  }
}

let sseEverOpened = false;
function connectEvents() {
  const es = new EventSource("/api/events");
  es.onopen = () => {
    // After a reconnect (not the initial open), catch up on missed events.
    if (sseEverOpened) scheduleRefresh(true);
    sseEverOpened = true;
  };
  es.onmessage = (e) => {
    const ev = JSON.parse(e.data) as {
      scope: string;
      project_id: string | null;
      origin: string | null;
    };
    if (ev.origin === clientId) return; // our own write — DOM already updated
    // On the dashboard every change can move a card, so none is ignored.
    if (ev.scope === "projects" || !currentProjectId) scheduleRefresh(true);
    else if (ev.project_id === currentProjectId) scheduleRefresh(false);
  };
  // onerror: EventSource reconnects on its own; onopen does the catch-up.
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") scheduleRefresh(true);
});

// --- helpers ------------------------------------------------------------------

function twoTapConfirm(btn: HTMLButtonElement, label: string, action: () => void) {
  let timer: number | undefined;
  btn.addEventListener("click", () => {
    if (!btn.classList.contains("confirming")) {
      btn.classList.add("confirming");
      btn.textContent = "Sure?";
      timer = window.setTimeout(() => {
        btn.classList.remove("confirming");
        btn.textContent = label;
      }, 3000);
      return;
    }
    clearTimeout(timer);
    action();
  });
}

function containerIds(ul: HTMLElement): string[] {
  return [...ul.children].map((li) => (li as HTMLElement).dataset.id!);
}

// Grow a notes textarea to fit its content so the whole note stays visible
// while editing (no inner scrollbar), matching the low-friction inline UX.
function autoGrow(el: HTMLTextAreaElement) {
  el.style.height = "auto";
  el.style.height = `${el.scrollHeight}px`;
}

// Render text into `el`, turning bare http(s) URLs into clickable links (issue
// #6). Notes are the primary place Joshua drops source-ticket links. We build
// the DOM by hand (createElement + textContent) — never innerHTML — so note
// content can't inject markup. Trailing sentence punctuation is kept outside
// the link so "see https://…/6." doesn't swallow the period.
const URL_RE = /https?:\/\/[^\s<>]+/g;
function renderLinkedText(el: HTMLElement, text: string) {
  el.replaceChildren();
  let last = 0;
  for (const m of text.matchAll(URL_RE)) {
    const start = m.index!;
    if (start > last) el.append(text.slice(last, start));
    let url = m[0];
    const trailing = url.match(/[.,;:!?)\]}>'"]+$/)?.[0] ?? "";
    if (trailing) url = url.slice(0, -trailing.length);
    const a = document.createElement("a");
    a.className = "note-link";
    a.href = url;
    a.textContent = url;
    a.target = "_blank";
    a.rel = "noopener";
    el.append(a);
    if (trailing) el.append(trailing);
    last = start + m[0].length;
  }
  if (last < text.length) el.append(text.slice(last));
}

// Recompute a group's "done/total" badge from its DOM list.
function updateGroupCount(section: HTMLElement) {
  const ul = section.querySelector(".group-tasks") as HTMLElement;
  const total = ul.children.length;
  const done = ul.querySelectorAll("li.done").length;
  const badge = section.querySelector(".group-count") as HTMLElement;
  badge.textContent = `${done}/${total}`;
  badge.classList.toggle("complete", total > 0 && done === total);
}

function updateCountsAround(...uls: (HTMLElement | null)[]) {
  for (const ul of uls) {
    const section = ul?.closest("section.group") as HTMLElement | null;
    if (section) updateGroupCount(section);
  }
}

function updateEmptyState() {
  emptyTasks.hidden =
    taskList.children.length > 0 || groupList.children.length > 0;
}

// --- completed-sink sort (issue #3) -------------------------------------------
// Completed items sink to the bottom for display only — stored positions are
// never touched. Within a container, done tasks go below open ones; a fully
// completed group sinks below groups that still have open work. Relative order
// within each partition is preserved (re-appending existing nodes just moves
// them). Applied at render and kept live on toggle/add, since a tab never
// refetches its own writes.

function sinkDoneRows(ul: HTMLElement) {
  const rows = [...ul.children] as HTMLElement[];
  const open = rows.filter((li) => !li.classList.contains("done"));
  const done = rows.filter((li) => li.classList.contains("done"));
  ul.append(...open, ...done);
}

// A group counts as complete via the same rule as its badge: it has tasks and
// every one is done (updateGroupCount sets `.complete`). Empty groups stay put.
function groupIsComplete(section: HTMLElement): boolean {
  return !!section.querySelector(".group-count.complete");
}

function sinkCompletedGroups() {
  const sections = [...groupList.children] as HTMLElement[];
  const active = sections.filter((s) => !groupIsComplete(s));
  const complete = sections.filter((s) => groupIsComplete(s));
  groupList.append(...active, ...complete);
}

// --- rendering ------------------------------------------------------------------

const STATUS_ORDER: Record<ProjectStatus, number> = { active: 0, paused: 1, done: 2, closed: 2 };

function projectLabel(p: Project): string {
  const parts = [p.name];
  if (p.goal && p.task_count > 0) parts.push(`${p.percent}%`);
  if (p.status !== "active") parts.push(p.status);
  return parts.join(" · ");
}

// The app bar's project picker, on a project page.
function renderProjects() {
  const ordered = [...projects].sort((a, b) => STATUS_ORDER[a.status] - STATUS_ORDER[b.status]);
  projectSelect.replaceChildren(
    ...ordered.map((p) => {
      const option = document.createElement("option");
      option.value = p.id;
      option.textContent = projectLabel(p);
      return option;
    }),
  );
  if (currentProjectId) projectSelect.value = currentProjectId;
}

// --- the dashboard: one card for each project --------------------------------

function projectCard(view: CardView): HTMLElement {
  const card = document.createElement("a");
  card.className = "card" + (view.finished ? " finished" : "");
  card.href = view.href;

  const head = document.createElement("div");
  head.className = "card-head";
  const name = document.createElement("span");
  name.className = "card-name";
  name.textContent = view.name;
  head.append(name);
  if (view.badge) {
    const badge = document.createElement("span");
    badge.className = `health ${view.badge.kind}`;
    badge.textContent = view.badge.text;
    head.append(badge);
  }

  const progress = document.createElement("div");
  progress.className = "card-progress";
  const bar = document.createElement("div");
  bar.className = "progress-bar";
  const fill = document.createElement("div");
  fill.className = "progress-fill";
  fill.style.width = `${view.percent}%`;
  bar.append(fill);
  const percent = document.createElement("span");
  percent.className = "progress-text";
  percent.textContent = view.percentLabel;
  progress.append(bar, percent);

  const meta = document.createElement("div");
  meta.className = "card-meta";
  const count = document.createElement("span");
  count.textContent = view.countLabel;
  meta.append(count);
  if (view.targetLabel) {
    const target = document.createElement("span");
    target.className = "card-target";
    target.textContent = view.targetLabel;
    meta.append(target);
  }

  card.append(head);
  if (view.goal) {
    // A span, not a paragraph: the whole card is one link.
    const goal = document.createElement("span");
    goal.className = "card-goal";
    goal.textContent = view.goal;
    card.append(goal);
  }
  card.append(progress, meta);
  if (view.nextTitle) {
    const next = document.createElement("div");
    next.className = "card-next";
    const label = document.createElement("span");
    label.className = "next-badge";
    label.textContent = "up next";
    const title = document.createElement("span");
    title.className = "card-next-title";
    title.textContent = view.nextTitle;
    next.append(label, title);
    card.append(next);
  }
  return card;
}

// The search area is open while the box has text, or while a person is in it.
let searchOpen = false;

// One card, with the archive action beside it. The card is one link, so the
// button cannot sit inside it.
function projectRow(project: Project): HTMLElement {
  const row = document.createElement("div");
  row.className = "card-row";
  const archive = document.createElement("button");
  archive.type = "button";
  archive.className = "ghost card-archive";
  archive.textContent = "Archive";
  archive.title = "Hide this project, as it is. Ask Joshua to unarchive it.";
  twoTapConfirm(archive, "Archive", () => archiveProject(project.id));
  row.append(projectCard(projectCardView(project)), archive);
  return row;
}

// Archiving hides a project from this page, from the search, and from
// Joshua, and keeps its status: the work was not done. Joshua brings one
// back with unarchive_project.
async function archiveProject(id: string) {
  try {
    await api.updateProject(id, { archived: true });
  } catch {
    return; // the card stays; the next event or refresh shows the truth
  }
  projects = projects.filter((p) => p.id !== id);
  renderProjects();
  renderDashboard();
  // Archiving the last project leaves the home page empty: show the hero.
  dashboard.hidden = projects.length === 0;
  emptyProjects.hidden = projects.length > 0;
}

function renderDashboard() {
  const shown = dashboardProjects(projects);
  projectCards.replaceChildren(...shown.map(projectRow));
  const finished = finishedProjects(projects);
  finishedCards.replaceChildren(...finished.map(projectRow));
  finishedSection.hidden = finished.length === 0;
  const note = dashboardMessage(projects, shown);
  dashboardNote.textContent = note ?? "";
  dashboardNote.hidden = note === null;
  searchResults.hidden = !searchOpen;
  // While a search runs, the full project list steps aside.
  dashboardProjectList.hidden = searchOpen && route.query.trim() !== "";
}

// --- the search: matching projects, then matching tasks ----------------------

function searchTaskRow(view: SearchTaskView): HTMLElement {
  const li = document.createElement("li");
  li.className = "found-task" + (view.done ? " done" : "") + (view.blocked ? " blocked" : "");
  const link = document.createElement("a");
  link.href = view.href;
  const number = document.createElement("span");
  number.className = "task-number";
  number.textContent = `#${view.number}`;
  number.title = `Task ${view.number}`;
  const title = document.createElement("span");
  title.className = "found-title";
  title.textContent = view.title;
  const where = document.createElement("span");
  where.className = "found-where";
  where.textContent = view.whereLabel;
  link.append(number, title, where);
  if (view.done) {
    const badge = document.createElement("span");
    badge.className = "health done";
    badge.textContent = "done";
    link.append(badge);
  } else if (view.blocked) {
    const badge = document.createElement("span");
    badge.className = "blocked-badge";
    badge.textContent = "blocked";
    link.append(badge);
  }
  li.append(link);
  return li;
}

function renderSearch(result: SearchResult | null) {
  const view = searchView(route.query, result);
  searchNote.textContent = view.message ?? "";
  searchNote.hidden = view.message === null;
  foundProjectCards.replaceChildren(...view.projects.map(projectCard));
  foundProjects.hidden = view.projects.length === 0;
  foundTaskList.replaceChildren(...view.tasks.map(searchTaskRow));
  foundTasks.hidden = view.tasks.length === 0;
}

// One request at a time wins: a slow answer to an old query is dropped.
let searchSeq = 0;
let searchTimer: number | undefined;

async function runSearch() {
  if (!searchOpen) return;
  const text = route.query.trim();
  const seq = ++searchSeq;
  renderSearch(null);
  if (!text) return;
  try {
    const result = await api.search(text, route.includeDone);
    if (seq === searchSeq) renderSearch(result);
  } catch {
    if (seq !== searchSeq) return;
    searchNote.textContent = "The search failed. Try again.";
    searchNote.hidden = false;
  }
}

// The search text and the two toggles live in the URL, so a search is a link
// a person can keep. Typing replaces the entry instead of pushing a new one.
function setSearchRoute(patch: Partial<Route>) {
  route = { ...route, ...patch };
  history.replaceState({}, "", homeHref(route));
}

function openSearch() {
  searchOpen = true;
  renderDashboard();
}

function closeSearch() {
  searchOpen = false;
  renderDashboard();
}

searchInput.addEventListener("focus", openSearch);
searchInput.addEventListener("blur", () => {
  if (!searchInput.value.trim()) closeSearch();
});
searchInput.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = window.setTimeout(() => {
    setSearchRoute({ query: searchInput.value });
    openSearch();
    runSearch();
  }, 250);
});
searchForm.addEventListener("submit", (e) => {
  e.preventDefault();
  clearTimeout(searchTimer);
  setSearchRoute({ query: searchInput.value });
  openSearch();
  runSearch();
});
searchInput.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  searchInput.value = "";
  setSearchRoute({ query: "" });
  closeSearch();
});
// The same checkbox decides whether done tasks match and whether the done
// and the closed projects are searched: one "include done" for the search.
searchDone.addEventListener("change", () => {
  setSearchRoute({ includeDone: searchDone.checked });
  runSearch();
});

// --- routing: the dashboard, or one project ----------------------------------

function navigate(href: string) {
  history.pushState({}, "", href);
  applyRoute();
}

// Every in-app link (a card, a found task, the app bar) moves the view
// without a page load. An external link, and a note link in a new tab, pass
// through.
document.addEventListener("click", (e) => {
  if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey) return;
  const link = (e.target as HTMLElement).closest("a");
  if (!link || link.target || link.origin !== location.origin) return;
  e.preventDefault();
  navigate(link.pathname + link.search + link.hash);
});

window.addEventListener("popstate", () => applyRoute());

// A task link is "/p/<slug>#task-<number>": mark the task and bring it into
// view. A link that holds the task id instead still finds the same task.
function revealLinkedTask() {
  const ref = /^#task-(.+)$/.exec(location.hash)?.[1];
  if (!ref) return;
  const escaped = CSS.escape(ref);
  const li = main.querySelector(
    `li.task[data-number="${escaped}"], li.task[data-id="${escaped}"]`,
  );
  if (!li) return;
  li.scrollIntoView({ block: "center" });
  li.classList.add("linked");
  window.setTimeout(() => li.classList.remove("linked"), 2500);
}

async function applyRoute() {
  route = parseRoute(location.pathname + location.search);
  let project = route.project ? projects.find((p) => p.slug === route.project) : undefined;
  if (route.project && !project) {
    // A slug no project has (a renamed or a deleted project): show the home page.
    history.replaceState({}, "", "/");
    route = parseRoute("/");
    project = undefined;
  }
  currentProjectId = project?.id ?? null;
  const onProject = project !== undefined;
  main.hidden = !onProject;
  projectSelect.hidden = !onProject;
  searchLink.hidden = !onProject;
  dashboard.hidden = onProject || projects.length === 0;
  emptyProjects.hidden = onProject || projects.length > 0;
  if (!onProject) {
    // "/?q=" (the app bar's Search link, from a project page) opens the box.
    const wantsSearch = new URLSearchParams(location.search).has("q");
    searchOpen = wantsSearch || route.query.trim() !== "";
    searchInput.value = route.query;
    searchDone.checked = route.includeDone;
    renderDashboard();
    if (wantsSearch) searchInput.focus();
    await runSearch();
    return;
  }
  renderProjects();
  renderProjectHeader();
  await loadTasks();
  revealLinkedTask();
}

// --- project header: goal, progress, target date, status ---------------------

function renderProjectHeader() {
  const p = currentProject();
  if (!p) return;
  projectGoal.textContent = p.goal ?? "Set a goal for this project…";
  projectGoal.classList.toggle("unset", !p.goal);
  progressFill.style.width = `${p.percent}%`;
  progressText.textContent = `${p.done_count}/${p.task_count} · ${p.percent}%`;
  projectHealth.textContent = p.health ? p.health.replaceAll("_", " ") : "";
  projectHealth.className = "health" + (p.health ? ` ${p.health}` : "");
  projectTarget.value = p.target_date ?? "";
  projectStatus.value = p.status;
  const finished = p.status === "done" || p.status === "closed";
  main.classList.toggle("finished", finished);
  completeBtn.hidden = p.status === "done";
  completeBtn.disabled = p.open_count > 0 || p.task_count === 0;
  completeBtn.title =
    p.open_count > 0
      ? `${p.open_count} task(s) still open. Finish them, or set the status to closed.`
      : "Mark the project done";
}

async function patchProject(patch: Parameters<typeof api.updateProject>[1]) {
  if (!currentProjectId) return;
  try {
    const updated = await api.updateProject(currentProjectId, patch);
    projects = projects.map((p) => (p.id === updated.id ? updated : p));
  } catch {
    /* keep the previous values; the header is re-rendered from them */
  }
  renderProjects();
  renderProjectHeader();
}

projectGoal.addEventListener("click", () => {
  const p = currentProject();
  if (!p || projectGoal.querySelector("input")) return;
  const input = document.createElement("input");
  input.type = "text";
  input.className = "project-goal-input";
  input.value = p.goal ?? "";
  input.maxLength = 2000;
  input.placeholder = "What does done look like?";
  let settled = false;
  const finish = async (save: boolean) => {
    if (settled) return;
    settled = true;
    const value = input.value.trim();
    input.replaceWith(projectGoal);
    if (save && value !== (p.goal ?? "")) await patchProject({ goal: value || null });
    else renderProjectHeader();
  };
  input.addEventListener("blur", () => finish(true));
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      finish(false);
    } else if (e.key === "Enter") {
      e.preventDefault();
      input.blur();
    }
  });
  projectGoal.replaceWith(input);
  input.focus();
});

projectTarget.addEventListener("change", () => {
  patchProject({ target_date: projectTarget.value || null });
});

projectStatus.addEventListener("change", () => {
  patchProject({ status: projectStatus.value as ProjectStatus });
});

completeBtn.addEventListener("click", () => patchProject({ status: "done" }));

// --- the up-next marker --------------------------------------------------------
// The first open task in display order (phases, then the backlog) is the
// project's next step. Display order is roadmap order, so the DOM is enough.
// A task that waits for an open blocker is never the next step, as on the
// dashboard card and in next_steps.

function markNext() {
  for (const li of main.querySelectorAll("li.task.next")) li.classList.remove("next");
  const first = main.querySelector("li.task:not(.done):not(.blocked)");
  if (first) first.classList.add("next");
}

function taskChanged(...uls: (HTMLElement | null)[]) {
  updateCountsAround(...uls);
  markNext();
  scheduleSummaryRefresh();
}

// --- task details: size, due date, assignee -------------------------------------

function isoToday(): string {
  return new Date().toISOString().slice(0, 10);
}

function taskRow(task: Task): HTMLLIElement {
  const li = document.createElement("li");
  li.className = "task" + (task.done ? " done" : "") + (task.blocked ? " blocked" : "");
  li.dataset.id = task.id;
  li.dataset.number = String(task.number);

  const row = document.createElement("div");
  row.className = "task-row";

  const handle = document.createElement("span");
  handle.className = "drag-handle";
  handle.textContent = "⠿";
  handle.title = "Drag to reorder";

  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = task.done;
  checkbox.ariaLabel = task.done ? "Mark not done" : "Mark done";
  checkbox.addEventListener("change", async () => {
    const ul = li.parentElement as HTMLElement;
    li.classList.toggle("done", checkbox.checked);
    sinkDoneRows(ul);
    sinkCompletedGroups();
    taskChanged(ul);
    try {
      await api.setDone(task.id, checkbox.checked);
      // The tasks that wait for this one are blocked no longer, or blocked
      // again: their rows come from the server.
      if (task.blocking.length > 0) await loadTasks();
    } catch {
      checkbox.checked = !checkbox.checked;
      li.classList.toggle("done", checkbox.checked);
      sinkDoneRows(ul);
      sinkCompletedGroups();
      taskChanged(ul);
    }
  });

  const nextBadge = document.createElement("span");
  nextBadge.className = "next-badge";
  nextBadge.textContent = "up next";

  // --- the blocking links -----------------------------------------------------
  // What the task waits for, and what waits for it. Each linked task is a
  // link to its own row, which can be in another project. The "×" on a
  // blocker removes that link; a done blocker needs no removal.
  const blockedBadge = document.createElement("span");
  blockedBadge.className = "blocked-badge";
  blockedBadge.textContent = "blocked";

  const links = document.createElement("div");
  links.className = "task-links";

  function linkChip(ref: TaskLink, onRemove: (() => void) | null): HTMLElement {
    const chip = document.createElement("span");
    chip.className = "task-link" + (ref.done ? " done" : "");
    let label: HTMLElement;
    if (ref.href) {
      const anchor = document.createElement("a");
      anchor.href = ref.href;
      label = anchor;
    } else {
      label = document.createElement("span");
    }
    label.className = "task-link-title";
    label.textContent = ref.title;
    const number = document.createElement("span");
    number.className = "task-number";
    number.textContent = `#${ref.number}`;
    number.title = `Task ${ref.number}`;
    chip.append(number, label);
    if (ref.projectName) {
      const where = document.createElement("span");
      where.className = "task-link-project";
      where.textContent = ref.projectName;
      chip.append(where);
    }
    if (onRemove) {
      const drop = document.createElement("button");
      drop.type = "button";
      drop.className = "task-link-drop";
      drop.textContent = "×";
      drop.title = "Remove this blocker";
      drop.addEventListener("click", onRemove);
      chip.append(drop);
    }
    return chip;
  }

  function renderLinks() {
    const view = taskLinksView(task);
    li.classList.toggle("blocked", view.blocked);
    blockedBadge.hidden = !view.blocked;
    links.replaceChildren();
    const line = (text: string, refs: TaskLink[], cls: string, removable: boolean) => {
      if (refs.length === 0) return;
      const row = document.createElement("div");
      row.className = `task-link-line ${cls}`;
      const label = document.createElement("span");
      label.className = "task-link-label";
      label.textContent = text;
      row.append(label);
      for (const ref of refs) {
        row.append(linkChip(ref, removable ? () => removeBlocker(ref.id) : null));
      }
      links.append(row);
    };
    line("blocked by", view.blockers, "blocked-by", true);
    line("blocks", view.blocking, "blocks", false);
    links.hidden = view.blockers.length === 0 && view.blocking.length === 0;
  }

  async function removeBlocker(blockerId: string) {
    try {
      const updated = await api.unblockTask(task.id, blockerId);
      Object.assign(task, updated);
    } catch {
      /* keep the link on failure */
    }
    renderLinks();
    markNext();
  }

  // Size, due date, and assignee show as small badges when set. The "⋯"
  // button opens a details row with the three controls.
  const meta = document.createElement("span");
  meta.className = "task-meta";
  const details = document.createElement("div");
  details.className = "task-details";
  details.hidden = true;

  function renderMeta() {
    meta.replaceChildren();
    const badge = (text: string, cls = "") => {
      const b = document.createElement("span");
      b.className = "meta-badge" + (cls ? ` ${cls}` : "");
      b.textContent = text;
      meta.append(b);
    };
    if (task.size) badge(task.size);
    if (task.due_date) {
      badge(task.due_date, !task.done && task.due_date < isoToday() ? "overdue" : "");
    }
    if (task.assignee) badge(task.assignee);
  }

  async function saveDetails(patch: TaskPatch) {
    try {
      const updated = await api.updateTask(task.id, patch);
      Object.assign(task, updated);
    } catch {
      /* keep the previous values */
    }
    renderMeta();
  }

  const sizeSelect = document.createElement("select");
  const none = document.createElement("option");
  none.value = "";
  none.textContent = "size";
  sizeSelect.append(none);
  for (const size of TASK_SIZES) {
    const option = document.createElement("option");
    option.value = size;
    option.textContent = size;
    sizeSelect.append(option);
  }
  sizeSelect.value = task.size ?? "";
  sizeSelect.addEventListener("change", () =>
    saveDetails({ size: (sizeSelect.value || null) as TaskSize | null }),
  );

  const dueInput = document.createElement("input");
  dueInput.type = "date";
  dueInput.value = task.due_date ?? "";
  dueInput.addEventListener("change", () => saveDetails({ due_date: dueInput.value || null }));

  const assigneeInput = document.createElement("input");
  assigneeInput.type = "text";
  assigneeInput.placeholder = "assignee";
  assigneeInput.maxLength = 200;
  assigneeInput.value = task.assignee ?? "";
  assigneeInput.addEventListener("change", () =>
    saveDetails({ assignee: assigneeInput.value.trim() || null }),
  );

  const wrap = (text: string, control: HTMLElement) => {
    const label = document.createElement("label");
    label.append(text, control);
    return label;
  };
  details.append(wrap("", sizeSelect), wrap("due", dueInput), wrap("", assigneeInput));

  const detailsBtn = document.createElement("button");
  detailsBtn.className = "details-toggle";
  detailsBtn.textContent = "⋯";
  detailsBtn.title = "Size, due date, assignee";
  detailsBtn.addEventListener("click", () => {
    details.hidden = !details.hidden;
    li.classList.toggle("editing", !details.hidden);
  });

  // The task number: the short name a person says ("task 24"). It is text,
  // so it can be selected and copied, and the task's link holds it too.
  const number = document.createElement("span");
  number.className = "task-number";
  number.textContent = `#${task.number}`;
  number.title = `Task ${task.number}`;

  const title = document.createElement("span");
  title.className = "title";
  title.textContent = task.title;
  title.title = "Click to edit";

  // --- inline title editing (issue #6) ----------------------------------------
  // Clicking the title makes it editable in place — the only way to rename a
  // task without going through Joshua, and the natural gesture on mobile. Same
  // pattern as the group rename / notes edit: blur saves, Enter (or Ctrl/Cmd+
  // Enter) commits, Escape cancels.
  function editTitle() {
    if (!title.isConnected) return; // already in edit mode
    const input = document.createElement("input");
    input.type = "text";
    input.className = "task-title-input";
    input.value = task.title;
    input.maxLength = 1000;

    let settled = false;
    const finish = async (save: boolean) => {
      if (settled) return;
      settled = true;
      const value = input.value.trim();
      if (save && value && value !== task.title) {
        try {
          const updated = await api.updateTitle(task.id, value);
          task.title = updated.title;
        } catch {
          /* keep the previous title on failure */
        }
      }
      title.textContent = task.title;
      input.replaceWith(title);
    };
    input.addEventListener("blur", () => finish(true));
    input.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        e.preventDefault();
        finish(false);
      } else if (e.key === "Enter") {
        e.preventDefault();
        input.blur(); // commits via the blur handler
      }
    });
    title.replaceWith(input);
    input.focus();
    input.select();
  }
  title.addEventListener("click", editTitle);

  // --- notes (issue #5) -------------------------------------------------------
  // Shown below the row when present; empty notes stay hidden behind a
  // hover-revealed "add note" affordance so the display stays uncluttered.
  // Inline edit mirrors the group-rename pattern: click to edit, blur saves,
  // Escape cancels (Ctrl/Cmd+Enter also commits, since Enter adds a newline).
  const noteBtn = document.createElement("button");
  noteBtn.className = "note-toggle";
  noteBtn.textContent = "✎";

  const notes = document.createElement("div");
  notes.className = "task-notes";

  function renderNotes() {
    const has = task.notes.trim().length > 0;
    notes.hidden = !has;
    notes.classList.toggle("has-notes", has);
    if (has) renderLinkedText(notes, task.notes); // URLs → clickable links (#6)
    else notes.replaceChildren();
    notes.title = has ? "Click to edit note" : "";
    noteBtn.classList.toggle("active", has); // hide the add button once noted
    noteBtn.title = has ? "Edit note" : "Add note";
  }

  function editNotes() {
    if (notes.querySelector(".task-notes-input")) return; // already editing
    const textarea = document.createElement("textarea");
    textarea.className = "task-notes-input";
    textarea.value = task.notes;
    textarea.placeholder = "Add a note…";
    notes.hidden = false;
    notes.replaceChildren(textarea);
    autoGrow(textarea);
    textarea.focus();
    textarea.setSelectionRange(task.notes.length, task.notes.length);

    let settled = false;
    const finish = async (save: boolean) => {
      if (settled) return;
      settled = true;
      const value = textarea.value.trim();
      if (save && value !== task.notes) {
        try {
          const updated = await api.updateNotes(task.id, value);
          task.notes = updated.notes;
        } catch {
          /* keep the previous note on failure */
        }
      }
      renderNotes();
    };
    textarea.addEventListener("input", () => autoGrow(textarea));
    textarea.addEventListener("blur", () => finish(true));
    textarea.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        e.preventDefault();
        finish(false);
      } else if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        textarea.blur();
      }
    });
  }

  notes.addEventListener("click", (e) => {
    if ((e.target as HTMLElement).closest("a")) return; // let links open
    if (notes.classList.contains("has-notes")) editNotes();
  });
  // The pencil affordance (shown only until a note exists) opens a coherent
  // edit mode — title and the fresh notes textarea both editable at once, with
  // focus landing in the notes area the tap invited.
  noteBtn.addEventListener("click", () => {
    editTitle();
    editNotes();
  });

  const del = document.createElement("button");
  del.className = "delete";
  del.textContent = "×";
  del.title = "Delete task";
  async function deleteTask() {
    const ul = li.parentElement;
    await api.deleteTask(task.id);
    li.remove();
    taskChanged(ul);
    updateEmptyState();
  }
  twoTapConfirm(del, "×", deleteTask);

  // On a phone the row keeps only the "⋯" button; the note and delete
  // actions live in the details row instead (the stylesheet swaps them).
  const detailsActions = document.createElement("span");
  detailsActions.className = "task-details-actions";
  const noteAction = document.createElement("button");
  noteAction.type = "button";
  noteAction.className = "ghost";
  noteAction.textContent = "Note";
  noteAction.addEventListener("click", () => {
    editTitle();
    editNotes();
  });
  const deleteAction = document.createElement("button");
  deleteAction.type = "button";
  deleteAction.className = "ghost delete-action";
  deleteAction.textContent = "Delete";
  twoTapConfirm(deleteAction, "Delete", deleteTask);
  detailsActions.append(noteAction, deleteAction);
  details.append(detailsActions);

  // The title and its badges share one flexible column, so the badges wrap
  // under the title on a narrow screen instead of squeezing it.
  const tags = document.createElement("span");
  tags.className = "task-tags";
  tags.append(nextBadge, blockedBadge, meta);
  const body = document.createElement("div");
  body.className = "task-body";
  body.append(number, title, tags);
  const actions = document.createElement("span");
  actions.className = "task-actions";
  actions.append(noteBtn, detailsBtn, del);
  row.append(handle, checkbox, body, actions);
  li.append(row, links, notes, details);
  renderNotes();
  renderMeta();
  renderLinks();
  return li;
}

const taskSortableOpts: Sortable.Options = {
  group: "tasks",
  handle: ".drag-handle",
  animation: 150,
  onStart: () => {
    dragging = true;
  },
  onEnd: async (evt) => {
    dragDone();
    if (!currentProjectId) return;
    const to = evt.to as HTMLElement;
    const groupId = to.dataset.groupId ?? null;
    taskChanged(evt.from as HTMLElement, to);
    updateEmptyState();
    try {
      // Optimistic: Sortable already moved the row. The id list is the
      // authoritative content of the destination container.
      await api.reorder(currentProjectId, containerIds(to), groupId);
    } catch {
      await loadTasks(); // conflict or failure — reconcile with the server
    }
  },
};

function groupSection(group: Group, tasks: Task[]): HTMLElement {
  const section = document.createElement("section");
  section.className = "group";
  section.dataset.id = group.id;

  const header = document.createElement("header");
  header.className = "group-header";

  const handle = document.createElement("span");
  handle.className = "group-handle";
  handle.textContent = "⠿";
  handle.title = "Drag to reorder groups";

  const name = document.createElement("span");
  name.className = "group-name";
  name.textContent = group.name;
  name.title = "Click to rename";
  name.addEventListener("click", () => {
    const input = document.createElement("input");
    input.type = "text";
    input.value = group.name;
    input.maxLength = 200;
    input.className = "group-name-input";
    const save = async () => {
      const newName = input.value.trim();
      if (newName && newName !== group.name) {
        try {
          const updated = await api.renameGroup(group.id, newName);
          group.name = updated.name;
        } catch {
          /* keep old name */
        }
      }
      name.textContent = group.name;
      input.replaceWith(name);
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") input.blur();
      if (e.key === "Escape") {
        input.value = group.name;
        input.blur();
      }
    });
    input.addEventListener("blur", save);
    name.replaceWith(input);
    input.focus();
    input.select();
  });

  const count = document.createElement("span");
  count.className = "group-count";

  // The phase's target date: a badge that turns into a date input on click.
  const target = document.createElement("span");
  target.className = "group-target";
  const renderTarget = () => {
    target.textContent = group.target_date ?? "set date";
    target.classList.toggle("no-date", !group.target_date);
  };
  target.addEventListener("click", () => {
    const input = document.createElement("input");
    input.type = "date";
    input.value = group.target_date ?? "";
    // change and blur both end the edit; only the first one acts.
    let settled = false;
    const finish = async () => {
      if (settled) return;
      settled = true;
      const value = input.value || null;
      if (value !== group.target_date) {
        try {
          const updated = await api.setGroupTargetDate(group.id, value);
          group.target_date = updated.target_date;
        } catch {
          /* keep the old date */
        }
      }
      renderTarget();
      input.replaceWith(target);
    };
    input.addEventListener("change", finish);
    input.addEventListener("blur", finish);
    target.replaceWith(input);
    input.focus();
  });
  renderTarget();

  const del = document.createElement("button");
  del.className = "delete group-delete";
  del.textContent = "×";
  del.title = "Delete group (tasks move to the main list)";
  twoTapConfirm(del, "×", async () => {
    await api.deleteGroup(group.id);
    await loadTasks(); // members re-appear in the ungrouped list
  });

  header.append(handle, name, count, target, del);

  const ul = document.createElement("ul");
  ul.className = "group-tasks";
  ul.dataset.groupId = group.id;
  ul.append(...tasks.map(taskRow));
  new Sortable(ul, taskSortableOpts);

  const addForm = document.createElement("form");
  addForm.className = "group-add";
  addForm.autocomplete = "off";
  const addInput = document.createElement("input");
  addInput.type = "text";
  addInput.placeholder = "Add to group…";
  addInput.maxLength = 1000;
  addForm.append(addInput);
  addForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const title = addInput.value.trim();
    if (!title || !currentProjectId) return;
    addInput.value = "";
    const task = await api.createTask(currentProjectId, title, group.id);
    ul.append(taskRow(task));
    sinkDoneRows(ul); // new open task sits above any completed ones
    sinkCompletedGroups(); // a group that regained open work rises again
    taskChanged(ul);
  });

  section.append(header, ul, addForm);
  updateGroupCount(section);
  return section;
}

function renderTasks(tasks: Task[], groups: Group[]) {
  const byGroup = new Map<string, Task[]>();
  const ungrouped: Task[] = [];
  for (const t of tasks) {
    if (t.group_id) {
      if (!byGroup.has(t.group_id)) byGroup.set(t.group_id, []);
      byGroup.get(t.group_id)!.push(t);
    } else {
      ungrouped.push(t);
    }
  }
  taskList.replaceChildren(...ungrouped.map(taskRow));
  groupList.replaceChildren(
    ...groups.map((g) => groupSection(g, byGroup.get(g.id) ?? [])),
  );
  // Display-only completed-sink pass (issue #3): stored order is untouched.
  sinkDoneRows(taskList);
  for (const section of groupList.children) {
    sinkDoneRows(
      (section as HTMLElement).querySelector(".group-tasks") as HTMLElement,
    );
  }
  sinkCompletedGroups();
  // A project with no phases is one plain list; the "Backlog" label only
  // makes sense next to phases.
  backlogHeader.hidden = groups.length === 0;
  markNext();
  updateEmptyState();
}

// --- data flow ---------------------------------------------------------------

async function loadTasks() {
  if (!currentProjectId) return;
  const [tasks, groups] = await Promise.all([
    api.listTasks(currentProjectId),
    api.listGroups(currentProjectId),
  ]);
  renderTasks(tasks, groups);
}

/** Go to one project's page. */
function openProject(id: string) {
  const project = projects.find((p) => p.id === id);
  if (project) navigate(projectHref(project));
}

// ``GET /api/projects`` returns every project that is not archived, the done
// and the closed ones too; the dashboard puts those in their own section.
async function loadProjects() {
  projects = await api.listProjects();
  renderProjects();
  renderDashboard();
  if (currentProjectId) renderProjectHeader();
}

// --- new project (inline, dialog-free) ----------------------------------------

function showNewProjectForm() {
  newProjectForm.hidden = false;
  newProjectInput.focus();
}

function hideNewProjectForm() {
  newProjectForm.hidden = true;
  newProjectInput.value = "";
  newProjectGoal.value = "";
}

projectSelect.addEventListener("change", () => openProject(projectSelect.value));
addProjectBtn.addEventListener("click", showNewProjectForm);
createFirstBtn.addEventListener("click", showNewProjectForm);
newProjectCancel.addEventListener("click", hideNewProjectForm);
newProjectInput.addEventListener("keydown", (e) => {
  if (e.key === "Escape") hideNewProjectForm();
});

newProjectForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = newProjectInput.value.trim();
  if (!name) return;
  const project = await api.createProject(name, newProjectGoal.value.trim() || null);
  projects.push(project);
  hideNewProjectForm();
  renderProjects();
  openProject(project.id);
});

// --- new group (inline, dialog-free) -------------------------------------------

function hideNewGroupForm() {
  newGroupForm.hidden = true;
  newGroupInput.value = "";
  addGroupBtn.hidden = false;
}

addGroupBtn.addEventListener("click", () => {
  addGroupBtn.hidden = true;
  newGroupForm.hidden = false;
  newGroupInput.focus();
});
newGroupCancel.addEventListener("click", hideNewGroupForm);
newGroupInput.addEventListener("keydown", (e) => {
  if (e.key === "Escape") hideNewGroupForm();
});

newGroupForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const name = newGroupInput.value.trim();
  if (!name || !currentProjectId) return;
  const group = await api.createGroup(currentProjectId, name);
  hideNewGroupForm();
  groupList.append(groupSection(group, []));
  backlogHeader.hidden = false;
  updateEmptyState();
});

// --- add task (ungrouped root list) --------------------------------------------

addTaskForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const title = addTaskInput.value.trim();
  if (!title || !currentProjectId) return;
  addTaskInput.value = "";
  const task = await api.createTask(currentProjectId, title);
  taskList.append(taskRow(task));
  sinkDoneRows(taskList); // new open task sits above any completed ones
  taskChanged(taskList);
  updateEmptyState();
});

// --- sortables ------------------------------------------------------------------

new Sortable(taskList, taskSortableOpts);

new Sortable(groupList, {
  handle: ".group-handle",
  animation: 150,
  onStart: () => {
    dragging = true;
  },
  onEnd: async () => {
    dragDone();
    if (!currentProjectId) return;
    const ids = [...groupList.children].map((s) => (s as HTMLElement).dataset.id!);
    markNext();
    try {
      await api.reorderGroups(currentProjectId, ids);
      scheduleSummaryRefresh();
    } catch {
      await loadTasks();
    }
  },
});

// The project list comes first: the route names a project by its slug.
async function start() {
  try {
    await loadProjects();
  } catch {
    /* the next event or visibility change retries */
  }
  await applyRoute();
}

start();
connectEvents();
