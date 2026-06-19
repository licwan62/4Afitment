import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { openBrowser } from "./browser.js";
import {
  chooseOptionIfNeeded,
  chooseOptionTextIfNeeded,
  findButton,
  findControl,
  findYearRangeControls,
  getOptions
} from "./dom.js";
import { appendJsonLine, readJson, writeJson } from "./io.js";

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
    throw error;
  }
}

async function runPass() {
  const { config, context, page } = await openBrowser();
  applyCliOptions(config, cli);

  try {
    if (cli.reset && !resetDone) {
      resetOutputFiles(config);
      resetDone = true;
    }

    const input = parseInputTsv(config.inputTsvFile, { optional: config.allMode });
    const skip = readSkipFile(cli.skipMd || cli.skip || cli.excludeMd || cli.exclude || cli.skipTsv || cli.excludeTsv, config.projectOutputDir);
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

    if (fs.existsSync(config.requestLogFile)) fs.rmSync(config.requestLogFile);
    ensureMarkdownHeader(config, input);
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
    console.log(`TSV 输入：${config.inputTsvFile}`);
    console.log(`Markdown 输出：${config.tsvMarkdownFile}`);
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
    console.log(`TSV 品牌数量：${input.entries.length}`);

    for (const entry of input.entries) {
      assertPageOpen(page);

      const manufacturer = findOption(siteManufacturers, entry.make);
      if (!manufacturer) {
        recordNotFound(notFound, {
          make: entry.make,
          model: entry.model,
          reason: "品牌在 4AFitment 制造商下拉列表中找不到"
        });
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

      const changedManufacturer = await chooseOptionIfNeeded(page, manufacturerSelector, manufacturer);
      if (changedManufacturer) await page.waitForTimeout(config.timeouts.settleMs);

      if (!models.length) {
        models = await getOptions(page, modelSelector);
        modelsByManufacturer[manufacturer.text] = models;
        checkpoint.modelsByManufacturer = modelsByManufacturer;
        saveCheckpoint(config, checkpoint, completed, failed, notFound, { currentManufacturer: manufacturer.text });
      }

      const targetModels = needsAllModels
        ? models
        : [findOption(models, entry.model)].filter(Boolean);

      if (!targetModels.length) {
        recordNotFound(notFound, {
          make: entry.make,
          model: entry.model,
          reason: "车型在该品牌车型下拉列表中找不到"
        });
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
          appendMarkdownSection(config, {
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
    console.log(`完成：${completed.size} 个组合，输出 ${config.tsvMarkdownFile}`);
    console.log(`Summary：${config.tsvSummaryFile}`);
  } finally {
    await context.close().catch(() => {});
  }
}

function applyCliOptions(config, args) {
  const requestedInput = args.file || args.input || "";
  const projectsDir = path.resolve(process.cwd(), args.projectsDir || args.projects || config.projectsDir || "projects");
  const projectRef = resolveProjectRef(args.project, projectsDir);
  const projectName = projectRef.name || sanitizeFileStem(inferProjectName(requestedInput || discoverDefaultInput(config.inputTsvFile)));
  const projectDir = path.resolve(process.cwd(), args.projectDir || projectRef.dir || path.join(projectsDir, projectName));
  const projectInputDir = path.join(projectDir, "input");
  const projectOutputDir = path.join(projectDir, "output");
  const outputName = sanitizeFileStem(args.name || args.prefix || "from_tsv");

  const defaultProjectInput = path.join(projectInputDir, path.basename(config.inputTsvFile));
  const discoveredInput = requestedInput
    ? resolveProjectPath(requestedInput, projectInputDir)
    : discoverDefaultInput(config.inputTsvFile, projectInputDir, { allowRootFallback: !args.project });

  config.projectName = projectName;
  config.projectsDir = projectsDir;
  config.projectDir = projectDir;
  config.projectInputDir = projectInputDir;
  config.projectOutputDir = projectOutputDir;
  config.inputTsvFile = requestedInput ? discoveredInput : (fs.existsSync(defaultProjectInput) ? defaultProjectInput : discoveredInput);
  config.allMode = !requestedInput && !fs.existsSync(config.inputTsvFile);
  config.tsvMarkdownFile = args.output || args.out
    ? resolveProjectPath(args.output || args.out, projectOutputDir)
    : path.join(projectOutputDir, `${outputName}.md`);
  config.tsvSummaryFile = args.summary
    ? resolveProjectPath(args.summary, projectOutputDir)
    : path.join(projectOutputDir, `${outputName}_summary.md`);
  config.tsvCheckpointFile = args.checkpoint
    ? resolveProjectPath(args.checkpoint, projectOutputDir)
    : path.join(projectOutputDir, `${outputName}_checkpoint.json`);
  config.requestLogFile = args.networkLog
    ? resolveProjectPath(args.networkLog, projectOutputDir)
    : path.join(projectOutputDir, "network.jsonl");
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

function parseInputTsv(file, options = {}) {
  if (!fs.existsSync(file)) {
    if (options.optional) {
      return {
        file: "ALL_SITE_MANUFACTURERS",
        hasModelColumn: false,
        allMode: true,
        entries: []
      };
    }
    throw new Error(`找不到 TSV 文件：${file}`);
  }

  const raw = fs.readFileSync(file, "utf8").replace(/^\uFEFF/, "");
  const lines = raw.split(/\r?\n/).filter((line) => line.trim());
  if (!lines.length) throw new Error(`TSV 文件为空：${file}`);

  const first = splitTsvLine(lines[0]);
  const normalizedHeader = first.map(normalizeHeader);
  const hasHeader = normalizedHeader.some((name) => ["make", "model"].includes(name));

  const makeIndex = hasHeader ? normalizedHeader.findIndex((name) => name === "make") : 0;
  const modelIndex = hasHeader ? normalizedHeader.findIndex((name) => name === "model") : 1;
  if (makeIndex < 0) throw new Error("TSV 需要包含品牌列：make / brand / manufacturer / 品牌");

  const dataLines = hasHeader ? lines.slice(1) : lines;
  const entries = [];
  const seen = new Set();

  for (const line of dataLines) {
    const cells = splitTsvLine(line);
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

function readSkipFile(file, projectOutputDir = process.cwd()) {
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

function readSkipTsv(file) {
  const input = parseInputTsv(file);
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

function ensureMarkdownHeader(config, input) {
  fs.mkdirSync(path.dirname(config.tsvMarkdownFile), { recursive: true });
  if (fs.existsSync(config.tsvMarkdownFile)) return;

  fs.writeFileSync(
    config.tsvMarkdownFile,
    [
      "# 4AFitment TSV Copied Vehicle Data",
      "",
      `Input: ${input.file}`,
      `Year range: ${config.yearRange.from} - ${config.yearRange.to}`,
      `Generated at: ${new Date().toISOString()}`,
      ""
    ].join("\n"),
    "utf8"
  );
}

function appendMarkdownSection(config, { manufacturer, model, content }) {
  const safeContent = String(content || "").replaceAll("```", "`\\`\\`");
  const block = [
    "",
    `## ${manufacturer} / ${model}`,
    "",
    `- Year range: ${config.yearRange.from} - ${config.yearRange.to}`,
    `- Copied at: ${new Date().toISOString()}`,
    "",
    "```text",
    safeContent.trim(),
    "```",
    ""
  ].join("\n");

  fs.appendFileSync(config.tsvMarkdownFile, block, "utf8");
}

function writeSummary(config, input, checkpoint) {
  fs.mkdirSync(path.dirname(config.tsvSummaryFile), { recursive: true });
  const notFound = checkpoint.notFound ?? [];
  const failed = checkpoint.failed ?? [];
  const completed = checkpoint.completed ?? [];

  const lines = [
    "# 4AFitment TSV Summary",
    "",
    `Input: ${input.file}`,
    `Input rows: ${input.entries.length}`,
    `Completed combinations: ${completed.length}`,
    `Failed combinations: ${failed.length}`,
    `Not found rows: ${notFound.length}`,
    `Updated at: ${new Date().toISOString()}`,
    ""
  ];

  if (notFound.length) {
    lines.push("## Not Found", "");
    for (const item of notFound) {
      lines.push(`- ${item.make}${item.model ? ` / ${item.model}` : ""}: ${item.reason}`);
    }
    lines.push("");
  }

  if (failed.length) {
    lines.push("## Failed", "");
    for (const item of failed) lines.push(`- ${item}`);
    lines.push("");
  }

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
    if (entry.isFile() && entry.name.toLowerCase().endsWith(".tsv")) return fullPath;
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
