/**
 * 状态→语气/文案的单点。四个 view 原先各写一份三元表达式，症状是同一个 `running`
 * 在历史页是红的、在工作台是蓝的，而"进行中"被画成 danger 等于告诉值班的人"出事了"。
 *
 * 只覆盖语气与中文说法。**不在这儿抄终态名单**——哪些状态算终态由后端
 * `queueing.TERMINAL` 算并随作业行给出（`data.terminal`），抄一份到前端迟早分叉，
 * 表现是"还在跑"被当成"完了"（`api/sse.ts` 里同一立场）。
 *
 * 英文枚举原值留在 `title` 里：分诊的人要拿界面上的状态去对 API 与产物，
 * 只给中文就断了这条路。
 */

export type Tone = "success" | "warning" | "danger" | "info" | "primary";

const TONES: Record<string, Tone> = {
  // 作业与运行的终态（与 jobs.status 同源）
  success: "success",
  partial: "warning",
  degraded: "warning",
  failed: "danger",
  error: "danger",
  cancelled: "info",
  // 非终态：这是"还没结果"，不是"坏消息"
  pending: "info",
  queued: "info",
  running: "primary",
  // 场景包/数据集解析态（bundles.status）
  ready: "success",
  parsing: "primary",
  // 引擎任务事件用的大写那一套（orchestrator 只发这两个）
  SUCCEEDED: "success",
  FAILED: "danger",
  RUNNING: "primary",
  PENDING: "info",
};

const LABELS: Record<string, string> = {
  success: "成功",
  partial: "部分完成",
  degraded: "降级完成",
  failed: "失败",
  error: "出错",
  cancelled: "已取消",
  pending: "排队中",
  queued: "排队中",
  running: "进行中",
  ready: "就绪",
  parsing: "解析中",
  table: "成表",
  document: "证据",
  skipped: "被拒",
  SUCCEEDED: "成功",
  FAILED: "失败",
  RUNNING: "进行中",
  PENDING: "排队中",
};

/** 认不出的状态不许渲染成绿色：一律 info，并且原值照样显示，让人看得见"这里有个新值"。 */
export function tone(status: unknown): Tone {
  return TONES[String(status ?? "")] ?? "info";
}

export function label(status: unknown): string {
  const raw = String(status ?? "");
  return LABELS[raw] ?? raw;
}

/** Tag 的 tooltip：中文说法 + 后端原值。 */
export function tooltip(status: unknown): string {
  const raw = String(status ?? "");
  const zh = LABELS[raw];
  return zh ? `${zh}（后端原值 ${raw}）` : raw;
}
