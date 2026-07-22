import fs from "node:fs";
import { execFileSync } from "node:child_process";
import { openBrowser } from "./browser.js";
import {
  chooseOptionIfNeeded,
  chooseOptionTextIfNeeded,
  findButton,
  findControl,
  findYearRangeControls,
  getOptions,
  waitForOptionsRefresh
} from "./dom.js";
import { appendJsonLine, appendTsv, ensureTsv, parseCopiedTsv, readJson, writeJson } from "./io.js";

let restartCount = 0;
const maxRestarts = 5;

while (true) {
  try {
    await runPass();
    break;
  } catch (error) {
    if (isBrowserClosedError(error) && restartCount < maxRestarts) {
      restartCount += 1;
      console.log(`浏览器被关闭，自动重启继续，第 ${restartCount}/${maxRestarts} 次。`);
      continue;
    }
    throw error;
  }
}

async function runPass() {
  const { config, context, page } = await openBrowser();

  try {
    const checkpoint = readJson(config.checkpointFile, { completed: [], failed: [], manufacturers: [], modelsByManufacturer: {} });
    const completed = new Set(checkpoint.completed ?? []);
    const failed = new Set(checkpoint.failed ?? []);
    const modelsByManufacturer = checkpoint.modelsByManufacturer ?? {};

    if (fs.existsSync(config.requestLogFile)) fs.rmSync(config.requestLogFile);
    ensureTsv(config.tsvFile, ["year", "make", "model"]);

    page.on("response", async (response) => {
      const url = response.url();
      if (!/4afitment|vehicle|year|make|model|manufacturer|vc/i.test(url)) return;

      const contentType = response.headers()["content-type"] || "";
      if (!contentType.includes("json")) return;

      try {
        appendJsonLine(config.requestLogFile, {
          status: response.status(),
          url,
          body: await response.json()
        });
      } catch {
        // Some JSON-looking responses are not readable after navigation; ignore them.
      }
    });

    await context.grantPermissions(["clipboard-read", "clipboard-write"], { origin: new URL(config.startUrl).origin }).catch(() => {});

    await page.goto(config.startUrl, { waitUntil: "domcontentloaded" });
    await page.waitForLoadState("networkidle").catch(() => {});

    const loginVisible = await page.locator("input[name='username'], input[name='password']").first().isVisible().catch(() => false);
    if (loginVisible) {
      throw new Error("当前还是登录页。请先运行 .\\run.ps1 src\\login.js，手动登录后再运行抓取。");
    }

    const yearSelectors = await findYearRangeControls(page, config.selectors, config.labels);
    const manufacturerSelector = await findControl(page, config.selectors.manufacturer, config.labels.manufacturer);
    const modelSelector = await findControl(page, config.selectors.model, config.labels.model);
    const searchButton = await findButton(page, config.selectors.searchButton, config.labels.searchButton);

    console.log("识别到控件：");
    console.log(`yearFrom: ${yearSelectors.from}`);
    console.log(`yearTo: ${yearSelectors.to}`);
    console.log(`manufacturer: ${manufacturerSelector}`);
    console.log(`model: ${modelSelector}`);
    console.log(`yearRange: ${config.yearRange.from} - ${config.yearRange.to}`);

    const changedYearFrom = await chooseOptionTextIfNeeded(page, yearSelectors.from, config.yearRange.from);
    if (changedYearFrom) await page.waitForTimeout(config.timeouts.settleMs);
    const changedYearTo = await chooseOptionTextIfNeeded(page, yearSelectors.to, config.yearRange.to);
    if (changedYearTo) await page.waitForTimeout(config.timeouts.settleMs);

    const manufacturers = await getOptions(page, manufacturerSelector);
    checkpoint.manufacturers = manufacturers;
    saveCheckpoint(config, checkpoint, completed, failed);
    console.log(`制造商数量：${manufacturers.length}`);

    for (const manufacturer of manufacturers) {
      assertPageOpen(page);
      let models = modelsByManufacturer[manufacturer.text] ?? [];
      if (models.length && models.every((model) => completed.has(rowKey(manufacturer.text, model.text)))) {
        continue;
      }

      const previousModels = await getOptions(page, modelSelector).catch(() => []);
      const changedManufacturer = await chooseOptionIfNeeded(page, manufacturerSelector, manufacturer);
      if (changedManufacturer) await page.waitForTimeout(config.timeouts.settleMs);

      models = changedManufacturer
        ? await waitForOptionsRefresh(page, modelSelector, previousModels, config.timeouts.dropdownMs)
        : await getOptions(page, modelSelector);
      modelsByManufacturer[manufacturer.text] = models;
      checkpoint.modelsByManufacturer = modelsByManufacturer;
      saveCheckpoint(config, checkpoint, completed, failed, { currentManufacturer: manufacturer.text });
      console.log(`${manufacturer.text}: 车型数量 ${models.length}`);

      for (const model of models) {
        assertPageOpen(page);
        const key = rowKey(manufacturer.text, model.text);
        if (completed.has(key)) continue;

        try {
          const changedModel = await chooseOptionIfNeeded(page, modelSelector, model);
          if (changedModel) await page.waitForTimeout(config.timeouts.settleMs);

          await searchButton.click();
          await page.waitForLoadState("networkidle").catch(() => {});
          await page.waitForTimeout(config.timeouts.settleMs);

          const copyButton = await findButton(page, config.selectors.copyButton, config.labels.copyButton);
          await copyButton.click();
          await page.waitForTimeout(300);

          const copied = await readClipboardText(page);
          const rows = parseCopiedTsv(copied);
          if (!rows.length) {
            throw new Error("复制结果中没有有效的 year / manufacturer / model 数据行");
          }
          const mismatched = rows.find((row) => (
            normalizeLabel(row.make) !== normalizeLabel(manufacturer.text)
            || normalizeLabel(row.model) !== normalizeLabel(model.text)
          ));
          if (mismatched) {
            throw new Error(
              `剪贴板数据与当前选择不一致：期望 ${manufacturer.text} / ${model.text}，`
              + `实际 ${mismatched.make} / ${mismatched.model}`
            );
          }
          appendTsv(config.tsvFile, ["year", "make", "model"], rows);

          completed.add(key);
          failed.delete(key);
          saveCheckpoint(config, checkpoint, completed, failed, {
            currentManufacturer: manufacturer.text,
            currentModel: model.text
          });

          console.log(`已复制：${manufacturer.text} / ${model.text}`);
        } catch (error) {
          if (isBrowserClosedError(error)) throw error;

          failed.add(key);
          saveCheckpoint(config, checkpoint, completed, failed, {
            currentManufacturer: manufacturer.text,
            currentModel: model.text,
            lastError: `${manufacturer.text} / ${model.text}: ${error.message}`
          });
          console.log(`跳过失败：${manufacturer.text} / ${model.text}，原因：${error.message}`);
        }
      }
    }

    saveCheckpoint(config, checkpoint, completed, failed, { completedAt: new Date().toISOString() });
    console.log(`完成：${completed.size} 个制造商/车型组合，已写入 ${config.tsvFile}`);
  } finally {
    await context.close().catch(() => {});
  }
}

function rowKey(manufacturer, model) {
  return `${manufacturer}\t${model}`;
}

function normalizeLabel(value) {
  return String(value ?? "").trim().replace(/\s+/g, " ").toLocaleLowerCase("en");
}

function saveCheckpoint(config, checkpoint, completed, failed, extra = {}) {
  writeJson(config.checkpointFile, {
    manufacturers: checkpoint.manufacturers ?? [],
    modelsByManufacturer: checkpoint.modelsByManufacturer ?? {},
    completed: [...completed],
    failed: [...failed],
    updatedAt: new Date().toISOString(),
    ...extra
  });
}

async function readClipboardText(page) {
  const fromBrowser = await page.evaluate(async () => {
    try {
      return await navigator.clipboard.readText();
    } catch {
      return "";
    }
  }).catch(() => "");

  if (fromBrowser) return fromBrowser;

  return execFileSync("powershell.exe", ["-NoProfile", "-Command", "Get-Clipboard -Raw"], {
    encoding: "utf8"
  });
}

function assertPageOpen(page) {
  if (page.isClosed()) throw new Error("Target page, context or browser has been closed");
}

function isBrowserClosedError(error) {
  return /target page, context or browser has been closed|browser has been closed|page has been closed/i.test(error?.message ?? "");
}
