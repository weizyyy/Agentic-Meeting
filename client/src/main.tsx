import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { AuthGate } from "./components/AuthGate.tsx";
import "./index.css";
import "./styles.css";
import { messages } from "./i18n/index.ts";
import { initLanguage, subscribeLanguage } from "./i18n/language.ts";
import { initTheme } from "./theme.ts";

// 告诉 index.html 里的兜底脚本：主脚本运行起来了，不用提示浏览器太旧。
document.documentElement.dataset.started = "true";
// 登录页也按保存的外观和语言显示
initTheme();
initLanguage();
const showTitle = () => {
  document.title = messages().common.appName;
};
showTitle();
subscribeLanguage(showTitle);

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <AuthGate />
  </StrictMode>,
);
