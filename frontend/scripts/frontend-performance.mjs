import { chromium } from "@playwright/test";
import { mkdir, writeFile } from "node:fs/promises";

const baseUrl = process.argv[2] || "http://localhost:5173";
const output = process.argv[3] || "../verification/results/frontend-performance.json";
const browser = await chromium.launch();
const page = await browser.newPage();

await page.addInitScript(() => {
  window.__waypointVitals = { cls: 0, lcp: null, longTasks: [] };
  new PerformanceObserver((list) => {
    for (const entry of list.getEntries()) window.__waypointVitals.lcp = entry.startTime;
  }).observe({ type: "largest-contentful-paint", buffered: true });
  new PerformanceObserver((list) => {
    for (const entry of list.getEntries()) {
      if (!entry.hadRecentInput) window.__waypointVitals.cls += entry.value;
    }
  }).observe({ type: "layout-shift", buffered: true });
  new PerformanceObserver((list) => {
    for (const entry of list.getEntries()) {
      window.__waypointVitals.longTasks.push({ start: entry.startTime, duration: entry.duration });
    }
  }).observe({ type: "longtask", buffered: true });
});

const started = performance.now();
const response = await page.goto(baseUrl, { waitUntil: "networkidle", timeout: 60_000 });
await page.waitForTimeout(1500);
const metrics = await page.evaluate(() => {
  const navigation = performance.getEntriesByType("navigation")[0];
  const resources = performance.getEntriesByType("resource");
  const vitals = window.__waypointVitals;
  return {
    navigation: {
      ttfb_ms: navigation.responseStart,
      dom_content_loaded_ms: navigation.domContentLoadedEventEnd,
      load_event_ms: navigation.loadEventEnd,
      transfer_bytes: navigation.transferSize,
    },
    web_vitals: {
      largest_contentful_paint_ms: vitals.lcp,
      cumulative_layout_shift: vitals.cls,
      interaction_to_next_paint_ms: null,
      interaction_to_next_paint_status: "not_measured_no_scripted_interaction",
    },
    resources: {
      requests: resources.length,
      transfer_bytes: resources.reduce((sum, item) => sum + (item.transferSize || 0), 0),
      duration_ms: resources.reduce((sum, item) => sum + item.duration, 0),
    },
    long_tasks: {
      count: vitals.longTasks.length,
      total_duration_ms: vitals.longTasks.reduce((sum, item) => sum + item.duration, 0),
      maximum_duration_ms: Math.max(0, ...vitals.longTasks.map((item) => item.duration)),
    },
    javascript_heap: performance.memory ? {
      used_mib: performance.memory.usedJSHeapSize / 1024 / 1024,
      total_mib: performance.memory.totalJSHeapSize / 1024 / 1024,
      limit_mib: performance.memory.jsHeapSizeLimit / 1024 / 1024,
    } : null,
  };
});

const result = {
  base_url: baseUrl,
  status: response?.status() ?? null,
  successful: Boolean(response?.ok()),
  wall_time_ms: performance.now() - started,
  ...metrics,
  thresholds: {
    largest_contentful_paint_ms: 2500,
    cumulative_layout_shift: 0.1,
    source: "Core Web Vitals good-experience boundaries; INP requires a representative interaction and is not fabricated.",
  },
};
result.thresholds_passed = Boolean(
  result.successful &&
  result.web_vitals.largest_contentful_paint_ms !== null &&
  result.web_vitals.largest_contentful_paint_ms <= result.thresholds.largest_contentful_paint_ms &&
  result.web_vitals.cumulative_layout_shift <= result.thresholds.cumulative_layout_shift
);

await browser.close();
await mkdir(output.replace(/[\\/][^\\/]+$/, ""), { recursive: true });
await writeFile(output, `${JSON.stringify(result, null, 2)}\n`, "utf8");
console.log(JSON.stringify(result, null, 2));
