import { chromium } from "playwright";
import { ensureDir, loadConfig } from "./config.js";

export async function openBrowser(configPath = "") {
  const config = loadConfig(configPath);
  ensureDir(config.authProfileDir);

  const context = await chromium.launchPersistentContext(config.authProfileDir, {
    headless: config.headless,
    slowMo: config.slowMoMs,
    viewport: { width: 1440, height: 1000 }
  });

  const page = context.pages()[0] ?? await context.newPage();
  page.setDefaultTimeout(config.timeouts.dropdownMs);
  page.setDefaultNavigationTimeout(config.timeouts.pageLoadMs);

  return { config, context, page };
}
