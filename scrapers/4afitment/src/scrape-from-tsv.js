import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";
import readXlsxFile from "read-excel-file/node";
import { openBrowser } from "./browser.js";
import { loadConfig, projectRoot } from "./config.js";
import {
  chooseOptionIfNeeded,
  chooseOptionTextIfNeeded,
  findButton,
  findControl,
  findYearRangeControls,
  getOptions,
  waitForOptionsRefresh
} from "./dom.js";
import { appendJsonLine, csvEscape, readJson, writeJson } from "./io.js";

const cli = parseArgs(process.argv.slice(2));
let restartCount = 0;
let resetDone = false;
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
    try {
      const config = loadConfig(cli.config);
      appendJsonLine(config.log || config.requestLogFile, { time: new Date().toISOString(), level: "error", event: "run_failed", detail: error.message });
    } catch {
      // Preserve the original error if configuration or log initialization also fails.
    }
    throw error;
  }
}

async function runPass() {
  const { config, context, page } = await openBrowser(cli.config);
  applyCliOptions(config, cli);

  try {
    if (cli.reset && !resetDone) {
      resetOutputFiles(config);
      resetDone = true;
    }

    const input = await parseInputTable(config.inputTsvFile, { optional: config.allMode, sheetname: config.sheetname });
    const maxInputRows = Number(cli.max ?? config.max ?? 0);
    if (!Number.isInteger(maxInputRows) || maxInputRows < 0) throw new Error("--max 必须是大于等于 0 的整数");
    if (maxInputRows) input.entries = input.entries.slice(0, maxInputRows);
    const skip = await readSkipFile(cli.skipMd || cli.skip || cli.excludeMd || cli.exclude || cli.skipTsv || cli.excludeTsv, config.projectOutputDir);
    const checkpoint = readJson(config.tsvCheckpointFile, {
      completed: [],
      failed: [],
      notFound: [],
      modelsByManufacturer: {}
    });
    const completed = new Set(checkpoint.completed ?? []);
    const failed = new Set(checkpoint.failed ?? []);
    const notFound = checkpoint.notFound ?? [];
    const modelsByManufacturer = checkpoint.modelsByManufacturer ?? {};

    appendJsonLine(config.requestLogFile, { time: new Date().toISOString(), event: "run_started", input: config.inputTsvFile, sheetname: config.sheetname || null, output: config.tsvMarkdownFile });
    ensureCsvHeader(config);
    writeSummary(config, input, checkpoint);

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
      throw new Error("当前还是登录页。请先运行 .\\run.ps1 src\\login.js，手动登录后再运行 TSV 抓取。");
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
    console.log(`Project：${config.projectName}`);
    console.log(`表格输入：${config.inputTsvFile}`);
    console.log(`CSV 输出：${config.tsvMarkdownFile}`);
    if (skip.keys.size || skip.brands.size) {
      console.log(`Skip 组合数量：${skip.keys.size}`);
      console.log(`Skip 品牌数量：${skip.brands.size}`);
    }

    const changedYearFrom = await chooseOptionTextIfNeeded(page, yearSelectors.from, config.yearRange.from);
    if (changedYearFrom) await page.waitForTimeout(config.timeouts.settleMs);
    const changedYearTo = await chooseOptionTextIfNeeded(page, yearSelectors.to, config.yearRange.to);
    if (changedYearTo) await page.waitForTimeout(config.timeouts.settleMs);

    const siteManufacturers = await getOptions(page, manufacturerSelector);
    if (input.allMode) {
      input.entries = siteManufacturers.map((manufacturer) => ({ make: manufacturer.text, model: "" }));
    }
    console.log(`输入品牌数量：${input.entries.length}`);

    for (const entry of input.entries) {
      assertPageOpen(page);

      const manufacturer = findOption(siteManufacturers, entry.make);
      if (!manufacturer) {
        recordNotFound(notFound, {
          make: entry.make,
          model: entry.model,
          reason: "品牌在 4AFitment 制造商下拉列表中找不到"
        });
        appendJsonLine(config.requestLogFile, { time: new Date().toISOString(), event: "not_found", make: entry.make, model: entry.model, reason: "manufacturer_not_found" });
        saveCheckpoint(config, checkpoint, completed, failed, notFound);
        writeSummary(config, input, { ...checkpoint, notFound });
        console.log(`找不到品牌：${entry.make}`);
        continue;
      }
      if (skip.brands.has(normalizeText(manufacturer.text))) {
        console.log(`跳过品牌：${manufacturer.text}`);
        continue;
      }

      let models = modelsByManufacturer[manufacturer.text] ?? [];
      const needsAllModels = !entry.model;
      const requestedModelsAlreadyDone = !needsAllModels
        && isDoneOrSkipped(completed, skip.keys, rowKey(manufacturer.text, entry.model));
      if (requestedModelsAlreadyDone) continue;

      const previousModels = await getOptions(page, modelSelector).catch(() => []);
      const changedManufacturer = await chooseOptionIfNeeded(page, manufacturerSelector, manufacturer);
      if (changedManufacturer) await page.waitForTimeout(config.timeouts.settleMs);

      models = changedManufacturer
        ? await waitForOptionsRefresh(page, modelSelector, previousModels, config.timeouts.dropdownMs)
        : await getOptions(page, modelSelector);
      modelsByManufacturer[manufacturer.text] = models;
      checkpoint.modelsByManufacturer = modelsByManufacturer;
      saveCheckpoint(config, checkpoint, completed, failed, notFound, { currentManufacturer: manufacturer.text });

      const targetModels = needsAllModels
        ? models
        : [findOption(models, entry.model)].filter(Boolean);

      if (!targetModels.length) {
        recordNotFound(notFound, {
          make: entry.make,
          model: entry.model,
          reason: "车型在该品牌车型下拉列表中找不到"
        });
        appendJsonLine(config.requestLogFile, { time: new Date().toISOString(), event: "not_found", make: entry.make, model: entry.model, reason: "model_not_found" });
        saveCheckpoint(config, checkpoint, completed, failed, notFound);
        writeSummary(config, input, { ...checkpoint, notFound });
        console.log(`找不到车型：${entry.make} / ${entry.model}`);
        continue;
      }

      if (needsAllModels && targetModels.every((model) => isDoneOrSkipped(completed, skip.keys, rowKey(manufacturer.text, model.text)))) {
        continue;
      }

      console.log(`${manufacturer.text}: 准备处理 ${targetModels.length} 个车型`);

      for (const model of targetModels) {
        assertPageOpen(page);
        const key = rowKey(manufacturer.text, model.text);
        if (skip.keys.has(key)) continue;
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
          appendCsvRow(config, {
            manufacturer: manufacturer.text,
            model: model.text,
            content: copied
          });

          completed.add(key);
          failed.delete(key);
          saveCheckpoint(config, checkpoint, completed, failed, notFound, {
            currentManufacturer: manufacturer.text,
            currentModel: model.text
          });
          writeSummary(config, input, { ...checkpoint, notFound, completed: [...completed], failed: [...failed] });

          console.log(`已复制：${manufacturer.text} / ${model.text}`);
        } catch (error) {
          if (isBrowserClosedError(error)) throw error;

          failed.add(key);
          appendJsonLine(config.requestLogFile, { time: new Date().toISOString(), event: "row_failed", make: manufacturer.text, model: model.text, reason: error.message });
          saveCheckpoint(config, checkpoint, completed, failed, notFound, {
            currentManufacturer: manufacturer.text,
            currentModel: model.text,
            lastError: `${manufacturer.text} / ${model.text}: ${error.message}`
          });
          writeSummary(config, input, { ...checkpoint, notFound, completed: [...completed], failed: [...failed] });
          console.log(`跳过失败：${manufacturer.text} / ${model.text}，原因：${error.message}`);
        }
      }
    }

    saveCheckpoint(config, checkpoint, completed, failed, notFound, { completedAt: new Date().toISOString() });
    writeSummary(config, input, { ...checkpoint, notFound, completed: [...completed], failed: [...failed] });
    appendJsonLine(config.requestLogFile, { time: new Date().toISOString(), event: "run_completed", completed: completed.size, failed: failed.size, not_found: notFound.length });
    console.log(`完成：${completed.size} 个组合，输出 ${config.tsvMarkdownFile}`);
    console.log(`Summary：${config.tsvSummaryFile}`);
  } finally {
    await context.close().catch(() => {});
  }
}

