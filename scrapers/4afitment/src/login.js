import readline from "node:readline/promises";
import { stdin as input, stdout as output } from "node:process";
import { openBrowser } from "./browser.js";

const { config, context, page } = await openBrowser();

await page.goto(config.startUrl, { waitUntil: "domcontentloaded" });

console.log("");
console.log("浏览器已打开。请手动登录 4AFitment，并确认能看到车辆兼容搜索页面。");
console.log("完成后回到这里按回车，登录状态会保存到 .auth/profile。");
console.log("");

const rl = readline.createInterface({ input, output });
await rl.question("登录完成后按回车继续...");
rl.close();

await context.close();
console.log("已保存登录状态。");
