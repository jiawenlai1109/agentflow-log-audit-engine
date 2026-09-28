/** 带 Authorization 头的 SSE 读取。
 *  EventSource 设不了请求头，把 token 拼进 query 会进访问日志/浏览器历史，
 *  所以这里用 fetch + ReadableStream 自己解帧。
 */

export type JobEvent = Record<string, unknown> & { type?: string };

export async function streamJobEvents(
  jobId: string,
  onEvent: (event: JobEvent) => void,
  signal?: AbortSignal
): Promise<void> {
  const token = localStorage.getItem("token") || "";
  const response = await fetch(`/api/jobs/${jobId}/events`, {
    headers: { Authorization: `Bearer ${token}` },
    signal,
  });
  if (!response.ok || !response.body) {
    throw new Error(`进度流连接失败（HTTP ${response.status}）`);
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
      const payload = frame
        .split("\n")
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trim())
        .join("\n");
      if (!payload) continue;
      onEvent(JSON.parse(payload) as JobEvent);
    }
  }
}
