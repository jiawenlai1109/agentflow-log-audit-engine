/** 带 Authorization 头的进度流读取，以及**断了之后自己接上**（P5-2）。
 *
 * 为什么用 fetch + ReadableStream 而不是 EventSource：EventSource 设不了请求头，
 * 把 token 拼进 query 会进访问日志与浏览器历史。
 *
 * 为什么这里要自己重连：P2 的对照读数写着——杀掉受理进程那一刻 **39 个作业仍在跑**、
 * worker 在 26.3s 内把它们排空，**结果一个没丢**，可用户那边是 **40 次轮询失败**。
 * "作业没丢"与"用户当时看不到"是两笔账。这一片修的就是后者：
 * 流断了就按退避重连，并用 `Last-Event-ID` 告诉服务端"我看到第几帧了"，
 * 于是重连之后**补齐**而不是从头重放（服务端那条协议 P2 就实现了，此前没有客户端用它）。
 *
 * 三条不退避的情况，各有反面：
 * - 用户取消/切走（abort）：继续重试等于替一个已经离开页面的人占用连接；
 * - 401/403：登录态的问题，重试不会变好，交给 client.ts 那一道统一登出；
 * - 作业已经到终态：没东西可接了，再连一次只是多打一遍服务端。
 */

import { isAbort, reconnectDelay, shouldRetry, sleep } from "./backoff";
import { api } from "./index";

export type JobEvent = Record<string, unknown> & { type?: string };

export interface StreamOptions {
  signal?: AbortSignal;
  /** 每次准备重连时告诉界面一声。静默重连的坏处是用户看见"卡住"，而看不见"正在接回来"。 */
  onReconnect?: (info: { attempt: number; waitMs: number; reason: string }) => void;
  /** 最多重连几轮。默认沿用 backoff 的那一份策略。 */
  maxAttempts?: number;
}

/** 解一帧 SSE：把 `data:` 拼起来，同时把 `id:` 记下来当续读游标。 */
function parseFrame(frame: string): { data?: string; id?: string } {
  let id: string | undefined;
  const payload = frame
    .split("\n")
    .filter((line) => {
      if (line.startsWith("id:")) {
        id = line.slice(3).trim();
        return false;
      }
      return line.startsWith("data:");
    })
    .map((line) => line.slice(5).trim())
    .join("\n");
  return payload ? { data: payload, id } : { id };
}

/** 连一次、读到断为止。返回**这一趟看到的最后一个游标**（没有新事件时返回传进来的那个）。 */
async function readOnce(
  jobId: string,
  lastEventId: string | null,
  onEvent: (event: JobEvent) => void,
  signal?: AbortSignal
): Promise<string | null> {
  const token = localStorage.getItem("token") || "";
  const headers: Record<string, string> = { Authorization: `Bearer ${token}` };
  // 续读游标只在"不是第一次"时带：第一次带个空值会让服务端以为要从 0 之后读
  if (lastEventId) headers["Last-Event-ID"] = lastEventId;
  let cursor = lastEventId;

  const response = await fetch(`/api/jobs/${jobId}/events`, { headers, signal });
  if (!response.ok || !response.body) {
    // status 挂上去，外层用同一份 shouldRetry 判"该不该再试"（429/5xx 试，401/403 不试）
    const error: any = new Error(`进度流连接失败（HTTP ${response.status}）`);
    error.response = { status: response.status, headers: { "retry-after": response.headers.get("Retry-After") } };
    throw error;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let boundary = buffer.indexOf("\n\n");
    while (boundary >= 0) {
      const frame = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      boundary = buffer.indexOf("\n\n");
      const parsed = parseFrame(frame);
      if (parsed.id) cursor = parsed.id;
      if (!parsed.data) continue;
      const event = JSON.parse(parsed.data) as JobEvent & { seq?: number };
      // 服务端把游标放在 `id:` 行里；帧内再带一份 `seq` 时以协议为准、顺手核对
      if (typeof event.seq === "number" && !parsed.id) cursor = String(event.seq);
      onEvent(event);
    }
  }
  return cursor;
}

/** 服务端说完了才算完：作业行的 `terminal` 由 `queueing.TERMINAL` 那一份名单算，
 *  前端不自己抄一份终态名单——抄的那份迟早和真的分叉，表现是"还在跑"被当成"完了"。 */
async function isTerminal(jobId: string, signal?: AbortSignal): Promise<boolean> {
  const { data } = await api.getJob(jobId, { signal });
  return data?.terminal === true;
}

export async function streamJobEvents(
  jobId: string,
  onEvent: (event: JobEvent) => void,
  signal?: AbortSignal,
  options: StreamOptions = {}
): Promise<{ reconnections: number; endedBy: "terminal" | "attempts" }> {
  const maxAttempts = options.maxAttempts ?? undefined;
  let lastEventId: string | null = null;
  let attempt = 0;
  for (;;) {
    let failure: any = null;
    try {
      // 游标必须由这一趟**带回来**：读Once 里自己推进而不返回的话，重连永远从老位置开始，
      // 症状正是这一片要消灭的那个东西——从头重放（P2 的 before 读数：重播 3 条）。
      const seen = await readOnce(jobId, lastEventId, onEvent, signal);
      if (seen) lastEventId = seen;
    } catch (error: any) {
      if (isAbort(error) || signal?.aborted) throw error;
      failure = error;
    }

    // 流正常结束也要问一次状态：服务端在作业到终态后会自己收尾，此时"结束"不是"断了"
    if (await isTerminal(jobId, signal).catch(() => false)) {
      return { reconnections: attempt, endedBy: "terminal" };
    }

    attempt += 1;
    const retriable = failure ? shouldRetry(failure, attempt, { maxAttempts }) : true;
    if (!retriable || (maxAttempts && attempt >= maxAttempts)) {
      if (failure) throw failure;
      return { reconnections: attempt - 1, endedBy: "attempts" };
    }
    const waitMs = reconnectDelay(failure, attempt, { maxAttempts });
    options.onReconnect?.({ attempt, waitMs, reason: failure ? failure.message || String(failure) : "进度流结束" });
    await sleep(waitMs, signal);
  }
}
