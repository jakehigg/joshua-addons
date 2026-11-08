// Unit tests for the home page: the route, the order of the projects, the
// finished-projects toggle, and what each card and each found row shows.
// They run on node's own test runner, with node's type stripping, so the
// web UI needs no test package: `npm test` in this directory.

import assert from "node:assert/strict";
import test from "node:test";

import type { Group, Project, SearchResult, Task, TaskRef } from "./api.ts";
import {
  dashboardMessage,
  dashboardProjects,
  homeHref,
  orderProjects,
  parseRoute,
  projectCardView,
  searchView,
  taskLinksView,
} from "./dashboard.ts";

function project(fields: Partial<Project> & { name: string }): Project {
  return {
    id: `id-${fields.name}`,
    slug: fields.name.toLowerCase().replaceAll(" ", "-"),
    goal: null,
    target_date: null,
    status: "active",
    completed_at: null,
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

test("the bare path is the dashboard, with no search and both toggles off", () => {
  assert.deepEqual(parseRoute("/"), {
    project: null,
    query: "",
    includeDone: false,
    showFinished: false,
  });
});

test("the route carries the project slug, the search text, and the toggles", () => {
  assert.equal(parseRoute("/p/paint-the-hall").project, "paint-the-hall");
  assert.equal(parseRoute("/p/paint-the-hall/").project, "paint-the-hall");
  const searching = parseRoute("/?q=paint%20tin&done=1&finished=1");
  assert.deepEqual(searching, {
    project: null,
    query: "paint tin",
    includeDone: true,
    showFinished: true,
  });
});

test("homeHref keeps only the parts that are set", () => {
  assert.equal(homeHref({ query: "", includeDone: false, showFinished: false }), "/");
  assert.equal(
    homeHref({ query: "paint tin", includeDone: true, showFinished: true }),
    "/?q=paint+tin&done=1&finished=1",
  );
  const route = parseRoute(homeHref({ query: "tin", includeDone: false, showFinished: true }));
  assert.deepEqual(route, {
    project: null,
    query: "tin",
    includeDone: false,
    showFinished: true,
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

// --- the finished-projects toggle ----------------------------------------------

test("the dashboard drops the done and the closed projects until the toggle is on", () => {
  const projects = [
    project({ name: "active", health: "on_track", task_count: 1, open_count: 1 }),
    project({ name: "done", status: "done", health: "done", task_count: 1, done_count: 1 }),
    project({ name: "closed", status: "closed", health: "closed" }),
  ];
  assert.deepEqual(names(dashboardProjects(projects, false)), ["active"]);
  assert.deepEqual(names(dashboardProjects(projects, true)), ["active", "done", "closed"]);
});

test("a dashboard with nothing to show says why", () => {
  const finished = [project({ name: "done", status: "done", health: "done" })];
  assert.match(dashboardMessage(finished, [], false) ?? "", /Show finished/);
  assert.equal(dashboardMessage(finished, finished, true), null);
  // No projects at all: the "create your first project" hero says it instead.
  assert.equal(dashboardMessage([], [], false), null);
});

// --- one card -------------------------------------------------------------------

test("a card shows the goal, the percent, the counts, the health, the date, and the next task", () => {
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
  assert.equal(view.goal, "A painted hallway");
  assert.equal(view.percent, 40);
  assert.equal(view.percentLabel, "40%");
  assert.equal(view.countLabel, "2/5 done · 3 open");
  assert.deepEqual(view.badge, { text: "on track", kind: "on_track" });
  assert.equal(view.targetLabel, "target 2026-02-01");
  assert.equal(view.nextTitle, "Buy the paint");
  assert.equal(view.finished, false);
});

test("a project with no tasks says so in place of an empty badge", () => {
  const view = projectCardView(project({ name: "Fresh" }));
  assert.deepEqual(view.badge, { text: "no tasks yet", kind: "none" });
  assert.equal(view.countLabel, "0/0 done · 0 open");
  assert.equal(view.goal, null);
  assert.equal(view.targetLabel, null);
  assert.equal(view.nextTitle, null);
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
      { task: task({ id: "t1", title: "Buy the paint" }), project: hallway, group: group("Prep") },
      {
        task: task({ id: "t2", title: "Paint the trim", done: true }),
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
      title: "Buy the paint",
      href: "/p/hallway#task-t1",
      done: false,
      blocked: false,
      whereLabel: "Hallway · Prep",
    },
    {
      id: "t2",
      title: "Paint the trim",
      href: "/p/hallway#task-t2",
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
        ref({ id: "t1", title: "Buy the paint", done: true }),
        ref({
          id: "t9",
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
      title: "Buy the paint",
      done: true,
      href: "/p/hallway#task-t1",
      projectName: "Hallway",
    },
    {
      id: "t9",
      title: "Collect the keys",
      done: false,
      href: "/p/garage#task-t9",
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
      blocking: [ref({ id: "t2", title: "Paint the trim", project: null })],
    }),
  );
  assert.equal(view.blocked, false);
  assert.deepEqual(view.blocking, [
    { id: "t2", title: "Paint the trim", done: false, href: null, projectName: null },
  ]);
});
