/** 新建会话时的名字：单点，因为它同时管三件事——优先级、空白、以及能不能存进库。
 *
 *  优先级：**用户自己写的名字 > 问题前缀 > 一句人读得懂的默认**。
 *  三件事各有各的坏法：只敲空格会被后端原样入库，于是列表里出现一个"看不见名字"的会话；
 *  上限比库里的列宽松会"能输入但存不下"（`app/models.py` 的 `sessions.title` 是 `String(200)`，
 *  而 SQLite 不按 VARCHAR 长度报错——所以这条界只能界面上先守住，后端另有一条 422 兜着）；
 *  完全没有默认值的话，会话下拉里就是一串 epoch（那是这一页刚修掉过的形状）。
 */

/** 与库里那一列声明的宽度一致；改这里要同时改 `app/schemas.py` 的 `max_length`。 */
export const TITLE_LIMIT = 200;

/** 留空时问题前 20 字当名字——沿用工作台一直有的那个回退，它比"未命名会话"更有信息量。 */
export const QUESTION_TITLE_CHARS = 20;

export const UNTITLED_PREFIX = "未命名会话";

/** 只留下真正是名字的那部分：trim 后为空就不算名字。 */
export function customTitle(raw: string | null | undefined): string {
  return (raw ?? "").trim().slice(0, TITLE_LIMIT);
}

/** 建会话那一刻的时间戳。日期在这里读一次是正当的——它是"这一件事发生的时刻"，
 *  不是渲染期反复取"现在"（那种写法会让补发的旧帧整列显示成此刻）。 */
function stamp(at: Date): string {
  return at.toLocaleString("zh-CN", { hour12: false });
}

/** 发给后端的 title。空白输入不许直接发出去（后端会照原样存成一个看不见的名字）。 */
export function sessionTitle(
  custom: string | null | undefined,
  options: { question?: string; at?: Date } = {},
): string {
  const typed = customTitle(custom);
  if (typed) return typed;
  const derived = (options.question ?? "").trim().slice(0, QUESTION_TITLE_CHARS);
  if (derived) return derived;
  return `${UNTITLED_PREFIX}（${stamp(options.at ?? new Date())}）`;
}

/** 输入框下面的那一行提示：把"为什么我留空它叫这个"当场说清，别让用户去猜回退规则。 */
export function titleHint(custom: string | null | undefined, options: { question?: string } = {}): string {
  if (customTitle(custom)) return `会用你写的这个名字（上限 ${TITLE_LIMIT} 字）`;
  const derived = (options.question ?? "").trim();
  if (derived) return `留空则用问题前 ${QUESTION_TITLE_CHARS} 字：「${derived.slice(0, QUESTION_TITLE_CHARS)}」`;
  return `留空则叫「${UNTITLED_PREFIX}（创建时刻）」`;
}
