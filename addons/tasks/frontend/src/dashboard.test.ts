// Unit tests for the home page: the route, the order of the projects, the
// open list, the finished and archived page, and what each card and each
// found row shows.
// They run on node's own test runner, with node's type stripping, so the
// web UI needs no test package: `npm test` in this directory.

import assert from "node:assert/strict";
import test from "node:test";

import type { Group, Project, SearchResult, Task, TaskRef } from "./api.ts";
import {
  archiveHref,
  archiveRowView,
  dashboardMessage,
  dashboardProjects,
  finishedAndArchived,
  homeHref,
  orderProjects,
  parseRoute,
  projectCardView,
  projectOptions,
  searchView,
  taskHref,
  taskLinksView,
  taskNotesView,
} from "./dashboard.ts";

function project(fields: Partial<Project> & { name: string }): Project {
  return {
    id: `id-${fields.name}`,
    slug: fields.name.toLowerCase().replaceAll(" ", "-"),
    goal: null,
    target_date: null,
    status: "active",
    completed_at: null,
    archived_at: null,
    task_count: 0,
    done_count: 0,
    open_count: 0,
    percent: 0,
    health: null,
    next_task: null,
    ...fields,
  };
}

function task(fields: Partial<Task> & { id: string; title: string }): Task {
  return {
    number: 1,
    project_id: "a-project",
    group_id: null,
    notes: "",
    done: false,
    done_at: null,
    size: null,
    due_date: null,
    assignee: null,
    position: 1024,
    blocked: false,
    blockers: [],
    blocking: [],
    ...fields,
  };
}

function ref(fields: Partial<TaskRef> & { id: string; title: string }): TaskRef {
  return {
    number: 1,
    done: false,
    project: { name: "Hallway", slug: "hallway" },
    ...fields,
  };
}

function group(name: string): Group {
  return {
    id: `group-${name}`,
    project_id: "a-project",
    name,
    position: 1024,
    target_date: null,
    task_count: 0,
    done_count: 0,
    percent: 0,
  };
}

// --- the route ----------------------------------------------------------------

test("the bare path is the dashboard, with no search and the toggle off", () => {
  assert.deepEqual(parseRoute("/"), {
    project: null,
    archive: false,
    query: "",
    includeDone: false,
  });
});

test("the route carries the project slug, the search text, and its toggle", () => {
  assert.equal(parseRoute("/p/paint-the-hall").project, "paint-the-hall");
  assert.equal(parseRoute("/p/paint-the-hall/").project, "paint-the-hall");
  const searching = parseRoute("/?q=paint%20tin&done=1");
  assert.deepEqual(searching, {
    project: null,
    archive: false,
    query: "paint tin",
    includeDone: true,
  });
});

test("the finished and archived page is its own route, not a project", () => {
  assert.equal(archiveHref(), "/archive");
  const route = parseRoute(archiveHref());
  assert.equal(route.archive, true);
  assert.equal(route.project, null);
  assert.equal(parseRoute("/archive/").archive, true);
  assert.equal(parseRoute("/").archive, false);
  assert.equal(parseRoute("/p/hallway").archive, false);
});

test("homeHref keeps only the parts that are set, and no finished parameter", () => {
  assert.equal(homeHref({ query: "", includeDone: false }), "/");
  assert.equal(homeHref({ query: "paint tin", includeDone: true }), "/?q=paint+tin&done=1");
  const route = parseRoute(homeHref({ query: "tin", includeDone: false }));
  assert.deepEqual(route, {
    project: null,
    archive: false,
    query: "tin",
    includeDone: false,
  });
});

// --- the order ----------------------------------------------------------------

function names(projects: Project[]): string[] {
  return projects.map((p) => p.name);
}

test("a project that needs a person comes before one that runs itself", () => {
  const ordered = orderProjects([
    project({ name: "fine", health: "on_track", task_count: 2, open_count: 1 }),
    project({ name: "late", health: "overdue", task_count: 2, open_count: 1 }),
    project({ name: "quiet", health: "stalled", task_count: 2, open_count: 1 }),
    project({ name: "ready", health: "ready_to_complete", task_count: 2 }),
  ]);
  assert.deepEqual(names(ordered), ["late", "quiet", "ready", "fine"]);
});

