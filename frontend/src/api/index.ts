import client from "./client";

const CHUNK_BYTES = 4 * 1024 * 1024;

/** 客户端生成的 upload_id：后端的形状校验是 ^[A-Za-z0-9_-]{8,64}$，这里必须服从。 */
function newUploadId(): string {
  const random = Array.from({ length: 24 }, () => Math.floor(Math.random() * 36).toString(36)).join("");
  return `up_${random}`;
}

export const api = {
  login: (username: string, password: string) =>
    client.post("/api/auth/login", { username, password }),

  listDatasets: () => client.get("/api/datasets"),
  /** 型号可用性的只读预检结论。页面只看不动上游——一次真实调用只能由预检脚本发起。 */
  llmModels: () => client.get("/api/llm/models"),
  uploadDataset: (file: File) => {
    const form = new FormData();
    form.append("file", file);
    return client.post("/api/datasets", form);
  },
  deleteDataset: (id: number) => client.delete(`/api/datasets/${id}`),

  listBundles: () => client.get("/api/bundles"),
  getBundle: (bundleId: string) => client.get(`/api/bundles/${bundleId}`),
  previewBundle: (bundleId: string, tableRef = "t1", rows = 20) =>
    client.get(`/api/bundles/${bundleId}/preview`, { params: { table_ref: tableRef, rows } }),
  deleteBundle: (bundleId: string) => client.delete(`/api/bundles/${bundleId}`),

  /** 建包：小文件走 multipart 直传，大文件切片走 /chunks 再让后端按清单重组。 */
  async createBundle(files: File[], options: { name?: string; asyncParse?: boolean } = {}) {
    const form = new FormData();
    const specs: { upload_id: string; filename: string; total: number }[] = [];
    for (const file of files) {
      if (file.size > CHUNK_BYTES) {
        const uploadId = newUploadId();
        const total = Math.ceil(file.size / CHUNK_BYTES);
        for (let index = 0; index < total; index += 1) {
          const chunkForm = new FormData();
          chunkForm.append("upload_id", uploadId);
          chunkForm.append("index", String(index));
          chunkForm.append("total", String(total));
          chunkForm.append("filename", file.name);
          chunkForm.append("file", file.slice(index * CHUNK_BYTES, (index + 1) * CHUNK_BYTES), `${index}.part`);
          await client.post("/api/bundles/chunks", chunkForm);
        }
        specs.push({ upload_id: uploadId, filename: file.name, total });
      } else {
        form.append("files", file);
      }
    }
    if (specs.length) form.append("uploads", JSON.stringify(specs));
    if (options.name) form.append("name", options.name);
    if (options.asyncParse) form.append("async_parse", "true");
    return client.post("/api/bundles", form);
  },

  analyze: (payload: {
    question: string;
    mode: string;
    dataset_id?: number;
    bundle_id?: string;
    session_id?: string;
  }) => client.post("/api/analyze", payload),
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