function applyCliOptions(config, args) {
  const requestedInput = args.file || args.input || config.input || config.inputTsvFile || "";
  const projectName = sanitizeFileStem(args.name || config.name || "4afitment");
  const projectDir = path.resolve(config.projectDir || projectRoot);
  const projectInputDir = path.resolve(config.input || path.join(projectDir, "..", "input"));
  const projectOutputDir = path.resolve(args.output || args.out || config.output || path.join(projectDir, "..", "output"));
  const outputName = sanitizeFileStem(args.name || args.prefix || "from_tsv");
  const discoveredInput = discoverInputFile(requestedInput, projectInputDir);

  config.projectName = projectName;
  config.projectsDir = projectsDir;
  config.projectDir = projectDir;
  config.projectInputDir = projectInputDir;
  config.projectOutputDir = projectOutputDir;
  config.inputTsvFile = discoveredInput;
  config.sheetname = args.sheetname || config.sheetname || undefined;
  config.max = args.max ?? config.max ?? 0;
  config.allMode = !requestedInput && !config.input && !fs.existsSync(config.inputTsvFile);
  config.tsvMarkdownFile = path.join(projectOutputDir, `${outputName}.csv`);
  config.tsvSummaryFile = path.join(projectOutputDir, `${outputName}_summary.csv`);
  config.tsvCheckpointFile = args.checkpoint
    ? resolveProjectPath(args.checkpoint, projectOutputDir)
    : path.join(projectOutputDir, `${outputName}_checkpoint.json`);
  config.requestLogFile = path.resolve(args.log || config.log || path.join(projectDir, "..", "log", "4afitment.log"));
}

