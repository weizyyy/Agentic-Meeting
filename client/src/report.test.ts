import assert from "node:assert/strict";
import { test } from "node:test";

import {
  generateLabel,
  reportBlocker,
  reportDownloadUrl,
  reportHeadline,
  type ReportInfo,
} from "./report.ts";

const report = (extra: Partial<ReportInfo> = {}): ReportInfo => ({
  id: 3,
  status: "done",
  created_at: 1000,
  provider: "realtime_llm",
  text_md: "# 报告",
  error: null,
  ...extra,
});

test("什么时候可以生成报告", () => {
  const ended = { state: "ended", utterance_count: 12 } as const;
  assert.equal(reportBlocker(ended, null), null);
  assert.equal(reportBlocker({ ...ended, state: "interrupted" }, null), null);
  assert.equal(reportBlocker(ended, report()), null); // 有了一份也可以重新生成
  assert.equal(reportBlocker(ended, report({ status: "failed" })), null);
  assert.match(reportBlocker({ ...ended, state: "live" }, null) ?? "", /正在进行/);
  assert.match(reportBlocker(ended, report({ status: "running" })) ?? "", /正在生成/);
  assert.match(reportBlocker({ ...ended, utterance_count: 0 }, null) ?? "", /没有发言/);
  assert.match(reportBlocker(null, null) ?? "", /还没有会议/);
});

test("按钮上的字和顶上的说明", () => {
  assert.equal(generateLabel(null), "生成报告");
  assert.equal(generateLabel(report()), "重新生成");
  assert.equal(reportHeadline(null), "这场会议还没有报告。");
  assert.equal(reportHeadline(report()), "由实时模型生成。");
  assert.match(
    reportHeadline(report({ status: "running", provider: "agent_llm" })),
    /后台的远端模型/,
  );
  assert.equal(
    reportHeadline(report({ status: "failed", error: "生成超时" })),
    "报告没有生成出来：生成超时",
  );
  assert.equal(reportHeadline(report({ status: "failed" })), "报告没有生成出来：原因不明");
  assert.equal(reportHeadline(report({ provider: "别的" })), "由别的生成。");
});

test("下载地址：会议编号要编码", () => {
  assert.equal(reportDownloadUrl("a b/c"), "/api/sessions/a%20b%2Fc/report.md");
});
