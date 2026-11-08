export type ProjectStatus = "active" | "paused" | "done" | "closed";
export type TaskSize = "XS" | "S" | "M" | "L" | "XL";
export const TASK_SIZES: TaskSize[] = ["XS", "S", "M", "L", "XL"];

// One end of a blocking link: enough of that task to name it and open it.
// project is null only when the linked task lost its project.
export interface TaskRef {
  id: string;
  title: string;
  done: boolean;
  project: { name: string; slug: string } | null;
}

export interface Task {
  id: string;
  project_id: string;
  group_id: string | null;
  title: string;
  notes: string;
  done: boolean;
  done_at: string | null;
  size: TaskSize | null;
  due_date: string | null;
  assignee: string | null;
  position: number;
  // Computed by the server from the blocking links on every read. blocked is
  // true while at least one blocker is open. A blocker can be in another
  // project.
  blocked: boolean;
  blockers: TaskRef[];
  blocking: TaskRef[];
}

// A project summary: the stored fields plus the computed ones. percent,
// health, and next_task are computed by the server on every read.
export interface Project {
  id: string;
  name: string;
  slug: string;
  goal: string | null;
  target_date: string | null;
  status: ProjectStatus;
  completed_at: string | null;
  task_count: number;
  done_count: number;
  open_count: number;
  percent: number;
  health: string | null;
  next_task: Task | null;
}

export interface ProjectPatch {
  name?: string;
  goal?: string | null;
  target_date?: string | null;
  status?: ProjectStatus;
}

export interface Group {
  id: string;
  project_id: string;
  name: string;
  position: number;
  target_date: string | null;
  task_count: number;
  done_count: number;
  percent: number;
}

// One task a search found: the task, its project, and its group (null for a
// task in the project's backlog).
export interface SearchTask {
  task: Task;
  project: Project;
  group: Group | null;
}

// The two kinds of match, apart: the projects, then the tasks.
export interface SearchResult {
  projects: Project[];
  tasks: SearchTask[];
}

export interface TaskPatch {
  title?: string;
  notes?: string;
  size?: TaskSize | null;
  due_date?: string | null;
  assignee?: string | null;
}

// Per-tab id sent with every request; the server echoes it in /api/events
// so this tab can ignore the changes it made itself.
export const clientId = crypto.randomUUID();

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { "X-Client-Id": clientId };
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const res = await fetch(path, {
    method,
    headers,
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    throw new Error(`${method} ${path} failed: ${res.status}`);
  }
  return res.status === 204 ? (undefined as T) : res.json();
}

export const api = {
  listProjects: () => request<Project[]>("GET", "/api/projects"),
  createProject: (name: string, goal?: string | null, targetDate?: string | null) =>
    request<Project>("POST", "/api/projects", {
      name,
      goal: goal ?? null,
      target_date: targetDate ?? null,
    }),
  updateProject: (projectId: string, patch: ProjectPatch) =>
    request<Project>("PATCH", `/api/projects/${projectId}`, patch),
  listTasks: (projectId: string) =>
    request<Task[]>("GET", `/api/projects/${projectId}/tasks`),
  listGroups: (projectId: string) =>
    request<Group[]>("GET", `/api/projects/${projectId}/groups`),
  createGroup: (projectId: string, name: string) =>
    request<Group>("POST", `/api/projects/${projectId}/groups`, { name }),
  renameGroup: (groupId: string, name: string) =>
    request<Group>("PATCH", `/api/groups/${groupId}`, { name }),
  setGroupTargetDate: (groupId: string, targetDate: string | null) =>
    request<Group>("PATCH", `/api/groups/${groupId}`, { target_date: targetDate }),
  deleteGroup: (groupId: string) => request<void>("DELETE", `/api/groups/${groupId}`),
  reorderGroups: (projectId: string, orderedIds: string[]) =>
    request<Group[]>("PUT", `/api/projects/${projectId}/groups/order`, {
      ordered_ids: orderedIds,
    }),
  createTask: (projectId: string, title: string, groupId?: string | null) =>
    request<Task>("POST", `/api/projects/${projectId}/tasks`, {
      title,
      group_id: groupId ?? null,
    }),
  setDone: (taskId: string, done: boolean) =>
    request<Task>("PATCH", `/api/tasks/${taskId}`, { done }),
  updateTitle: (taskId: string, title: string) =>
    request<Task>("PATCH", `/api/tasks/${taskId}`, { title }),
  updateNotes: (taskId: string, notes: string) =>
    request<Task>("PATCH", `/api/tasks/${taskId}`, { notes }),
  updateTask: (taskId: string, patch: TaskPatch) =>
    request<Task>("PATCH", `/api/tasks/${taskId}`, patch),
  deleteTask: (taskId: string) => request<void>("DELETE", `/api/tasks/${taskId}`),
  unblockTask: (taskId: string, blockerId: string) =>
    request<Task>("DELETE", `/api/tasks/${taskId}/blockers/${blockerId}`),
  // include_done also decides whether done and closed projects are searched.
  search: (query: string, includeDone: boolean) =>
    request<SearchResult>(
      "GET",
      `/api/search?query=${encodeURIComponent(query)}&include_done=${includeDone}`,
    ),
  reorder: (projectId: string, orderedIds: string[], groupId?: string | null) =>
    request<Task[]>("PUT", `/api/projects/${projectId}/tasks/order`, {
      ordered_ids: orderedIds,
      group_id: groupId ?? null,
    }),
};