function resolveProjectRef(project, projectsDir) {
  if (!project) return { name: "", dir: "" };

  const raw = String(project).trim().replace(/[\\/]+$/, "");
  const looksLikePath = path.isAbsolute(raw) || raw.includes("/") || raw.includes("\\");
  if (!looksLikePath) return { name: sanitizeFileStem(raw), dir: "" };

  const dir = path.resolve(process.cwd(), raw);
  return {
    name: sanitizeFileStem(path.basename(dir)),
    dir
  };
}

function resetOutputFiles(config) {
  for (const file of [config.tsvMarkdownFile, config.tsvSummaryFile, config.tsvCheckpointFile]) {
    if (fs.existsSync(file)) fs.rmSync(file);
  }
}

async function parseInputTable(file, options = {}) {
  if (!fs.existsSync(file)) {
    if (options.optional) {
      return {
        file: "ALL_SITE_MANUFACTURERS",
        hasModelColumn: false,
        allMode: true,
        entries: []
      };
    }
    throw new Error(`找不到输入表格：${file}`);
  }

  const extension = path.extname(file).toLowerCase();
  let rows;
  if (extension === ".xlsx") {
    try {
      rows = await readXlsxFile(file, { sheet: options.sheetname || 1 });
    } catch (error) {
      throw new Error(`读取 Excel 失败${options.sheetname ? `（sheet: ${options.sheetname}）` : ""}：${error.message}`);
    }
  } else {
    const delimiter = extension === ".csv" ? "," : "\t";
    const raw = fs.readFileSync(file, "utf8").replace(/^\uFEFF/, "");
    rows = raw.split(/\r?\n/).filter((line) => line.trim()).map((line) => delimiter === "\t" ? splitTsvLine(line) : parseCsvLine(line));
  }
  const lines = rows.filter((row) => row.some((cell) => String(cell).trim()));
  if (!lines.length) throw new Error(`输入表格为空：${file}`);

  const first = lines[0].map((cell) => String(cell).trim());
  const normalizedHeader = first.map(normalizeHeader);
  const hasHeader = normalizedHeader.some((name) => ["make", "model"].includes(name));

  const makeIndex = hasHeader ? normalizedHeader.findIndex((name) => name === "make") : 0;
  const modelIndex = hasHeader ? normalizedHeader.findIndex((name) => name === "model") : 1;
  if (makeIndex < 0) throw new Error("输入表格需要包含品牌列：make / brand / manufacturer / 品牌");

  const dataLines = hasHeader ? lines.slice(1) : lines;
  const entries = [];
  const seen = new Set();

  for (const line of dataLines) {
    const cells = line.map((cell) => String(cell).trim());
    const make = (cells[makeIndex] ?? "").trim();
    const model = modelIndex >= 0 ? (cells[modelIndex] ?? "").trim() : "";
    if (!make) continue;

    const key = `${normalizeText(make)}\t${normalizeText(model)}`;
    if (seen.has(key)) continue;
    seen.add(key);
    entries.push({ make, model });
  }

  return {
    file,
    hasModelColumn: modelIndex >= 0,
    allMode: false,
    entries
  };
}

