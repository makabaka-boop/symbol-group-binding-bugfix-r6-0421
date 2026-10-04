// 在 Monaco 加载前注册小语言：def/use 关键字、ASCII 标识符、# 行尾注释。
// 注释词法上允许任意 Unicode（Monaco 本身按 UTF-16 处理列号，与后端一致）。
export function registerMiniLang(monaco) {
  if (monaco.languages.getLanguages().some((l) => l.id === "minilang")) return;

  monaco.languages.register({ id: "minilang" });

  monaco.languages.setMonarchTokensProvider("minilang", {
    defaultToken: "",
    tokenizer: {
      root: [
        [/#.*$/, "comment"],
        [/\b(def|use)\b/, "keyword"],
        [/[A-Za-z_][A-Za-z0-9_]*/, "identifier"],
        [/[ \t]+/, ""],
      ],
    },
  });

  monaco.languages.setLanguageConfiguration("minilang", {
    comments: { lineComment: "#" },
  });

  monaco.editor.defineTheme("mini-dark", {
    base: "vs-dark",
    inherit: true,
    rules: [
      { token: "comment", foreground: "6a9955", fontStyle: "italic" },
      { token: "keyword", foreground: "569cd6" },
      { token: "identifier", foreground: "9cdcfe" },
    ],
    colors: {},
  });
}

// 后端诊断（1-based UTF-16 行列）→ Monaco IMarkerData（同为 1-based UTF-16）
export function toMarkers(monaco, diagnostics) {
  const sev = monaco.MarkerSeverity;
  const map = { error: sev.Error, warning: sev.Warning, info: sev.Info };
  return diagnostics.map((d) => ({
    startLineNumber: d.range.start.line,
    startColumn: d.range.start.column,
    endLineNumber: d.range.end.line,
    endColumn: d.range.end.column,
    severity: map[d.severity] ?? sev.Hint,
    message: d.message,
    code: d.code,
  }));
}
