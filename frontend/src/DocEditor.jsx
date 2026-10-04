import { useEffect, useRef } from "react";
import Editor, { useMonaco } from "@monaco-editor/react";
import { registerMiniLang, toMarkers } from "./monacoSetup.js";

// 单个文档的 Monaco 编辑器。
// props:
//   text/initialText            文本（受控仅在非脏的服务端同步时使用 value）
//   diagnostics                 服务端诊断（已绑定 revision）
//   onChange(text)              本地编辑回调
//   editorRef(editor, monaco)   向外暴露编辑器实例（光标位置取词用）
export default function DocEditor({
  docId,
  text,
  diagnostics,
  onChange,
  onEditorReady,
}) {
  const monaco = useMonaco();
  const editorRef = useRef(null);
  const modelRef = useRef(null);

  useEffect(() => {
    if (monaco) registerMiniLang(monaco);
  }, [monaco]);

  // 诊断 → Monaco markers。坐标系都是 1-based UTF-16 行列，直接绑定。
  useEffect(() => {
    if (!monaco || !modelRef.current || !diagnostics) return;
    monaco.editor.setModelMarkers(
      modelRef.current,
      "minilang-server",
      toMarkers(monaco, diagnostics),
    );
  }, [monaco, diagnostics, docId]);

  const handleMount = (editor, m) => {
    editorRef.current = editor;
    modelRef.current = editor.getModel();
    onEditorReady?.(editor, m);
  };

  return (
    <Editor
      height="100%"
      theme="mini-dark"
      language="minilang"
      value={text}
      onMount={handleMount}
      onChange={(value) => onChange(value ?? "")}
      options={{
        fontSize: 14,
        minimap: { enabled: false },
        automaticLayout: true,
        renderWhitespace: "boundary",
      }}
    />
  );
}
