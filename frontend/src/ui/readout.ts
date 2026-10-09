/**
 * "没量到"的单一出口。
 *
 * 起因是三处同族界面读数：`WorkbenchView` 的队列深度里 `queued ?? "?"` 与
 * `stale_pending ?? 0` 混用（同一个短语里两种"不知道"），`ReportsView` 把
 * `media_expires_in` 缺值经 `Math.max(1, Math.round(0/60))` 印成"约 1 分钟有效"，
 * 四个汇总卡不显示分母。三处都是**把没测到写成一个小数字**。
 *
 * 引擎那边同一立场已经立过：`?` 与 `0` 是两件事，0 是一个结论，`?` 是没有结论。
 */

export const UNKNOWN = "未取得";

/** 数值读数：null / undefined / 空串 / 非有限数一律走"未取得"，不许塌成 0。 */
export function num(value: unknown, unit = ""): string {
  if (value === null || value === undefined || value === "") return UNKNOWN;
  const n = Number(value);
  if (!Number.isFinite(n)) return UNKNOWN;
  return `${n}${unit}`;
}

/** 时长（秒）读数：0 与"不到 1 秒"在界面上必须是同一句话。
 *  后端两列时间戳只有秒级精度（缺陷 #55），`queue_wait_seconds=0.0` 读起来是"没排队"，
 *  实际含义是"<1s"——而 `avg_duration` 那边还有"缺耗时按 0 计入均值"的口径（新挂账 #56），
 *  所以这里宁可说一句更弱但为真的话。 */
export function seconds(value: unknown): string {
  if (value === null || value === undefined || value === "") return UNKNOWN;
  const n = Number(value);
  if (!Number.isFinite(n)) return UNKNOWN;
  if (n >= 0 && n < 1) return "<1s";
  return `${n}s`;
}
