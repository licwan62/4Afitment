#!/usr/bin/env python3
"""遍历 Amazon.de 商品页车型弹窗中的五级联动下拉框。

输出字段：vehicle_type, make, model, variant, engine_type

Amazon 的 AUI 下拉框通常由一个隐藏的原生 <select> 和一个可见按钮组成。
本脚本优先驱动原生 select；找不到时，再退回到自定义下拉选项点击模式。
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    JavascriptException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.ui import WebDriverWait


PROJECT_ROOT = Path(__file__).resolve().parent
START_URL = (
    "https://www.amazon.de/Protection-Tarpaulin-Price-Performance-Protect-"
    "Polyester/dp/B07RLG4NZV/?th=1"
)
POPOVER_XPATH = '//*[@id="a-popover-2"]'
FIELD_NAMES = ("vehicle_type", "make", "model", "variant", "engine_type")
FIELD_HINTS = (
    ("select a vehicle type", "vehicle type", "fahrzeugtyp"),
    ("select make", "make", "marke", "hersteller"),
    ("select model", "model", "modell"),
    ("select variant", "variant", "variante"),
    ("select engine type", "engine type", "motor", "motortyp"),
)
PLACEHOLDER_RE = re.compile(
    r"^(select|choose|please select|auswählen|bitte wählen|请选择)\b", re.I
)


@dataclass(frozen=True)
class Choice:
    text: str
    value: str = ""
    index: int = -1


class AmazonFitmentScraper:
    def __init__(self, driver: webdriver.Chrome, args: argparse.Namespace) -> None:
        self.driver = driver
        self.args = args
        self.wait = WebDriverWait(driver, args.timeout)
        self.output = Path(args.output).resolve()
        self.error_log = self.output.with_suffix(".errors.log")
        self.rows: list[dict[str, str]] = []
        self.seen: set[tuple[str, ...]] = set()
        self.csv_handle: Any = None
        self.csv_writer: csv.DictWriter | None = None
        self.stop_requested = False
        self.level_announced = [False] * len(FIELD_NAMES)
        self.branch_counts = [0] * len(FIELD_NAMES)

    def run(self) -> None:
        self._load_existing_rows()
        self._open_csv()
        try:
            print("[1/4] 正在打开 Amazon 商品页……")
            self.driver.get(self.args.url)
            self._accept_cookie_banner()
            self._pause_for_captcha_if_needed()
            print("[2/4] 正在识别车型弹窗和五级控件……")
            popover = self._open_or_wait_for_popover()

            if self.args.inspect:
                self.inspect(popover)
                return

            print("[3/4] 已找到车型弹窗，开始五级嵌套遍历……")
            self._walk(level=0, path=[])
            self._write_json()
            counts = "，".join(
                f"第{i + 1}级 {count}" for i, count in enumerate(self.branch_counts)
            )
            print(f"[4/4] 完成：累计落盘 {len(self.rows)} 条（{counts}）")
            print(f"CSV：{self.output}")
        finally:
            if self.csv_handle:
                self.csv_handle.close()

    def _walk(self, level: int, path: list[str]) -> None:
        if self.stop_requested:
            return

        choices = self._get_choices(level)
        self.branch_counts[level] += len(choices)
        if not self.level_announced[level]:
            print(f"  已进入第 {level + 1}/5 级，首批可选项约 {len(choices)} 个")
            self.level_announced[level] = True

        for choice in choices:
            if self.stop_requested:
                return
            try:
                # 每次重新定位控件，避免上级选择后 Amazon 重绘 DOM 导致 stale。
                self._select_choice(level, choice)
                current_path = [*path, choice.text]

                if level == len(FIELD_NAMES) - 1:
                    self._save_row(current_path)
                    if self.args.max_rows and len(self.rows) >= self.args.max_rows:
                        self.stop_requested = True
                    continue

                self._wait_until_level_ready(level + 1)
                self._walk(level + 1, current_path)
            except KeyboardInterrupt:
                self._write_json()
                raise
            except Exception as exc:  # 单个车型分支失败时继续其余分支
                self._log_error(level, path, choice, exc)
                print("  ! 一个分支读取失败，已记录并继续", file=sys.stderr)

    def _get_choices(self, level: int) -> list[Choice]:
        control = self._find_control(level)
        tag = control.tag_name.lower()
        if tag == "select":
            raw = self.driver.execute_script(
                """
                return Array.from(arguments[0].options).map((o, i) => ({
                    text: (o.textContent || '').replace(/\\s+/g, ' ').trim(),
                    value: o.value || '', index: i,
                    disabled: !!o.disabled || !!o.parentElement?.disabled
                }));
                """,
                control,
            )
            return self._clean_choices(raw)

        self._safe_click(control)
        raw = self.wait.until(lambda _d: self._visible_custom_options())
        self.driver.find_element(By.TAG_NAME, "body").send_keys("\ue00c")  # Escape
        return self._clean_choices(raw)

    def _clean_choices(self, raw: list[dict[str, Any]]) -> list[Choice]:
        result: list[Choice] = []
        seen_text: set[str] = set()
        for item in raw:
            text = " ".join(str(item.get("text", "")).split())
            if (
                not text
                or item.get("disabled")
                or PLACEHOLDER_RE.search(text)
                or text.lower() in {"-", "none", "n/a"}
                or text in seen_text
            ):
                continue
            seen_text.add(text)
            result.append(
                Choice(
                    text=text,
                    value=str(item.get("value", "")),
                    index=int(item.get("index", -1)),
                )
            )
        return result

    def _select_choice(self, level: int, choice: Choice) -> None:
        control = self._find_control(level)
        if control.tag_name.lower() == "select":
            selected = self.driver.execute_script(
                """
                const s = arguments[0], wantedValue = arguments[1],
                      wantedText = arguments[2], wantedIndex = arguments[3];
                let option = Array.from(s.options).find(o =>
                    wantedValue && o.value === wantedValue);
                if (!option) option = Array.from(s.options).find(o =>
                    (o.textContent || '').replace(/\\s+/g, ' ').trim() === wantedText);
                if (!option && wantedIndex >= 0) option = s.options[wantedIndex];
                if (!option || option.disabled) return false;
                s.value = option.value;
                option.selected = true;
                s.dispatchEvent(new Event('input', {bubbles: true}));
                s.dispatchEvent(new Event('change', {bubbles: true}));
                return true;
                """,
                control,
                choice.value,
                choice.text,
                choice.index,
            )
            if not selected:
                raise RuntimeError(f"原生下拉框中找不到选项：{choice.text}")
        else:
            self._safe_click(control)
            option = self.wait.until(
                lambda _d: self._find_visible_custom_option(choice.text)
            )
            self._safe_click(option)

        time.sleep(self.args.delay)

    def _find_control(self, level: int) -> WebElement:
        popover = self._visible_popover()
        if popover is None:
            raise RuntimeError("车型弹窗已关闭或被页面重绘")

        native = self.driver.execute_script(
            """
            // 后四级在上级未选择时通常是 disabled，但仍需保留它们的位置。
            return Array.from(arguments[0].querySelectorAll('select'));
            """,
            popover,
        )
        if len(native) >= len(FIELD_NAMES):
            return native[level]

        hints = FIELD_HINTS[level]
        control = self.driver.execute_script(
            """
            const root = arguments[0], hints = arguments[1];
            const visible = e => {
                const r = e.getBoundingClientRect(), s = getComputedStyle(e);
                return r.width > 0 && r.height > 0 &&
                       s.visibility !== 'hidden' && s.display !== 'none';
            };
            const candidates = Array.from(root.querySelectorAll(
                '[role="combobox"],[aria-haspopup="listbox"],button,' +
                '.a-dropdown-container,.a-button-dropdown,input'
            )).filter(visible).filter(e => !e.disabled &&
                e.getAttribute('aria-disabled') !== 'true');
            let best = null, bestScore = 0;
            for (const e of candidates) {
                const hay = [e.innerText, e.textContent, e.value,
                    e.getAttribute('aria-label'), e.getAttribute('title'),
                    e.getAttribute('data-value')].filter(Boolean).join(' ').toLowerCase();
                let score = 0;
                for (const hint of hints) if (hay.includes(hint)) score += hint.length;
                if (score > bestScore) { best = e; bestScore = score; }
            }
            return best;
            """,
            popover,
            list(hints),
        )
        if control is None:
            raise RuntimeError(
                f"找不到第 {level + 1} 个控件（{FIELD_NAMES[level]}）；"
                "请运行 --inspect 查看 DOM"
            )
        return control

    def _wait_until_level_ready(self, level: int) -> None:
        def ready(_driver: webdriver.Chrome) -> bool:
            try:
                control = self._find_control(level)
                if not control.is_enabled():
                    return False
                if control.tag_name.lower() == "select":
                    return bool(self._get_choices(level))
                return control.get_attribute("aria-disabled") != "true"
            except (StaleElementReferenceException, JavascriptException, RuntimeError):
                return False

        self.wait.until(ready)

    def _visible_custom_options(self) -> list[dict[str, Any]]:
        popover = self._visible_popover()
        if popover is None:
            return []
        return self.driver.execute_script(
            """
            const main = arguments[0];
            const visible = e => {
                const r = e.getBoundingClientRect(), s = getComputedStyle(e);
                return r.width > 0 && r.height > 0 &&
                       s.visibility !== 'hidden' && s.display !== 'none';
            };
            const nodes = Array.from(document.querySelectorAll(
                '.a-popover[aria-hidden="false"] a.a-dropdown-link,' +
                '[role="option"],.a-dropdown-item,.a-select-option'
            )).filter(visible).filter(e => !main.contains(e));
            return nodes.map((e, i) => ({
                text: (e.innerText || e.textContent || '').replace(/\\s+/g, ' ').trim(),
                value: e.getAttribute('data-value') || e.getAttribute('data-id') || '',
                index: i,
                disabled: e.getAttribute('aria-disabled') === 'true' ||
                          e.classList.contains('a-button-disabled')
            })).filter(x => x.text);
            """,
            popover,
        )

    def _find_visible_custom_option(self, text: str) -> WebElement | None:
        popover = self._visible_popover()
        if popover is None:
            return None
        return self.driver.execute_script(
            """
            const main = arguments[0], wanted = arguments[1];
            const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
            const visible = e => {
                const r = e.getBoundingClientRect(), s = getComputedStyle(e);
                return r.width > 0 && r.height > 0 &&
                       s.visibility !== 'hidden' && s.display !== 'none';
            };
            const nodes = Array.from(document.querySelectorAll(
                '.a-popover[aria-hidden="false"] a.a-dropdown-link,' +
                '[role="option"],.a-dropdown-item,.a-select-option'
            ));
            return nodes.find(e => !main.contains(e) && visible(e) &&
                norm(e.innerText || e.textContent) === wanted) || null;
            """,
            popover,
            text,
        )

    def _open_or_wait_for_popover(self) -> WebElement:
        existing = self._visible_popover()
        if existing is not None:
            return existing

        # a-popover-N 的数字可能变化，因此同时尝试 aria-controls 和页面文字入口。
        trigger_selectors = (
            '[aria-controls="a-popover-2"]',
            '[data-action*="garage"]',
            '[data-action*="vehicle"]',
            '.a-declarative[data-a-popover*="vehicle"]',
            '.a-declarative[data-a-popover*="garage"]',
        )
        for selector in trigger_selectors:
            for element in self.driver.find_elements(By.CSS_SELECTOR, selector):
                try:
                    if element.is_displayed() and element.is_enabled():
                        self._safe_click(element)
                        found = self._wait_for_popover_briefly()
                        if found is not None:
                            return found
                except (StaleElementReferenceException, WebDriverException):
                    continue

        text_xpath = (
            "//*[self::a or self::button or @role='button']"
            "[contains(translate(normalize-space(.),"
            "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'vehicle') or "
            "contains(translate(normalize-space(.),"
            "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'garage') or "
            "contains(normalize-space(.),'Fahrzeug')]"
        )
        for element in self.driver.find_elements(By.XPATH, text_xpath):
            try:
                if element.is_displayed() and element.is_enabled():
                    self._safe_click(element)
                    found = self._wait_for_popover_briefly()
                    if found is not None:
                        return found
            except (StaleElementReferenceException, WebDriverException):
                continue

        if sys.stdin.isatty() and not self.args.headless:
            print(
                "未能自动打开车型弹窗。请在浏览器中手动打开截图所示弹窗，"
                "然后回到这里按 Enter。"
            )
            input()
            found = self._visible_popover()
            if found is not None:
                return found

        raise RuntimeError(
            "未找到可见车型弹窗。确认已打开 XPath //*[@id=\"a-popover-2\"]，"
            "或用 --inspect 检查页面。"
        )

    def _wait_for_popover_briefly(self) -> WebElement | None:
        end = time.time() + min(3.0, self.args.timeout)
        while time.time() < end:
            found = self._visible_popover()
            if found is not None:
                return found
            time.sleep(0.15)
        return None

    def _visible_popover(self) -> WebElement | None:
        candidates = self.driver.find_elements(By.XPATH, POPOVER_XPATH)
        # 如果 a-popover-2 编号变化，以弹窗内的车型文案作为后备识别。
        candidates.extend(
            self.driver.find_elements(
                By.XPATH,
                "//*[contains(@class,'a-popover') and "
                ".//*[contains(translate(normalize-space(.),"
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),"
                "'enter a new vehicle')]]",
            )
        )
        for element in candidates:
            try:
                if element.is_displayed():
                    return element
            except StaleElementReferenceException:
                continue
        return None

    def _safe_click(self, element: WebElement) -> None:
        try:
            element.click()
        except (ElementClickInterceptedException, WebDriverException):
            self.driver.execute_script(
                "arguments[0].scrollIntoView({block:'center'}); arguments[0].click();",
                element,
            )

    def _accept_cookie_banner(self) -> None:
        for selector in ("#sp-cc-accept", "#sp-cc-rejectall-link"):
            elements = self.driver.find_elements(By.CSS_SELECTOR, selector)
            if elements and elements[0].is_displayed():
                self._safe_click(elements[0])
                return

    def _pause_for_captcha_if_needed(self) -> None:
        source = self.driver.page_source.lower()
        if "captcha" not in source and "enter the characters you see" not in source:
            return
        if self.args.headless or not sys.stdin.isatty():
            raise RuntimeError("Amazon 显示了验证码；请改用可视模式后手动完成")
        print("Amazon 显示了验证码。请在浏览器中手动完成，然后按 Enter。")
        input()

    def inspect(self, popover: WebElement) -> None:
        info = self.driver.execute_script(
            """
            const root = arguments[0];
            return Array.from(root.querySelectorAll(
                'select,[role="combobox"],[aria-haspopup="listbox"],button,input,' +
                '.a-dropdown-container,.a-button-dropdown'
            )).map((e, i) => ({
                i, tag: e.tagName.toLowerCase(), id: e.id || '',
                name: e.getAttribute('name') || '',
                role: e.getAttribute('role') || '',
                ariaLabel: e.getAttribute('aria-label') || '',
                ariaDisabled: e.getAttribute('aria-disabled') || '',
                text: (e.innerText || e.value || e.textContent || '')
                    .replace(/\\s+/g, ' ').trim().slice(0, 160),
                optionCount: e.tagName === 'SELECT' ? e.options.length : null
            }));
            """,
            popover,
        )
        print(json.dumps(info, ensure_ascii=False, indent=2))
        for level, name in enumerate(FIELD_NAMES):
            try:
                choices = self._get_choices(level)
                print(f"\n{name}: {len(choices)} 项")
                print(json.dumps([c.__dict__ for c in choices[:20]], ensure_ascii=False, indent=2))
            except Exception as exc:
                print(f"\n{name}: 识别失败：{exc}")

    def _load_existing_rows(self) -> None:
        if not self.output.exists():
            return
        with self.output.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if not all(name in row for name in FIELD_NAMES):
                    raise RuntimeError(f"已有 CSV 表头不兼容：{self.output}")
                normalized = {name: row[name] for name in FIELD_NAMES}
                self.rows.append(normalized)
                self.seen.add(tuple(normalized[name] for name in FIELD_NAMES))
        print(f"断点续跑：已读取 {len(self.rows)} 条历史记录")

    def _open_csv(self) -> None:
        self.output.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.output.exists() or self.output.stat().st_size == 0
        self.csv_handle = self.output.open("a", encoding="utf-8-sig", newline="")
        self.csv_writer = csv.DictWriter(self.csv_handle, fieldnames=FIELD_NAMES)
        if new_file:
            self.csv_writer.writeheader()
            self.csv_handle.flush()

    def _save_row(self, path: list[str]) -> None:
        key = tuple(path)
        if key in self.seen:
            return
        row = dict(zip(FIELD_NAMES, path, strict=True))
        assert self.csv_writer is not None
        self.csv_writer.writerow(row)
        self.csv_handle.flush()
        self.rows.append(row)
        self.seen.add(key)
        if len(self.rows) % 100 == 0:
            print(f"  进度：已累计落盘 {len(self.rows)} 条", flush=True)
        if len(self.rows) % 50 == 0:
            self._write_json()

    def _write_json(self) -> None:
        target = self.output.with_suffix(".json")
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_text(
            json.dumps(self.rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(target)

    def _log_error(
        self, level: int, path: list[str], choice: Choice, exc: Exception
    ) -> None:
        message = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "level": level + 1,
            "path": [*path, choice.text],
            "error": f"{type(exc).__name__}: {exc}",
        }
        with self.error_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(message, ensure_ascii=False) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="遍历 Amazon.de 车型弹窗的五级联动下拉选项"
    )
    parser.add_argument("--url", default=START_URL, help="Amazon 商品网址")
    parser.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "output" / "amazon_de_vehicles.csv"),
        help="输出 CSV 路径",
    )
    parser.add_argument("--timeout", type=float, default=20, help="控件等待秒数")
    parser.add_argument("--delay", type=float, default=0.55, help="每次选择后的等待秒数")
    parser.add_argument("--headless", action="store_true", help="无界面运行")
    parser.add_argument("--inspect", action="store_true", help="只检查控件，不遍历")
    parser.add_argument(
        "--max-rows", type=int, default=0, help="测试时最多新增/保留多少行，0 表示不限"
    )
    parser.add_argument(
        "--profile-dir",
        default=str(PROJECT_ROOT / ".amazon_selenium_profile"),
        help="Chrome 独立用户数据目录",
    )
    parser.add_argument("--keep-open", action="store_true", help="完成后不自动关闭浏览器")
    return parser.parse_args()


def make_driver(args: argparse.Namespace) -> webdriver.Chrome:
    options = Options()
    options.add_argument(f"--user-data-dir={Path(args.profile_dir).resolve()}")
    options.add_argument("--lang=en-GB")
    options.add_argument("--window-size=1440,1000")
    if args.headless:
        options.add_argument("--headless=new")
    return webdriver.Chrome(options=options)


def main() -> int:
    args = parse_args()
    driver: webdriver.Chrome | None = None
    try:
        driver = make_driver(args)
        AmazonFitmentScraper(driver, args).run()
        return 0
    except KeyboardInterrupt:
        print("\n已中断；CSV 中已写入的记录会保留，下次可继续运行。")
        return 130
    except Exception as exc:
        print(f"错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if driver is not None and not args.keep_open:
            driver.quit()


if __name__ == "__main__":
    raise SystemExit(main())
