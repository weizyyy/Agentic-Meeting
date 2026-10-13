// 文案目录的写法：每个界面区域一个文件，中文和英文并排写在一次 defineMessages 调用里。
// 英文的类型由中文推出来：少一个键、多一个键、带参数的文案参数对不上，`npm run build` 的类型检查就过不去，
// CI 的 Web client 任务靠它保证两种语言的词条一致。

export type Language = "zh-CN" | "en";

export const LANGUAGES: readonly Language[] = ["zh-CN", "en"];

/** 把中文目录的字面类型放宽：字符串就是 string，带参数的文案保持参数类型，嵌套的分组逐层放宽。 */
export type Widen<T> = T extends string
  ? string
  : T extends (...args: infer A) => string
    ? (...args: A) => string
    : { readonly [K in keyof T]: Widen<T[K]> };

/** 一个区域的文案：先写中文，再写英文。带参数的文案写成函数，参数要标类型。 */
export function defineMessages<T>(zhCN: T, en: Widen<T>): Record<Language, Widen<T>> {
  return { "zh-CN": zhCN as Widen<T>, en };
}
