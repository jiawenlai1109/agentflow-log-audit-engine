import client from "./client";
import { withRetry } from "./backoff";

const CHUNK_BYTES = 4 * 1024 * 1024;

/** 一次"提交"的幂等键（P5-1 的那把锁）。形状服从后端的判据：1-200 个可见字符、无空格。 */
export function newIdempotencyKey(): string {
  const random =
    (globalThis.crypto?.randomUUID?.() as string | undefined) ||
    Array.from({ length: 32 }, () => Math.floor(Math.random() * 36).toString(36)).join("");
  return `sub_${random.replace(/-/g, "").slice(0, 32)}`;
}

/** 创建作业的请求：重试必须带**同一个键**（键在外面算一次，不进 task 里）。
 *  写反了的后果很具体——网络抖一下，同一次提问变成两个作业、双份上游调用、两份报告。 */
function submitWithKey(path: string, payload: unknown, key: string, signal?: AbortSignal) {
  return withRetry(() => client.post(path, payload, { headers: { "Idempotency-Key": key } }), { signal });
}

/** 客户端生成的 upload_id：后端的形状校验是 ^[A-Za-z0-9_-]{8,64}$，这里必须服从。 */
function newUploadId(): string {
  const random = Array.from({ length: 24 }, () => Math.floor(Math.random() * 36).toString(36)).join("");
  return `up_${random}`;
}

export const api = {
  login: (username: string, password: string) =>
    client.post("/api/auth/login", { username, password }),

  /** 当前身份：后端回 username / role / orgs。role 与企业在本地的副本只用来显示，
   *  判定一律留在服务端——前端藏不住判据，这里也不假装能。 */
  me: () => client.get("/api/auth/me"),

  /** 同企业成员名单（后端按调用者的企业过滤；没有企业的人拿到空列表）。 */
  listMembers: () => client.get("/api/users"),
  /** 企业名单：只给管理员，建号时选归属用。 */
  listOrgs: () => client.get("/api/orgs"),
  /** 建号（只给管理员）：org 留空 = 该账号未归属，只能看见自己的资源。 */
  createAccount: (payload: { username: string; password: string; org?: string }) =>
    client.post("/api/users", payload),

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

  /** 提交分析。默认自己生成一个幂等键并按退避重试；调用方也可以把"这一次提交"的键传进来，
   *  让它跟同一次用户意图共享（比如先在 Workbench 提交、失败后再由界面重试按钮重发）。 */
  analyze: (
    payload: {
      question: string;
      mode: string;
      dataset_id?: number;
      bundle_id?: string;
      session_id?: string;
    },
    options: { idempotencyKey?: string; signal?: AbortSignal } = {}
  ) =>
    submitWithKey(
      "/api/analyze",
      payload,
      options.idempotencyKey || newIdempotencyKey(),
      options.signal
    ),
  /** 状态查询：断线与"服务正在重启"是这一片最常见的两种瞬时失败，所以也走同一份退避。 */
  getJob: (jobId: string, options: { signal?: AbortSignal } = {}) =>
    withRetry(() => client.get(`/api/jobs/${jobId}`), options),

  createSession: (payload: { title: string; dataset_id?: number }) =>
    client.post("/api/sessions", payload),
  listSessions: () => client.get("/api/sessions"),
  sessionMessages: (sessionId: string) => client.get(`/api/sessions/${sessionId}/messages`),
  /** 会话续轮同样创建作业，所以同样要有键——只在 /api/analyze 上装护栏，
   *  "重试不会重复"这条承诺就只对一半的请求成立。 */
  postMessage: (
    sessionId: string,
    payload: { question: string; mode: string },
    options: { idempotencyKey?: string; signal?: AbortSignal } = {}
  ) =>
    submitWithKey(
      `/api/sessions/${sessionId}/messages`,
      payload,
      options.idempotencyKey || newIdempotencyKey(),
      options.signal
    ),
  deleteSession: (sessionId: string) => client.delete(`/api/sessions/${sessionId}`),

  listRuns: () => client.get("/api/runs"),
  getReport: (runId: string) => client.get(`/api/reports/${runId}`),
  evaluationSummary: () => client.get("/api/evaluations/summary"),
};
