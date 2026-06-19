import { openBrowser } from "./browser.js";
import { inspectControls } from "./dom.js";

const { config, context, page } = await openBrowser();

await page.goto(config.startUrl, { waitUntil: "domcontentloaded" });
await page.waitForLoadState("networkidle").catch(() => {});

const controls = await inspectControls(page);
console.table(controls);

await context.close();
