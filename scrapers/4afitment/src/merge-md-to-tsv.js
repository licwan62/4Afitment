import fs from "node:fs";
import path from "node:path";

const args = parseArgs(process.argv.slice(2));
const projectName = sanitizeFileStem(args.project || discoverDefaultProject() || "carlist");
const projectsDir = path.resolve(process.cwd(), args.projectsDir || args.projects || "projects");
const projectDir = path.resolve(process.cwd(), args.projectDir || path.join(projectsDir, projectName));
const projectOutputDir = path.join(projectDir, "output");
const outputName = sanitizeFileStem(args.name || "from_tsv");
const defaultProjectFile = path.join(projectOutputDir, `${outputName}.md`);
const inputFile = args.file || args._[0] || defaultProjectFile;

if (!inputFile) {
  console.error("用法：.\\run.ps1 src\\merge-md-to-tsv.js --project brandlimit_0617 --file from_tsv.md");
  process.exit(1);
}

const resolvedInput = resolveProjectPath(inputFile, projectOutputDir);
if (!fs.existsSync(resolvedInput)) {
  console.error(`找不到文件：${resolvedInput}`);
  process.exit(1);
}

const outputFile = args.output || args.out
  ? resolveProjectPath(args.output || args.out, projectOutputDir)
  : replaceExtension(resolvedInput, ".tsv");

const markdown = fs.readFileSync(resolvedInput, "utf8");
const rows = extractRows(markdown);

fs.mkdirSync(path.dirname(outputFile), { recursive: true });
fs.writeFileSync(outputFile, toTsv(rows), "utf8");

console.log(`输入：${resolvedInput}`);
console.log(`输出：${outputFile}`);
console.log(`记录数：${rows.length}`);

function extractRows(markdownText) {
  const blocks = [...markdownText.matchAll(/```(?:text|tsv)?\s*\r?\n([\s\S]*?)```/gi)]
    .map((match) => match[1]);

  const seen = new Set();
  const rows = [];

  for (const block of blocks) {
    for (const line of block.split(/\r?\n/)) {
      if (!line.trim()) continue;

      const cells = trimTrailingEmptyCells(line.split("\t").map((cell) => cell.trim()));
      if (cells.length < 3) continue;

      const row = {
        year: cells[0],
        make: cells[1],
        model: cells[2]
      };

      if (!/^\d{4}$/.test(row.year)) continue;

      const key = `${row.year}\t${row.make}\t${row.model}`;
      if (seen.has(key)) continue;

      seen.add(key);
      rows.push(row);
    }
  }

  rows.sort((a, b) => {
    const make = a.make.localeCompare(b.make, "en", { sensitivity: "base" });
    if (make) return make;
    const model = a.model.localeCompare(b.model, "en", { sensitivity: "base" });
    if (model) return model;
    return Number(a.year) - Number(b.year);
  });

  return rows;
}

function trimTrailingEmptyCells(cells) {
  const next = [...cells];
  while (next.length && !next[next.length - 1]) next.pop();
  return next;
}

function toTsv(rows) {
  const lines = ["year\tmake\tmodel"];
  for (const row of rows) {
    lines.push([row.year, row.make, row.model].map(escapeTsv).join("\t"));
  }
  return `${lines.join("\n")}\n`;
}

function escapeTsv(value) {
  return String(value ?? "").replace(/\r?\n/g, " ").trim();
}

function replaceExtension(file, extension) {
  return path.join(path.dirname(file), `${path.basename(file, path.extname(file))}${extension}`);
}

function resolveProjectPath(value, baseDir) {
  const text = String(value || "");
  if (path.isAbsolute(text)) return path.resolve(text);
  if (text.includes("/") || text.includes("\\")) return path.resolve(process.cwd(), text);
  return path.join(baseDir, text);
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
      parsed[name] = inlineValue;
      continue;
    }

    const next = argv[index + 1];
    if (next && !next.startsWith("--")) {
      parsed[name] = next;
      index += 1;
    } else {
      parsed[name] = true;
    }
  }
  return parsed;
}

function discoverDefaultProject() {
  const projectsDir = path.resolve(process.cwd(), args.projectsDir || args.projects || "projects");
  if (!fs.existsSync(projectsDir)) return "";

  const projects = fs.readdirSync(projectsDir, { withFileTypes: true })
    .filter((entry) => entry.isDirectory() && !entry.name.startsWith("."))
    .map((entry) => ({
      name: entry.name,
      markdown: path.join(projectsDir, entry.name, "output", "from_tsv.md")
    }))
    .filter((entry) => fs.existsSync(entry.markdown))
    .map((entry) => ({
      ...entry,
      mtimeMs: fs.statSync(entry.markdown).mtimeMs
    }))
    .sort((a, b) => b.mtimeMs - a.mtimeMs);

  return projects[0]?.name || "";
}

function sanitizeFileStem(value) {
  return String(value || "from_tsv")
    .trim()
    .replace(/[<>:"/\\|?*\x00-\x1F]/g, "_")
    .replace(/\s+/g, "_")
    || "from_tsv";
}
