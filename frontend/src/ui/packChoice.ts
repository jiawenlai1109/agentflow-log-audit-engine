/** 场景包（pack）在界面上的那一格：选择 → 提交载荷 的单点。
 *
 * 它存在的理由是一条后端的判据：`pack: ""` **不算**"没选包"，`/api/analyze` 会把它判成 422
 * （`app/routers/jobs.py:93` 明写"写空串是想跑包的，静默降级成普通分析就是未知键被忽略那一类"）。
 * 所以"不挂包"必须是一个显式选项，出去的时候**整个键都不发**，而不是空串、也不是 null。
 */

export const NO_PACK = "__no_pack__";

export interface PackInfo {
  name: string;
  version?: string;
  description?: string;
  subject_label?: string;
  rule_count?: number;
  rule_ids?: string[];
  required_columns?: string[];
}

/** 界面选中的值 → 请求里的 pack 字段。没选就返回 undefined（axios 会把 undefined 的键丢掉）。 */
export function packPayload(selected: string | undefined | null): string | undefined {
  if (!selected || selected === NO_PACK) return undefined;
  return selected;
}

/** 选项文案：包名 + 它自己声明的规则条数。条数没给就写"未取得"，不印成 0 条。 */
export function packLabel(pack: PackInfo): string {
  const rules = typeof pack.rule_count === "number" ? `${pack.rule_count} 条规则` : "规则条数未取得";
  return `${pack.name}（${rules}）`;
}

/** 选中之后念给用户的一组事实。
 *  这里**故意不做"你的数据缺哪几列"的判断**：后端 preflight 比的是归一化之后的列
 *  （`jobs.py:139` 那条注释记着它曾用原始表头误拒过 `login_audit` 配 `login_auth.csv`），
 *  界面手里只有原始表头，照着算子集就是在一个拿不到正确输入的位置上下结论。
 */
export function packFacts(pack: PackInfo | undefined): string {
  if (!pack) return "不挂包：由规划器自己决定怎么看这份数据";
  const columns = pack.required_columns?.length ? pack.required_columns.join("、") : "未声明";
  const subject = pack.subject_label ? `主体：${pack.subject_label}｜` : "";
  return `${subject}必需列（判定以后端 preflight 为准）：${columns}`;
}

/** 装载失败的目录：后端不静默隐藏（`packs.py:37` 那句理由），界面这一侧也不许把它蒸发掉。 */
export function brokenPackNote(broken: unknown): string {
  if (!Array.isArray(broken) || broken.length === 0) return "";
  const detail = broken
    .map((item: any) => `${item?.dir ?? item?.name ?? "未知目录"}：${item?.reason ?? "原因未给出"}`)
    .join("；");
  return `${broken.length} 个场景包目录装载失败，因此不在可选项里——${detail}`;
}
