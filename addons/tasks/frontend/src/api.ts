export interface Project {
  id: string;
  name: string;
  slug: string;
  task_count: number;
  done_count: number;
}

export interface Group {
  id: string;
  project_id: string;
  name: string;
  position: number;
  task_count: number;
  done_count: number;
}

export interface Task {
  id: string;
  project_id: string;
  group_id: string | null;
  title: string;
  notes: string;
  done: boolean;
  done_at: string | null;
  position: number;
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
  createProject: (name: string) =>
    request<Project>("POST", "/api/projects", { name }),
  listTasks: (projectId: string) =>
    request<Task[]>("GET", `/api/projects/${projectId}/tasks`),
  listGroups: (projectId: string) =>
    request<Group[]>("GET", `/api/projects/${projectId}/groups`),
  createGroup: (projectId: string, name: string) =>
    request<Group>("POST", `/api/projects/${projectId}/groups`, { name }),
  renameGroup: (groupId: string, name: string) =>
    request<Group>("PATCH", `/api/groups/${groupId}`, { name }),
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
  deleteTask: (taskId: string) => request<void>("DELETE", `/api/tasks/${taskId}`),
  reorder: (projectId: string, orderedIds: string[], groupId?: string | null) =>
    request<Task[]>("PUT", `/api/projects/${projectId}/tasks/order`, {
      ordered_ids: orderedIds,
      group_id: groupId ?? null,
    }),
};
