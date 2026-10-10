/** 数据源那一格的单点：界面在"单文件数据集"与"多源包"之间选，判据只写这一份。
 *
 *  后端要的是**二者只给一个**（`app/schemas.py` 的 `_exactly_one_source`：`bool(dataset_id) ==
 *  bool(bundle_id)` 一律拒）。所以换了一种源，另一种的残留值必须以 undefined 出去——
 *  留一个 0 或空串在载荷里，就是"用户以为自己在跑三张表，实际跑的是那张单文件"的下一版。
 */

export type SourceKind = "dataset" | "bundle";

export const SOURCE_KINDS: { value: SourceKind; label: string }[] = [
  { value: "dataset", label: "单文件数据集" },
  { value: "bundle", label: "多源包" },
];

export interface BundleRow {
  bundle_id: string;
  name?: string;
  status?: string;
  file_count?: number;
  table_count?: number;
  document_count?: number;
}

/** 提交载荷里的数据源那一格。 */
export function sourcePayload(
  kind: SourceKind,
  datasetId: number | undefined,
  bundleId: string | undefined,
): { dataset_id?: number; bundle_id?: string } {
  if (kind === "bundle") return { bundle_id: bundleId || undefined };
  return { dataset_id: datasetId ?? undefined };
}

/** 选项文案：包名 + 它自己声明的表数/文件数。没给数就说"未取得"，不印成 0 张表。 */
export function bundleLabel(bundle: BundleRow): string {
  const tables = typeof bundle.table_count === "number" ? `${bundle.table_count} 表` : "表数未取得";
  const files = typeof bundle.file_count === "number" ? `${bundle.file_count} 文件` : "文件数未取得";
  return `${bundle.name || bundle.bundle_id}（${tables} / ${files}）`;
}

/** 只有解析完的包能跑（后端 `load_bundle_for_analysis` 对非 ready 一律 409）。
 *  不可用的包**留在列表里但选不动**并说明原因——让它从界面上蒸发，读起来就是"没有这个包"。 */
export function bundleUsable(bundle: BundleRow): boolean {
  return bundle.status === "ready";
}

export function bundleBlockedReason(bundle: BundleRow): string {
  if (bundleUsable(bundle)) return "";
  const statusText: Record<string, string> = { parsing: "还在解析", failed: "解析失败", ready: "" };
  return `不可选：${statusText[bundle.status || ""] || bundle.status || "状态未取得"}`;
}

/** 会话这一格什么时候真的有意义：单文件数据集、且这次不带包。
 *  两条互斥都写在后端的校验器里（`schemas.py:_exactly_one_source`）：
 *  会话续轮读的是 `sessions.dataset_path`——带包会被拒，而带多源包过去会让第二轮
 *  **悄悄换一个数据源**，那比直接拒危险。 */
export function sessionAllowed(kind: SourceKind, hasPack: boolean): boolean {
  return kind === "dataset" && !hasPack;
}

/** 选了不能挂会话的组合时，那一句解释。 */
export function sessionBlockedReason(kind: SourceKind, hasPack: boolean): string {
  if (sessionAllowed(kind, hasPack)) return "";
  if (hasPack) return "带包时这一轮不挂会话：结果照样进本页与历史页，但不进会话树";
  return "多源包不挂会话：会话只绑单文件，续轮会换一个数据源";
}

/** 发出去的那一格：这一轮不允许挂会话时整个键不发（undefined），而不是发空串。
 *  判据只有 `sessionAllowed` 一处，调用点不许自己再算一遍——两条互斥规则（带包、带多源包）
 *  分别写在后端 `app/schemas.py` 里，前端也必须是同一个地方说完。 */
export function sessionPayload(allowed: boolean, session: string | undefined): string | undefined {
  return allowed ? session : undefined;
}
