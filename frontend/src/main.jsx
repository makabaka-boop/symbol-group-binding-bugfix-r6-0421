import React from "react";
import { createRoot } from "react-dom/client";
import * as monaco from "monaco-editor";
import { loader } from "@monaco-editor/react";
import App from "./App.jsx";

// 用本地打包的 monaco，而不是默认的 jsDelivr CDN
loader.config({ monaco });

createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
