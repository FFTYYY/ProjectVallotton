import fs from "node:fs";
import path from "node:path";

import ts from "typescript";
import { describe, expect, it } from "vitest";

/**
 * A word is read where it is shown, never at import.
 *
 * `t.word` outside any function runs once, when the module loads, and keeps
 * that language for ever: that is how the quality and auto-library labels
 * stayed Chinese on an English screen. Every `t.…` under src/ has to sit
 * inside a function, a method or an arrow, so it runs each time the thing is
 * rendered and reads the language chosen then.
 */

const SRC = path.resolve(__dirname, "../../src");

function files(dir: string, out: string[] = []): string[] {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, entry.name);
    if (entry.isDirectory()) files(p, out);
    else if (/\.(ts|tsx)$/.test(entry.name)) out.push(p);
  }
  return out;
}

/** `t.x` reads that no function encloses, as file:line */
function importTimeReads(file: string): string[] {
  const text = fs.readFileSync(file, "utf8");
  if (!/import \{[^}]*\bt\b[^}]*\} from "[^"]*uiText"/.test(text)) return [];
  const kind = file.endsWith("x") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const sf = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, kind);
  const hits: string[] = [];
  const visit = (node: ts.Node) => {
    if (ts.isPropertyAccessExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === "t") {
      let p: ts.Node | undefined = node.parent;
      let enclosed = false;
      while (p) {
        if (ts.isFunctionLike(p)) {
          enclosed = true;
          break;
        }
        p = p.parent;
      }
      if (!enclosed) {
        const { line } = sf.getLineAndCharacterOfPosition(node.getStart());
        hits.push(`${path.relative(SRC, file)}:${line + 1}: ${node.getText()}`);
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  return hits;
}

describe("文案只在用到的时候读", () => {
  it("src/ 下没有一处在模块顶层读 t", () => {
    expect(files(SRC).flatMap(importTimeReads)).toEqual([]);
  });

  it("扫描认得出模块顶层的读取", () => {
    const tmp = path.join(SRC, "..", "tests", "fixtures", "_moduleLevel.tsx");
    fs.writeFileSync(tmp, 'import { t } from "../../src/uiText";\nexport const X = t.logout;\nexport function ok() { return t.logout; }\n');
    try {
      expect(importTimeReads(tmp)).toHaveLength(1);
    } finally {
      fs.unlinkSync(tmp);
    }
  });
});
