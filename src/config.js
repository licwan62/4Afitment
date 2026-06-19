import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
export const projectRoot = path.resolve(__dirname, "..");

export function loadConfig() {
  const configPath = path.join(projectRoot, "config.json");
  const raw = fs.readFileSync(configPath, "utf8");
  const config = JSON.parse(raw);

  for (const key of [
    "projectsDir",
    "outputDir",
    "inputTsvFile",
    "checkpointFile",
    "markdownFile",
    "tsvCheckpointFile",
    "tsvMarkdownFile",
    "tsvSummaryFile",
    "requestLogFile",
    "authProfileDir"
  ]) {
    config[key] = path.resolve(projectRoot, config[key]);
  }

  return config;
}

export function ensureDir(dir) {
  fs.mkdirSync(dir, { recursive: true });
}