test("a plain list, then an empty project, then paused, then finished, come last", () => {
  const ordered = orderProjects([
    project({ name: "closed", status: "closed", health: "closed" }),
    project({ name: "empty" }),
    project({ name: "paused", status: "paused", health: "paused" }),
    project({ name: "list", task_count: 3, done_count: 1, open_count: 2 }),
    project({ name: "goal", health: "on_track", task_count: 1, open_count: 1 }),
  ]);
  assert.deepEqual(names(ordered), ["goal", "list", "empty", "paused", "closed"]);
});

test("same rank: the nearer target date first, no target date last", () => {
  const ordered = orderProjects([
    project({ name: "no date", health: "on_track", task_count: 1, open_count: 1 }),
    project({ name: "december", health: "on_track", target_date: "2026-12-01", task_count: 1 }),
    project({ name: "january", health: "on_track", target_date: "2026-01-05", task_count: 1 }),
  ]);
  assert.deepEqual(names(ordered), ["january", "december", "no date"]);
});

test("same rank and same target date: the order the API returned", () => {
  const given = [
    project({ name: "older", health: "on_track", task_count: 1, open_count: 1 }),
    project({ name: "newer", health: "on_track", task_count: 1, open_count: 1 }),
  ];
  assert.deepEqual(names(orderProjects(given)), ["older", "newer"]);
  assert.deepEqual(names(orderProjects([...given].reverse())), ["newer", "older"]);
});

// --- the home list, and the finished and archived page ------------------------

const ARCHIVED = project({
  name: "shelf",
  archived_at: "2026-02-03T09:00:00Z",
  task_count: 2,
  open_count: 2,
});
const DONE = project({
  name: "done",
  status: "done",
  health: "done",
  completed_at: "2026-02-10T09:00:00Z",
  task_count: 1,
  done_count: 1,
});
const CLOSED = project({
  name: "closed",
  status: "closed",
  health: "closed",
  completed_at: "2026-01-05T09:00:00Z",
});
const OPEN = project({ name: "active", health: "on_track", task_count: 1, open_count: 1 });

test("the home page lists the open projects only", () => {
  assert.deepEqual(names(dashboardProjects([OPEN, DONE, CLOSED, ARCHIVED])), ["active"]);
  // a paused project is still open: it is not finished, and not archived
  const paused = project({ name: "paused", status: "paused", health: "paused" });
  assert.deepEqual(names(dashboardProjects([OPEN, paused])), ["active", "paused"]);
});

test("the finished and archived page lists both, the most recent first", () => {
  assert.deepEqual(names(finishedAndArchived([OPEN, CLOSED, ARCHIVED, DONE])), [
    "done",
    "shelf",
    "closed",
  ]);
  assert.deepEqual(finishedAndArchived([OPEN]), []);
});

test("a project selector offers neither a finished project nor an archived one", () => {
  assert.deepEqual(names(projectOptions([OPEN, DONE, CLOSED, ARCHIVED], null)), ["active"]);
  // the project a person has open keeps its place, whatever its status
  assert.deepEqual(names(projectOptions([OPEN, DONE, ARCHIVED], ARCHIVED.id)), [
    "active",
    "shelf",
  ]);
  assert.deepEqual(names(projectOptions([OPEN, DONE], DONE.id)), ["active", "done"]);
  // the open project a person has open is in the list once
  assert.deepEqual(names(projectOptions([OPEN, DONE], OPEN.id)), ["active"]);
});

test("a row of the finished and archived page names the status and the date", () => {
  const archived = archiveRowView(ARCHIVED);
  assert.deepEqual(archived, {
    id: ARCHIVED.id,
    href: "/p/shelf",
    name: "shelf",
    statusLabel: "active",
    dateLabel: "2026-02-03",
    archived: true,
  });
  const done = archiveRowView(DONE);
  assert.equal(done.statusLabel, "done");
  assert.equal(done.dateLabel, "2026-02-10");
  assert.equal(done.archived, false);
  // a project the API has no date for says nothing in place of one
  assert.equal(archiveRowView(project({ name: "old", status: "closed" })).dateLabel, null);
});

