import fs from "node:fs";
import path from "node:path";

import ts from "typescript";
import { describe, expect, it } from "vitest";

/**
 * No Chinese in the code, only in the Chinese table.
 *
 * Every string literal, template-literal span and piece of JSX text under
 * src/ is read with the TypeScript parser; one that holds a CJK character
 * outside src/uiText.zh.ts fails. That is what keeps a sentence, or a 、 used
 * to join a list, from being written into a component where only one
 * language would ever see it. Comments are not strings and may say what
 * they like.
 */

const SRC = path.resolve(__dirname, "../../src");
const CJK = /[⺀-鿿豈-﫿＀-￯　-〿]/;
/** the Chinese table, and the registry where a language names itself */
const ALLOWED = new Set(["uiText.zh.ts", "languages.ts"]);

function files(dir: string, out: string[] = []): string[] {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, entry.name);
    if (entry.isDirectory()) files(p, out);
    else if (/\.(ts|tsx)$/.test(entry.name)) out.push(p);
  }
  return out;
}

function chineseStrings(file: string): string[] {
  const text = fs.readFileSync(file, "utf8");
  const kind = file.endsWith("x") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const sf = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, kind);
  const hits: string[] = [];
  const visit = (node: ts.Node) => {
    let s: string | null = null;
    if (
      ts.isStringLiteral(node) ||
      ts.isNoSubstitutionTemplateLiteral(node) ||
      ts.isTemplateHead(node) ||
      ts.isTemplateMiddle(node) ||
      ts.isTemplateTail(node) ||
      ts.isJsxText(node)
    ) {
      s = node.text;
    }
    if (s && CJK.test(s)) {
      const { line } = sf.getLineAndCharacterOfPosition(node.getStart());
      hits.push(`${path.relative(SRC, file)}:${line + 1}: ${JSON.stringify(s.trim().slice(0, 60))}`);
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  return hits;
}

describe("代码里不写中文", () => {
  it("src/ 下除了中文表，没有一个带汉字的字符串", () => {
    const hits = files(SRC)
      .filter((f) => !ALLOWED.has(path.basename(f)))
      .flatMap(chineseStrings);
    expect(hits).toEqual([]);
  });

  it("扫描本身认得出汉字", () => {
    const zh = path.join(SRC, "uiText.zh.ts");
    expect(chineseStrings(zh).length).toBeGreaterThan(200);
  });
});
