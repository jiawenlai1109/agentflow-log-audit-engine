import client from "./client";

export const api = {
  login: (username: string, password: string) =>
    client.post("/api/auth/login", { username, password }),

  listDatasets: () => client.get("/api/datasets"),
  uploadDataset: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return client.post("/api/datasets", form);
  },
  deleteDataset: (id: number) => client.delete(`/api/datasets/${id}`),

  analyze: (payload: { question: string; dataset_id: number; mode: string; session_id?: string }) =>
    client.post("/api/analyze", payload),
  getJob: (jobId: string) => client.get(`/api/jobs/${jobId}`),

  createSession: (payload: { title: string; dataset_id?: number }) =>
    client.post("/api/sessions", payload),
  listSessions: () => client.get("/api/sessions"),
  sessionMessages: (sessionId: string) => client.get(`/api/sessions/${sessionId}/messages`),
  postMessage: (sessionId: string, payload: { question: string; mode: string }) =>
    client.post(`/api/sessions/${sessionId}/messages`, payload),
  deleteSession: (sessionId: string) => client.delete(`/api/sessions/${sessionId}`),

  listRuns: () => client.get("/api/runs"),
  getReport: (runId: string) => client.get(`/api/reports/${runId}`),
  evaluationSummary: () => client.get("/api/evaluations/summary"),
};
