import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import YAML from "yaml";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
export const projectRoot = path.resolve(__dirname, "..");

export function loadConfig(requestedPath = "") {
  const configPath = path.resolve(requestedPath || path.join(projectRoot, "config", "4afitment.yaml"));
  const raw = fs.readFileSync(configPath, "utf8");
  const document = path.extname(configPath).toLowerCase() === ".json" ? JSON.parse(raw) : YAML.parse(raw);
  const config = { ...(document.common || {}), ...(document.scrape || document) };
  const configDir = path.dirname(configPath);

  for (const key of [
    "projectsDir",
    "outputDir",
    "inputTsvFile",
    "checkpointFile",
    "tsvFile",
    "markdownFile",
    "tsvCheckpointFile",
    "tsvMarkdownFile",
    "tsvSummaryFile",
    "requestLogFile",
    "authProfileDir",
    "input",
    "output",
    "log",
    "checkpoint"
  ]) {
    if (config[key]) config[key] = path.resolve(configDir, config[key]);
  }

  return config;
}

export function ensureDir(dir) {
  fs.mkdirSync(dir, { recursive: true });
}
