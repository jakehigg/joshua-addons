// What the home page shows, as plain data: the route, the order of the
// projects, and one view for each card or found row. Nothing here touches
// the DOM, so main.ts can render it and the unit tests can read it.
//
// Every number on a card (percent, open_count, health, next_task) is
// computed by the API and only read here.

import type { Project, SearchResult, SearchTask, Task, TaskRef } from "./api";

// --- the route ----------------------------------------------------------------
// "/" is the dashboard, "/p/<slug>" is one project, and "/archive" is the
// finished and the archived projects. The search text and its toggle live in
// the query string, so a search is a link a person can keep, and the back
// button moves between searches.

export interface Route {
  // The project slug of a project page, or null on the dashboard.
  project: string | null;
  // True on the page of the finished and the archived projects.
  archive: boolean;
  query: string;
  // Search done tasks and finished projects as well.
  includeDone: boolean;
}

const ROUTE_BASE = "http://route.invalid";
const ARCHIVE_PATH = "/archive";

export function parseRoute(url: string): Route {
  const parsed = new URL(url, ROUTE_BASE);
  const path = parsed.pathname.replace(/\/$/, "");
  const slug = /^\/p\/([^/]+)$/.exec(path)?.[1];
  const params = parsed.searchParams;
  return {
    project: slug ? decodeURIComponent(slug) : null,
    archive: path === ARCHIVE_PATH,
    query: params.get("q") ?? "",
    includeDone: params.get("done") === "1",
  };
}

/** The home page, with the search text and the toggle of ``route``. */
export function homeHref(route: { query: string; includeDone: boolean }): string {
  const params = new URLSearchParams();
  if (route.query) params.set("q", route.query);
  if (route.includeDone) params.set("done", "1");
  const query = params.toString();
  return query ? `/?${query}` : "/";
}

/** The page of the finished and the archived projects. */
export function archiveHref(): string {
  return ARCHIVE_PATH;
}

export function projectHref(project: { slug: string }): string {
  return `/p/${encodeURIComponent(project.slug)}`;
}

/**
 * A task in its own project page, at the task. The link holds the task
 * number, so a person can read it; the older links that hold the task id
 * still open the same task.
 */
export function taskHref(project: { slug: string }, task: { number: number }): string {
  return `${projectHref(project)}#task-${task.number}`;
}

// --- the order of the projects -------------------------------------------------
// Most useful first: what needs a person comes before what runs itself, and
// what is finished comes last. A project with a goal gets a health signal
// from the API; a plain list gets none, and sits below the projects that do.

const RANK: Record<string, number> = {
  overdue: 0,
  stalled: 1,
  ready_to_complete: 2,
  on_track: 3,
  // health holds the status of a project that is not active
  paused: 6,
  done: 7,
  closed: 7,
};

// A project with tasks but no goal and no target date: a plain list.
const PLAIN_RANK = 4;
// A project with no tasks at all.
const EMPTY_RANK = 5;

export function projectRank(project: Project): number {
  if (project.health) return RANK[project.health] ?? PLAIN_RANK;
  return project.task_count === 0 ? EMPTY_RANK : PLAIN_RANK;
}

export function isFinished(project: Project): boolean {
  return project.status === "done" || project.status === "closed";
}

export function isArchived(project: Project): boolean {
  return project.archived_at !== null;
}

/** An open project: not archived, and in neither finished status. */
export function isOpen(project: Project): boolean {
  return !isArchived(project) && !isFinished(project);
}

// Two projects of the same rank: the nearer target date first, a project
// with no target date after one with it, and otherwise the order the API
// returned (oldest first), which a stable sort keeps.
function compareTargetDate(a: Project, b: Project): number {
  if (a.target_date === b.target_date) return 0;
  if (!a.target_date) return 1;
  if (!b.target_date) return -1;
  return a.target_date < b.target_date ? -1 : 1;
}

export function orderProjects(projects: Project[]): Project[] {
  return [...projects].sort(
    (a, b) => projectRank(a) - projectRank(b) || compareTargetDate(a, b),
  );
}

/** The projects the home page lists, in order: the open ones, and only those. */
export function dashboardProjects(projects: Project[]): Project[] {
  return orderProjects(projects.filter(isOpen));
}

/** The date part of a timestamp, or null when the API sent none. */
function day(when: string | null): string | null {
  return when ? when.slice(0, 10) : null;
}

/** The day a project left the open list: its archive date, or its finish date. */
function closedOn(project: Project): string {
  return (isArchived(project) ? day(project.archived_at) : day(project.completed_at)) ?? "";
}

/**
 * The finished and the archived projects, the most recent first, for the
 * page the link at the end of the home list opens. A project with no date
 * comes last, and the order the API returned breaks a tie.
 */
export function finishedAndArchived(projects: Project[]): Project[] {
  return projects
    .filter((p) => !isOpen(p))
    .sort((a, b) => closedOn(b).localeCompare(closedOn(a)));
}

/**
 * The projects a selector offers: the open ones only. The project a person
 * has open keeps its place, whatever its status, so the picker still names
 * the page they are on.
 */
export function projectOptions(projects: Project[], currentId: string | null): Project[] {
  const options = projects.filter(isOpen);
  const current = projects.find((p) => p.id === currentId);
  if (current && !isOpen(current)) options.push(current);
  return options;
}

/** What to say when no project is open, or null when there is a card to show. */
export function dashboardMessage(projects: Project[], shown: Project[]): string | null {
  if (shown.length > 0) return null;
  if (projects.length === 0) return null; // the "no projects yet" hero says it
  return "Every project is finished or archived.";
}

// --- one project card ----------------------------------------------------------

