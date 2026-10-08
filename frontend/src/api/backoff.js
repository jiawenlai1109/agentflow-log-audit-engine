/** 退避与"该不该再试一次"的策略，只这一份（P5-2）。
 *
 * 为什么这个文件是 `.js` 而不是 `.ts`：策略要能被 node 直接跑起来断言。
 * 前端这一侧的 `npm run build` 只有 `vite build`（没有类型检查、没有测试命令），
 * 而退避的错——无限重试、不退避就重试、忽略 `Retry-After`、把 401 也拿去重试——
 * 只有断言抓得住，读代码读不出来。所以策略单独成文件，`.appdata/check_backoff.mjs` 直接 import 它跑检查。
 */

export const DEFAULTS = {
  baseMs: 500, // 第一次退避多久：比"立刻重试"长，比一次分析的耗时无关紧要
  capMs: 15000, // 上限：再长用户就该看到"断了"的提示，而不是永远悄悄重试
  maxAttempts: 8, // 总次数：8 次大约覆盖 ~1 分钟的受理层重启
  jitterRatio: 0.3, // 抖动比例：同一时刻醒来的客户端会把重启中的服务再推一次
};

/** 第 attempt 次失败之后该等多久（attempt 从 1 开始）。指数增长 + 封顶 + 抖动。 */
export function nextDelay(attempt, options = {}) {
  const { baseMs, capMs, jitterRatio } = { ...DEFAULTS, ...options };
  const growth = baseMs * 2 ** Math.max(0, Number(attempt) - 1);
  const capped = Math.min(capMs, growth);
  const jitter = capped * jitterRatio * Math.random();
  return Math.round(capped + jitter);
}

/** 服务端给的 `Retry-After` 优先于我们自己算的：限流与配额那两格知道还要等多久，我们不知道。 */
export function retryAfterMs(header, capMs = 60000) {
  if (header === undefined || header === null || header === "") return 0;
  const seconds = Number(String(header).trim());
  if (!Number.isFinite(seconds) || seconds < 0) return 0; // 非法值不当成"等 0 秒"，也不当成"永远等"
  return Math.min(capMs, Math.round(seconds * 1000));
}

/**
 * 下一次连接该等多久：服务端的 `Retry-After` 优先，其次才是我们自己算的指数退避。
 * 单独成一个函数是为了"两条重试路径共用一份判断"——HTTP 重试（withRetry）与进度流重连
 * （sse.ts）各自抄一遍的话，其中一条忘了读 Retry-After 没人会发现（位点 PB3 第一次就是这么绿的）。
 */
export function reconnectDelay(failure, attempt, options = {}) {
  return retryAfterMs(failure?.response?.headers?.["retry-after"]) || nextDelay(attempt, options);
}

export function isAbort(error) {
  if (!error) return false;
  return (
    error.name === "CanceledError" ||
    error.name === "AbortError" ||
    error.code === "ERR_CANCELED" ||
    !!error.__aborted
  );
}

/** 只重试"连不上 / 暂时性"的失败。401/403/404/422 重试不会变好，只会把上游与队列再打一遍。 */
export function shouldRetry(error, attempt, options = {}) {
  const { maxAttempts } = { ...DEFAULTS, ...options };
  if (isAbort(error)) return false; // 用户切走视图/取消：立刻放手，不伪装成"还在重试"
  if (attempt >= maxAttempts) return false;
  const status = error?.response?.status;
  if (!status) return true; // 没有响应 = 连接被杀/断网，正是这一片要能扛住的那一类
  if (status === 429) return true; // 限流与配额：等 Retry-After 再来
  return status === 0 || status === 502 || status === 503 || status === 504;
}

/** 可中断的等待：视图切换或用户取消必须立刻打断，否则"停止"之后还会多等一轮退避。 */
export function sleep(ms, signal) {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      const error = new Error("已取消");
      error.name = "AbortError";
      error.__aborted = true;
      reject(error);
      return;
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener?.("abort", onAbort);
      resolve();
    }, Math.max(0, Math.round(ms)));
    function onAbort() {
      clearTimeout(timer);
      const error = new Error("已取消");
      error.name = "AbortError";
      error.__aborted = true;
      reject(error);
    }
    signal?.addEventListener?.("abort", onAbort, { once: true });
  });
}

/**
 * 带退避地重试一个异步任务。`task(attempt)` 每次都要能安全重入——
 * 对"创建作业"这类请求，这意味着**重试必须带同一个幂等键**（调用方负责，见 api/index.ts），
 * 否则这个 helper 本身就是"把一次提问变成两个作业"的放大器。
 */
export async function withRetry(task, options = {}) {
  let attempt = 0;
  for (;;) {
    try {
      return await task(attempt);
    } catch (error) {
      if (isAbort(error)) throw error;
      attempt += 1;
      if (!shouldRetry(error, attempt, options)) throw error;
      const waitMs = reconnectDelay(error, attempt, options);
      await sleep(waitMs, options.signal);
      options.onRetry?.({ attempt, waitMs, error });
    }
  }
}
