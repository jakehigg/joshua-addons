import Sortable from "sortablejs";
import { api, clientId, type Group, type Project, type Task } from "./api";

const LAST_PROJECT_KEY = "joshua-tasks.last-project";

const projectSelect = document.getElementById("project-select") as HTMLSelectElement;
const addProjectBtn = document.getElementById("add-project") as HTMLButtonElement;
const createFirstBtn = document.getElementById("create-first-project") as HTMLButtonElement;
const newProjectForm = document.getElementById("new-project-form") as HTMLFormElement;
const newProjectInput = document.getElementById("new-project-input") as HTMLInputElement;
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

let projects: Project[] = [];
let currentProjectId: string | null = null;

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
    if (withProjects) await loadProjects();
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
    if (ev.scope === "projects") scheduleRefresh(true);
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

function renderProjects() {
  projectSelect.replaceChildren(
    ...projects.map((p) => {
      const option = document.createElement("option");
      option.value = p.id;
      option.textContent = p.name;
      return option;
    }),
  );
  if (currentProjectId) projectSelect.value = currentProjectId;

  const hasProjects = projects.length > 0;
  main.hidden = !hasProjects;
  emptyProjects.hidden = hasProjects;
  projectSelect.hidden = !hasProjects;
}

function taskRow(task: Task): HTMLLIElement {
  const li = document.createElement("li");
  li.className = "task" + (task.done ? " done" : "");
  li.dataset.id = task.id;

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
    updateCountsAround(ul);
    sinkDoneRows(ul);
    sinkCompletedGroups();
    try {
      await api.setDone(task.id, checkbox.checked);
    } catch {
      checkbox.checked = !checkbox.checked;
      li.classList.toggle("done", checkbox.checked);
      updateCountsAround(ul);
      sinkDoneRows(ul);
      sinkCompletedGroups();
    }
  });

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
  twoTapConfirm(del, "×", async () => {
    const ul = li.parentElement;
    await api.deleteTask(task.id);
    li.remove();
    updateCountsAround(ul);
    updateEmptyState();
  });

  row.append(handle, checkbox, title, noteBtn, del);
  li.append(row, notes);
  renderNotes();
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
    updateCountsAround(evt.from as HTMLElement, to);
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

  const del = document.createElement("button");
  del.className = "delete group-delete";
  del.textContent = "×";
  del.title = "Delete group (tasks move to the main list)";
  twoTapConfirm(del, "×", async () => {
    await api.deleteGroup(group.id);
    await loadTasks(); // members re-appear in the ungrouped list
  });

  header.append(handle, name, count, del);

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
    updateGroupCount(section);
    sinkDoneRows(ul); // new open task sits above any completed ones
    sinkCompletedGroups(); // a group that regained open work rises again
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

async function selectProject(id: string) {
  currentProjectId = id;
  localStorage.setItem(LAST_PROJECT_KEY, id);
  projectSelect.value = id;
  await loadTasks();
}

async function loadProjects() {
  projects = await api.listProjects();
  const remembered = localStorage.getItem(LAST_PROJECT_KEY);
  currentProjectId =
    projects.find((p) => p.id === remembered)?.id ?? projects[0]?.id ?? null;
  renderProjects();
  if (currentProjectId) await selectProject(currentProjectId);
}

// --- new project (inline, dialog-free) ----------------------------------------

function showNewProjectForm() {
  newProjectForm.hidden = false;
  newProjectInput.focus();
}

function hideNewProjectForm() {
  newProjectForm.hidden = true;
  newProjectInput.value = "";
}

projectSelect.addEventListener("change", () => selectProject(projectSelect.value));
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
  const project = await api.createProject(name);
  projects.push(project);
  hideNewProjectForm();
  renderProjects();
  await selectProject(project.id);
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
    try {
      await api.reorderGroups(currentProjectId, ids);
    } catch {
      await loadTasks();
    }
  },
});

loadProjects();
connectEvents();
