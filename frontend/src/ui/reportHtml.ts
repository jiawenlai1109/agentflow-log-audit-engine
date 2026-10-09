/**
 * 报告正文的标题降级。单独成一个模块是为了它能被直接测到——
 * "少降一级"这种改动在页面上不报错、不变红，只会让文档大纲悄悄坏掉。
 */

/** `#` 级 `<h1>` → `<h2>`，一路降到 `<h6>` 封底。
 *  页面本身已经有一个 `<h1>`（view 的标题），一份以 `<h1>` 开头的产物嵌在里面会把大纲
 *  劈成两个平级根节点；读屏按标题跳转时列出的是"两个 h1"，而不是"页面 > 报告章节"。
 *  只动标签名，不动一个字的内容：这是展示层的变换，产物文件与校验（report_lint / Critic）
 *  读的始终是原文。markdown-it 开着 `html: false`，所以这里的 `<hN>` 只可能来自标题语法。 */
export function demoteHeadings(fragment: string): string {
  return fragment.replace(/<(\/?)h([1-5])(?=[\s>])/g, (_all, slash: string, level: string) => `<${slash}h${Number(level) + 1}`);
}
