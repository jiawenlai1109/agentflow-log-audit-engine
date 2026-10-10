/** 外部（MCP）证据在界面上的那一格：把"几路、可不可信、有没有取失败"说清楚。
 *
 *  存在的理由是两种"没有"长得太像：
 *  · `null`/缺字段 = **这份产物里没读到这个数**（老产物、读盘失败）；
 *  · `[]` = **这次分析确实没引用任何外部数据**。
 *  把前者印成后者，等于对值班说"它没去查威胁情报"，而真相是我们不知道——
 *  与把"没量到"印成 0 是同一条账。
 *
 *  另一条：接口给的字段是白名单投影（`app/routers/reports.py` 的 `_EVIDENCE_FIELDS`），
 *  所以这里**拿不到 path、也拿不到失败原因**（那是 `str(error)[:300]`，可能夹路径）。
 *  取失败就说"取失败"，不编原因。
 */

export interface EvidenceRow {
  server?: string;
  tool?: string;
  tool_name?: string;
  label?: string;
  untrusted?: boolean;
  status?: string;
  rows?: number;
  columns?: string[];
  sha256?: string;
}

export type EvidenceList = EvidenceRow[] | null | undefined;

/** 列表那一格用的短话：只给计数，不给整条。 */
export function countText(count: number | null | undefined): string {
  if (count === null || count === undefined) return "未取得";
  return `${count} 路`;
}

export function evidenceSummary(evidence: EvidenceList): string {
  if (evidence === null || evidence === undefined) return "外部证据未取得（这份产物里没读到这个字段）";
  if (evidence.length === 0) return "这次分析没有引用外部数据";
  const untrusted = evidence.filter((row: EvidenceRow) => row.untrusted).length;
  const failed = evidence.filter((row: EvidenceRow) => row.status && row.status !== "ok").length;
  const trustTail = untrusted > 0 ? `，其中 ${untrusted} 路标着不可信（只作参考，不进数字来源）` : "";
  const failTail = failed > 0 ? `；${failed} 路取失败（原因只在服务端留痕，接口不给）` : "";
  return `引用 ${evidence.length} 路外部数据${trustTail}${failTail}`;
}

/** 一行证据。行数没给就说"未取得"，不印成 0 行——那读起来像"查到了但没结果"。 */
export function evidenceLine(row: EvidenceRow): string {
  const source = row.server ? `${row.server}${row.tool ? ` · ${row.tool}` : ""}` : row.tool_name || "来源未标注";
  const rows = typeof row.rows === "number" ? `${row.rows} 行` : "行数未取得";
  const columns = typeof row.columns?.length === "number" ? `${row.columns.length} 列` : "列数未取得";
  const digest = row.sha256 ? `sha256 ${row.sha256}` : "指纹未取得";
  const state = !row.status || row.status === "ok" ? "取到" : `取失败（${row.status}）`;
  const trust = row.untrusted ? "不可信" : "未标注不可信";
  return `${row.label || source}｜${source}｜${state}：${rows} / ${columns}｜${digest}｜${trust}`;
}

/** "未标注不可信"要额外提醒一次：这条链的默认姿态是不可信来源不参与判定，
 *  少了这个标记不是"可信"，是"没标"。 */
export function evidenceNeedsAttention(evidence: EvidenceList): boolean {
  if (!Array.isArray(evidence)) return false;
  return evidence.some((row: EvidenceRow) => row.untrusted !== true);
}