export interface Badge {
  text: string;
  // The health or the status, for the colour; "none" for a project with no tasks.
  kind: string;
}

// A card is one compact row: the name with its status chip, the progress,
// then the counts and the target date as one quiet line. The goal and the
// next task belong to the project page, so four cards fit on a phone.

export interface CardView {
  id: string;
  href: string;
  name: string;
  percent: number;
  percentLabel: string;
  countLabel: string;
  // null for a plain list, which has no health to show.
  badge: Badge | null;
  targetLabel: string | null;
  finished: boolean;
}

export function projectCardView(project: Project): CardView {
  return {
    id: project.id,
    href: projectHref(project),
    name: project.name,
    percent: project.percent,
    percentLabel: `${project.percent}%`,
    countLabel: `${project.done_count}/${project.task_count} done · ${project.open_count} open`,
    badge: badgeOf(project),
    targetLabel: project.target_date ? `target ${project.target_date}` : null,
    finished: isFinished(project),
  };
}

// --- one row of the finished and archived page --------------------------------

export interface ArchiveRowView {
  id: string;
  href: string;
  name: string;
  // The status the project keeps: "done", "closed", or the one it had when
  // it was archived.
  statusLabel: string;
  // The day it was archived, or the day it was finished; null when the API
  // sent neither.
  dateLabel: string | null;
  // True while the project is archived: the row offers to bring it back.
  archived: boolean;
}

export function archiveRowView(project: Project): ArchiveRowView {
  return {
    id: project.id,
    href: projectHref(project),
    name: project.name,
    statusLabel: project.status,
    dateLabel: isArchived(project) ? day(project.archived_at) : day(project.completed_at),
    archived: isArchived(project),
  };
}

function badgeOf(project: Project): Badge | null {
  if (project.health) return { text: project.health.replaceAll("_", " "), kind: project.health };
  // health is null for a project with no tasks, and for a plain list.
  if (project.task_count === 0) return { text: "no tasks yet", kind: "none" };
  return null;
}

// --- the blocking links --------------------------------------------------------
// What a task waits for, and what waits for it. The API computes blocked and
// the two lists on every read; nothing is worked out here.

export interface TaskLink {
  id: string;
  // The task number, shown on the chip and held by the link.
  number: number;
  title: string;
  done: boolean;
  // The linked task in its own project page, or null when that task lost its
  // project and cannot be opened.
  href: string | null;
  // The project of the linked task, which can be another project.
  projectName: string | null;
}

export interface TaskLinksView {
  // True while at least one blocker is open: the task cannot start.
  blocked: boolean;
  // The tasks this task waits for, open or done.
  blockers: TaskLink[];
  // The tasks that wait for this task.
  blocking: TaskLink[];
}

function taskLink(ref: TaskRef): TaskLink {
  return {
    id: ref.id,
    number: ref.number,
    title: ref.title,
    done: ref.done,
    href: ref.project ? taskHref(ref.project, ref) : null,
    projectName: ref.project ? ref.project.name : null,
  };
}

/** The blocking links of one task, ready to render. */
export function taskLinksView(task: Task): TaskLinksView {
  return {
    blocked: task.blocked,
    blockers: task.blockers.map(taskLink),
    blocking: task.blocking.map(taskLink),
  };
}

// --- the description disclosure -------------------------------------------------
// A task list shows the title row only, so twelve tasks fit on one screen. The
// description sits behind a control that starts closed on every row, on first
// load and after a reload. One open row says nothing about the others.

export interface TaskNotesView {
  // True when the task has a description for the control to show.
  hasNotes: boolean;
  // True while the description is visible. A task with no description is
  // never open.
  open: boolean;
  // The text on the control.
  label: string;
  // The hint on the control, which also names its action.
  title: string;
}

/** The description of one task, ready to render. */
export function taskNotesView(task: { notes: string }, open: boolean): TaskNotesView {
  const hasNotes = task.notes.trim().length > 0;
  const shown = hasNotes && open;
  return {
    hasNotes,
    open: shown,
    label: shown ? "▾ note" : "▸ note",
    title: shown ? "Hide the note" : "Show the note",
  };
}

// --- the search ----------------------------------------------------------------

export interface SearchTaskView {
  id: string;
  // The task number, shown on the row and held by the link.
  number: number;
  title: string;
  href: string;
  done: boolean;
  // True while the task waits for an open blocker.
  blocked: boolean;
  // The project and the group the task is in: "Kitchen · Phase 1".
  whereLabel: string;
}

export interface SearchView {
  // What to say in place of the results: an invitation, or no match.
  message: string | null;
  projects: CardView[];
  tasks: SearchTaskView[];
}

export function searchTaskView(found: SearchTask): SearchTaskView {
  return {
    id: found.task.id,
    number: found.task.number,
    title: found.task.title,
    href: taskHref(found.project, found.task),
    done: found.task.done,
    blocked: found.task.blocked,
    whereLabel: `${found.project.name} · ${found.group ? found.group.name : "backlog"}`,
  };
}

/**
 * The two kinds of match, apart. ``result`` is null while the request is
 * still in flight, or when the query is too short to send.
 */
export function searchView(query: string, result: SearchResult | null): SearchView {
  const text = query.trim();
  if (!text) {
    return { message: "Type a word to search every project and task.", projects: [], tasks: [] };
  }
  if (!result) return { message: "Searching…", projects: [], tasks: [] };
  if (result.projects.length === 0 && result.tasks.length === 0) {
    return { message: `Nothing matches “${text}”.`, projects: [], tasks: [] };
  }
  return {
    message: null,
    projects: result.projects.map(projectCardView),
    tasks: result.tasks.map(searchTaskView),
  };
}