test("a dashboard with no open project says why", () => {
  const none = [DONE, ARCHIVED];
  assert.equal(dashboardMessage(none, []), "Every project is finished or archived.");
  assert.equal(dashboardMessage(none, [OPEN]), null);
  // No projects at all: the "create your first project" hero says it instead.
  assert.equal(dashboardMessage([], []), null);
});

// --- one card -------------------------------------------------------------------

test("a card shows the name, the percent, the counts, the health, and the date", () => {
  const next = task({ id: "t1", title: "Buy the paint" });
  const view = projectCardView(
    project({
      name: "Hallway",
      goal: "A painted hallway",
      target_date: "2026-02-01",
      task_count: 5,
      done_count: 2,
      open_count: 3,
      percent: 40,
      health: "on_track",
      next_task: next,
    }),
  );
  assert.equal(view.href, "/p/hallway");
  assert.equal(view.name, "Hallway");
  assert.equal(view.percent, 40);
  assert.equal(view.percentLabel, "40%");
  assert.equal(view.countLabel, "2/5 done · 3 open");
  assert.deepEqual(view.badge, { text: "on track", kind: "on_track" });
  assert.equal(view.targetLabel, "target 2026-02-01");
  assert.equal(view.finished, false);
});

test("a card is a link with no archive control and no long text on it", () => {
  const view = projectCardView(
    project({
      name: "Hallway",
      goal: "A long goal that would fill the card and push the next one off the screen",
      next_task: task({ id: "t1", title: "Buy the paint" }),
      task_count: 1,
      open_count: 1,
    }),
  );
  assert.deepEqual(Object.keys(view).sort(), [
    "badge",
    "countLabel",
    "finished",
    "href",
    "id",
    "name",
    "percent",
    "percentLabel",
    "targetLabel",
  ]);
});

test("a project with no tasks says so in place of an empty badge", () => {
  const view = projectCardView(project({ name: "Fresh" }));
  assert.deepEqual(view.badge, { text: "no tasks yet", kind: "none" });
  assert.equal(view.countLabel, "0/0 done · 0 open");
  assert.equal(view.targetLabel, null);
});

test("a plain list has no badge, and keeps its done/total count", () => {
  const view = projectCardView(
    project({ name: "Shopping", task_count: 4, done_count: 1, open_count: 3, percent: 25 }),
  );
  assert.equal(view.badge, null);
  assert.equal(view.countLabel, "1/4 done · 3 open");
  assert.equal(view.percentLabel, "25%");
});

test("a finished project keeps its status as the badge", () => {
  const view = projectCardView(
    project({ name: "Garage", status: "done", health: "done", task_count: 2, done_count: 2 }),
  );
  assert.deepEqual(view.badge, { text: "done", kind: "done" });
  assert.equal(view.finished, true);
});

// --- the search -----------------------------------------------------------------

test("an empty search invites one instead of showing nothing", () => {
  const view = searchView("  ", null);
  assert.match(view.message ?? "", /Type a word/);
  assert.deepEqual(view.projects, []);
  assert.deepEqual(view.tasks, []);
});

test("a search with no match says so", () => {
  const view = searchView("zebra", { projects: [], tasks: [] });
  assert.equal(view.message, "Nothing matches “zebra”.");
});

