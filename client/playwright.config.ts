import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  testMatch: "**/*.spec.ts",
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: [["list"], ["html", { open: "never" }]],
  use: { trace: "retain-on-failure", screenshot: "only-on-failure" },
  projects: [
    { name: "chromium", use: { browserName: "chromium" } },
    {
      name: "firefox",
      use: {
        browserName: "firefox",
        launchOptions: {
          // Firefox 原生假设备保留采集状态和 ICE 权限语义。
          firefoxUserPrefs: {
            "media.navigator.streams.fake": true,
            "media.navigator.permission.disabled": true,
            "media.peerconnection.ice.obfuscate_host_addresses": false,
          },
        },
      },
    },
    { name: "webkit", use: { browserName: "webkit" } },
  ],
});