async function readSkipFile(file, projectOutputDir = process.cwd()) {
  const empty = { keys: new Set(), brands: new Set() };
  if (!file) return empty;

  const resolved = resolveProjectPath(file, projectOutputDir);
  if (!fs.existsSync(resolved)) {
    throw new Error(`找不到 skip 文件：${resolved}`);
  }

  if (path.extname(resolved).toLowerCase() === ".tsv") {
    return readSkipTsv(resolved);
  }

  const markdown = fs.readFileSync(resolved, "utf8");
  const keys = new Set();

  for (const match of markdown.matchAll(/^##\s+(.+?)\s+\/\s+(.+?)\s*$/gm)) {
    keys.add(rowKey(match[1].trim(), match[2].trim()));
  }

  for (const block of markdown.matchAll(/\`\`\`(?:text|tsv)?\s*\r?\n([\s\S]*?)\`\`\`/gi)) {
    for (const line of block[1].split(/\r?\n/)) {
      if (!line.trim()) continue;
      const cells = line.split("\t").map((cell) => cell.trim());
      if (cells.length < 3) continue;
      if (!/^\d{4}$/.test(cells[0])) continue;
      keys.add(rowKey(cells[1], cells[2]));
    }
  }

  return { keys, brands: new Set() };
}

async function readSkipTsv(file) {
  const input = await parseInputTable(file);
  const keys = new Set();
  const brands = new Set();

  for (const entry of input.entries) {
    if (entry.model) {
      keys.add(rowKey(entry.make, entry.model));
    } else {
      brands.add(normalizeText(entry.make));
    }
  }

  return { keys, brands };
}

function splitTsvLine(line) {
  return line.split("\t").map((cell) => cell.trim());
}

function normalizeHeader(value) {
  const text = String(value ?? "").trim().toLowerCase().replaceAll(" ", "").replaceAll("_", "");
  if (["make", "brand", "manufacturer", "manufacture", "品牌", "制造商", "厂商"].includes(text)) return "make";
  if (["model", "车型", "车系"].includes(text)) return "model";
  return text;
}

function findOption(options, expected) {
  const target = normalizeText(expected);
  return options.find((option) => normalizeText(option.text) === target)
    || options.find((option) => normalizeText(option.value) === target)
    || null;
}

function normalizeText(value) {
  return String(value ?? "").trim().replace(/\s+/g, " ").toLowerCase();
}

function recordNotFound(notFound, item) {
  const key = `${item.make}\t${item.model ?? ""}\t${item.reason}`;
  if (notFound.some((existing) => `${existing.make}\t${existing.model ?? ""}\t${existing.reason}` === key)) return;
  notFound.push({ ...item, at: new Date().toISOString() });
}

function rowKey(manufacturer, model) {
  return `${manufacturer}\t${model}`;
}

function isDoneOrSkipped(completed, skipKeys, key) {
  return completed.has(key) || skipKeys.has(key);
}

function saveCheckpoint(config, checkpoint, completed, failed, notFound, extra = {}) {
  writeJson(config.tsvCheckpointFile, {
    modelsByManufacturer: checkpoint.modelsByManufacturer ?? {},
    completed: [...completed],
    failed: [...failed],
    notFound,
    updatedAt: new Date().toISOString(),
    ...extra
  });
}

function ensureCsvHeader(config) {
  fs.mkdirSync(path.dirname(config.tsvMarkdownFile), { recursive: true });
  if (fs.existsSync(config.tsvMarkdownFile)) return;
  fs.writeFileSync(config.tsvMarkdownFile, "make,model,year_from,year_to,content,copied_at\n", "utf8");
}

function appendCsvRow(config, { manufacturer, model, content }) {
  const values = [manufacturer, model, config.yearRange.from, config.yearRange.to, String(content || "").trim(), new Date().toISOString()];
  fs.appendFileSync(config.tsvMarkdownFile, `${values.map(csvEscape).join(",")}\n`, "utf8");
}

function writeSummary(config, input, checkpoint) {
  fs.mkdirSync(path.dirname(config.tsvSummaryFile), { recursive: true });
  const notFound = checkpoint.notFound ?? [];
  const failed = checkpoint.failed ?? [];
  const completed = checkpoint.completed ?? [];

  const lines = ["status,make,model,reason"];
  for (const key of completed) {
    const [make, model] = String(key).split("\t", 2);
    lines.push(["completed", make, model, ""].map(csvEscape).join(","));
  }
  for (const key of failed) {
    const [make, model] = String(key).split("\t", 2);
    lines.push(["failed", make, model, ""].map(csvEscape).join(","));
  }
  for (const item of notFound) lines.push(["not_found", item.make, item.model || "", item.reason].map(csvEscape).join(","));
  fs.writeFileSync(config.tsvSummaryFile, `${lines.join("\n")}\n`, "utf8");
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

function sanitizeFileStem(value) {
  return String(value || "from_tsv")
    .trim()
    .replace(/[<>:"/\\|?*\x00-\x1F]/g, "_")
    .replace(/\s+/g, "_")
    || "from_tsv";
}

function inferProjectName(file) {
  const resolved = path.resolve(process.cwd(), String(file || "carlist"));
  const parts = resolved.split(path.sep);
  const inputIndex = parts.lastIndexOf("input");
  const projectsIndex = parts.lastIndexOf("projects");
  if (projectsIndex >= 0 && parts[projectsIndex + 1]) return parts[projectsIndex + 1];
  if (inputIndex >= 0 && parts[inputIndex + 1] && parts[inputIndex + 1] !== path.basename(resolved)) {
    return parts[inputIndex + 1];
  }

  const stem = path.basename(resolved, path.extname(resolved));
  return stem || "carlist";
}

function discoverDefaultInput(configInputFile, projectInputDir = "", options = {}) {
  if (projectInputDir) {
    const projectInput = path.join(projectInputDir, path.basename(configInputFile));
    if (fs.existsSync(projectInput)) return projectInput;
    if (!options.allowRootFallback) return projectInput;
  }

  const configured = path.resolve(process.cwd(), configInputFile);
  if (fs.existsSync(configured)) return configured;

  const rootInputDir = path.resolve(process.cwd(), "input");
  const found = findFirstTsv(rootInputDir);
  return found || configured;
}

function resolveProjectPath(value, baseDir) {
  const text = String(value || "");
  if (path.isAbsolute(text)) return path.resolve(text);
  if (text.includes("/") || text.includes("\\")) return path.resolve(process.cwd(), text);
  return path.join(baseDir, text);
}

function findFirstTsv(dir) {
  if (!fs.existsSync(dir)) return "";

  const entries = fs.readdirSync(dir, { withFileTypes: true })
    .filter((entry) => !entry.name.startsWith("."))
    .sort((a, b) => a.name.localeCompare(b.name));

  for (const entry of entries) {
    const fullPath = path.join(dir, entry.name);
    if (entry.isFile() && /\.(tsv|csv|xlsx)$/i.test(entry.name)) return fullPath;
  }

  for (const entry of entries) {
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      const found = findFirstTsv(fullPath);
      if (found) return found;
    }
  }

  return "";
}

function discoverInputFile(value, defaultDir) {
  const resolved = path.resolve(value || defaultDir);
  if (fs.existsSync(resolved) && fs.statSync(resolved).isDirectory()) {
    return findFirstTsv(resolved) || path.join(resolved, "input.tsv");
  }
  return resolved;
}

function parseCsvLine(line) {
  const cells = [];
  let value = "";
  let quoted = false;
  for (let index = 0; index < line.length; index += 1) {
    const char = line[index];
    if (char === '"') {
      if (quoted && line[index + 1] === '"') { value += '"'; index += 1; }
      else quoted = !quoted;
    } else if (char === "," && !quoted) { cells.push(value.trim()); value = ""; }
    else value += char;
  }
  cells.push(value.trim());
  return cells;
}

function parseArgs(argv) {
  const parsed = { _: [] };
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (!arg.startsWith("--")) {
      parsed._.push(arg);
      continue;
    }

    const rawName = arg.slice(2);
    const [name, inlineValue] = rawName.split("=", 2);
    if (inlineValue !== undefined) {
      setArg(parsed, name, inlineValue);
      continue;
    }

    const next = argv[index + 1];
    if (next && !next.startsWith("--")) {
      setArg(parsed, name, next);
      index += 1;
    } else {
      setArg(parsed, name, true);
    }
  }

  if (!parsed.file && !parsed.input && parsed._[0]) parsed.file = parsed._[0];
  return parsed;
}

function setArg(parsed, name, value) {
  parsed[name] = value;
  const camelName = name.replace(/-([a-z])/g, (_, char) => char.toUpperCase());
  parsed[camelName] = value;
}