test("a search shows the matching projects and the matching tasks apart", () => {
  const hallway = project({
    name: "Hallway",
    goal: "A painted hallway",
    task_count: 2,
    done_count: 1,
    open_count: 1,
    percent: 50,
    health: "on_track",
  });
  const result: SearchResult = {
    projects: [hallway],
    tasks: [
      {
        task: task({ id: "t1", number: 24, title: "Buy the paint" }),
        project: hallway,
        group: group("Prep"),
      },
      {
        task: task({ id: "t2", number: 25, title: "Paint the trim", done: true }),
        project: hallway,
        group: null,
      },
    ],
  };
  const view = searchView("paint", result);
  assert.equal(view.message, null);
  assert.deepEqual(
    view.projects.map((p) => [p.name, p.href]),
    [["Hallway", "/p/hallway"]],
  );
  assert.deepEqual(view.tasks, [
    {
      id: "t1",
      number: 24,
      title: "Buy the paint",
      href: "/p/hallway#task-24",
      done: false,
      blocked: false,
      whereLabel: "Hallway · Prep",
    },
    {
      id: "t2",
      number: 25,
      title: "Paint the trim",
      href: "/p/hallway#task-25",
      done: true,
      blocked: false,
      whereLabel: "Hallway · backlog",
    },
  ]);
});

test("a found task that waits for an open blocker says it is blocked", () => {
  const hallway = project({ name: "Hallway", task_count: 1, open_count: 1 });
  const waiting = task({ id: "t1", title: "Paint the trim", blocked: true });
  const view = searchView("paint", {
    projects: [],
    tasks: [{ task: waiting, project: hallway, group: null }],
  });
  assert.equal(view.tasks[0].blocked, true);
});

// --- the description disclosure -------------------------------------------------

test("a task with a description keeps it closed until a person opens it", () => {
  const long = task({ id: "t1", title: "Build the API", notes: "A long description." });
  const closed = taskNotesView(long, false);
  assert.equal(closed.hasNotes, true);
  assert.equal(closed.open, false);
  assert.equal(closed.title, "Show the note");
  const open = taskNotesView(long, true);
  assert.equal(open.open, true);
  assert.equal(open.title, "Hide the note");
  assert.notEqual(open.label, closed.label);
});

test("a task with no description, or only spaces, shows no control", () => {
  assert.equal(taskNotesView(task({ id: "t1", title: "Buy the paint" }), false).hasNotes, false);
  const blank = taskNotesView(task({ id: "t2", title: "Paint the trim", notes: "  \n" }), true);
  assert.equal(blank.hasNotes, false);
  // Nothing to show, so the row is never open.
  assert.equal(blank.open, false);
});

// --- the blocking links ---------------------------------------------------------

test("a task with no links is not blocked and shows no link", () => {
  assert.deepEqual(taskLinksView(task({ id: "t1", title: "Buy the paint" })), {
    blocked: false,
    blockers: [],
    blocking: [],
  });
});

test("a blocked task names each blocker, with a link to it", () => {
  const view = taskLinksView(
    task({
      id: "t2",
      title: "Paint the trim",
      blocked: true,
      blockers: [
        ref({ id: "t1", number: 24, title: "Buy the paint", done: true }),
        ref({
          id: "t9",
          number: 9,
          title: "Collect the keys",
          project: { name: "Garage", slug: "garage" },
        }),
      ],
    }),
  );
  assert.equal(view.blocked, true);
  assert.deepEqual(view.blockers, [
    {
      id: "t1",
      number: 24,
      title: "Buy the paint",
      done: true,
      href: "/p/hallway#task-24",
      projectName: "Hallway",
    },
    {
      id: "t9",
      number: 9,
      title: "Collect the keys",
      done: false,
      href: "/p/garage#task-9",
      projectName: "Garage",
    },
  ]);
  assert.deepEqual(view.blocking, []);
});

test("a task says what waits for it, and a link with no project opens nothing", () => {
  const view = taskLinksView(
    task({
      id: "t1",
      title: "Buy the paint",
      blocking: [ref({ id: "t2", number: 25, title: "Paint the trim", project: null })],
    }),
  );
  assert.equal(view.blocked, false);
  assert.deepEqual(view.blocking, [
    { id: "t2", number: 25, title: "Paint the trim", done: false, href: null, projectName: null },
  ]);
});

test("a task link holds the task number, so a person can read it", () => {
  assert.equal(taskHref({ slug: "hallway" }, { number: 24 }), "/p/hallway#task-24");
});
