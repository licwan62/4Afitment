#!/usr/bin/env python3
"""按目录展示顺序抓取 Auto.ru 车型卡片排行。"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.ui import WebDriverWait


PROJECT_ROOT = Path(__file__).resolve().parent
START_URL = "https://auto.ru/catalog/cars/"
CARD_ROOT_XPATH = "/html/body/div[1]/div/div/div[5]/div[2]/div[2]/div[2]/div[2]/div[2]"
FIELD_NAMES = ("Rank", "Model", "Sale", "Price", "link_url", "page_url", "image_url")
CSS_URL_RE = re.compile(r"url\([\"']?(.*?)[\"']?\)", re.IGNORECASE)


def clean_text(value: object) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").split())


def page_url(base_url: str, page: int) -> str:
    parts = urlsplit(base_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["page"] = str(page)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


class CatalogRankScraper:
    def __init__(self, driver: webdriver.Chrome, args: argparse.Namespace) -> None:
        self.driver = driver
        self.args = args
        self.wait = WebDriverWait(driver, args.timeout)

    def run(self) -> None:
        output = Path(self.args.output).resolve()
        checkpoint = Path(
            self.args.checkpoint or output.with_suffix(".checkpoint.json")
        ).resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        checkpoint.parent.mkdir(parents=True, exist_ok=True)
        if self.args.restart:
            self._backup_for_restart(output)
            self._backup_for_restart(checkpoint)

        state = self._load_state(output, checkpoint)
        if state["completed"] and state.get("total_pages"):
            print(f"checkpoint 显示任务已完成：{output}（{state['rank']} 条）")
            return
        if state["completed"]:
            state["completed"] = False
            print(
                "旧 checkpoint 没有总页数却被标记为完成；"
                f"将从第 {state['next_page']} 页重新识别并继续。",
                flush=True,
            )

        rank = int(state["rank"])
        first_page = int(state["next_page"])
        total_pages = int(state["total_pages"]) if state.get("total_pages") else None
        previous_signature = tuple(state.get("last_signature", [])) or None
        skipped_pages = [int(value) for value in state.get("skipped_pages", [])]
        new_file = not output.exists()

        with output.open("a", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=FIELD_NAMES, delimiter="\t")
            if new_file:
                writer.writeheader()
                handle.flush()

            last_allowed_page = first_page + self.args.max_pages - 1
            page = first_page
            while page <= last_allowed_page:
                url = page_url(self.args.url, page)
                self._open_page(url, pause_after_load=(page == first_page))
                if total_pages is None:
                    total_pages = self._detect_total_pages(page)
                    print(f"识别到目录总页数：{total_pages} 页。", flush=True)
                    self._write_checkpoint(
                        checkpoint,
                        page,
                        rank,
                        previous_signature,
                        total_pages,
                        skipped_pages,
                        completed=False,
                    )

                cards = self._wait_for_cards(page, url)
                if cards is None:
                    if page not in skipped_pages:
                        skipped_pages.append(page)
                    completed = page >= total_pages
                    self._write_checkpoint(
                        checkpoint,
                        page + 1,
                        rank,
                        previous_signature,
                        total_pages,
                        skipped_pages,
                        completed=completed,
                    )
                    print(f"第 {page}/{total_pages} 页已跳过。", flush=True)
                    if completed:
                        self._print_completion(total_pages, skipped_pages)
                        break
                    page += 1
                    continue

                signature = tuple(card.get_attribute("href") or card.text for card in cards)
                while signature == previous_signature:
                    cards = self._ask_retry_or_skip(page, url, "内容与上一页重复")
                    if cards is None:
                        break
                    signature = tuple(card.get_attribute("href") or card.text for card in cards)
                if cards is None:
                    if page not in skipped_pages:
                        skipped_pages.append(page)
                    completed = page >= total_pages
                    self._write_checkpoint(
                        checkpoint,
                        page + 1,
                        rank,
                        previous_signature,
                        total_pages,
                        skipped_pages,
                        completed=completed,
                    )
                    print(f"第 {page}/{total_pages} 页已跳过。", flush=True)
                    if completed:
                        self._print_completion(total_pages, skipped_pages)
                        break
                    page += 1
                    continue

                # 整页解析成功后再统一写入，避免中断时留下半页并在恢复后重复。
                rows = [
                    self._extract_card(card, rank + index, url)
                    for index, card in enumerate(cards, 1)
                ]
                writer.writerows(rows)
                handle.flush()
                rank += len(rows)
                previous_signature = signature
                completed = page >= total_pages
                self._write_checkpoint(
                    checkpoint,
                    page + 1,
                    rank,
                    previous_signature,
                    total_pages,
                    skipped_pages,
                    completed=completed,
                )
                print(
                    f"第 {page}/{total_pages} 页：写入 {len(cards)} 条，累计 {rank} 条。",
                    flush=True,
                )
                if completed:
                    self._print_completion(total_pages, skipped_pages)
                    break
                page += 1
            if page > last_allowed_page and page <= total_pages:
                print(f"已达到 --max-pages={self.args.max_pages}。")

        print(f"完成：{output}（{rank} 条）")

    @staticmethod
    def _backup_for_restart(path: Path) -> None:
        if not path.exists():
            return
        candidate = path.with_name(path.name + ".before_restart")
        suffix = 1
        while candidate.exists():
            candidate = path.with_name(path.name + f".before_restart.{suffix}")
            suffix += 1
        path.replace(candidate)
        print(f"已备份旧文件：{candidate}")

    @staticmethod
    def _print_completion(total_pages: int, skipped_pages: list[int]) -> None:
        message = f"全部 {total_pages} 个 page_url 已遍历完成。"
        if skipped_pages:
            message += " 已按选择跳过页面：" + ", ".join(map(str, skipped_pages))
        print(message, flush=True)

    def _load_state(self, output: Path, checkpoint: Path) -> dict[str, object]:
        if not checkpoint.exists():
            if output.exists():
                raise RuntimeError(
                    f"输出已存在但缺少 checkpoint：{output}；"
                    "如需从头覆盖，请传入 --restart"
                )
            return {
                "next_page": self.args.start_page,
                "rank": 0,
                "last_signature": [],
                "total_pages": self.args.total_pages,
                "skipped_pages": [],
                "completed": False,
            }

        with checkpoint.open("r", encoding="utf-8") as handle:
            state = json.load(handle)
        if state.get("url") != self.args.url or state.get("fields") != list(FIELD_NAMES):
            raise RuntimeError("checkpoint 与当前 URL 或字段结构不匹配；请检查参数或使用 --restart")
        if not output.exists():
            raise RuntimeError("checkpoint 存在但 TSV 不存在；请使用 --restart 从头开始")
        with output.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle, delimiter="\t")
            header = next(reader, [])
            rows = list(reader)
        if tuple(header) != FIELD_NAMES:
            raise RuntimeError("TSV 表头与当前字段结构不匹配；请使用 --restart 从头开始")
        committed_rank = int(state["rank"])
        if len(rows) < committed_rank:
            raise RuntimeError("TSV 行数少于 checkpoint 记录，无法安全恢复；请检查文件")
        if len(rows) > committed_rank:
            # TSV 刷盘成功但 checkpoint 尚未替换时可能发生；丢弃未提交的尾部整页。
            with output.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle, delimiter="\t")
                writer.writerow(FIELD_NAMES)
                writer.writerows(rows[:committed_rank])
            print(f"已丢弃 {len(rows) - committed_rank} 条未提交的 TSV 尾部记录。")
        print(
            f"从 checkpoint 恢复：第 {state['next_page']} 页，下一条 Rank={int(state['rank']) + 1}",
            flush=True,
        )
        return state

    def _write_checkpoint(
        self,
        checkpoint: Path,
        next_page: int,
        rank: int,
        signature: tuple[str, ...] | None,
        total_pages: int,
        skipped_pages: list[int],
        *,
        completed: bool,
    ) -> None:
        state = {
            "url": self.args.url,
            "fields": list(FIELD_NAMES),
            "next_page": next_page,
            "rank": rank,
            "last_signature": list(signature or ()),
            "total_pages": total_pages,
            "skipped_pages": skipped_pages,
            "completed": completed,
        }
        temporary = checkpoint.with_suffix(checkpoint.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        temporary.replace(checkpoint)

    def _open_page(self, url: str, pause_after_load: bool) -> None:
        last_error: Exception | None = None
        for attempt in range(self.args.retries + 1):
            try:
                self.driver.get(url)
                self.wait.until(
                    lambda d: d.execute_script("return document.readyState")
                    in {"interactive", "complete"}
                )
                if pause_after_load and not self.args.no_start_pause:
                    if self.args.headless or not sys.stdin.isatty():
                        raise RuntimeError(
                            "首次打开需要人工确认；请使用可交互终端，或传入 --no-start-pause"
                        )
                    input("Auto.ru 已打开。请完成必要的验证，然后按 Enter 继续：")
                self._handle_robot_challenge()
                if self.args.delay:
                    time.sleep(self.args.delay)
                return
            except (TimeoutException, WebDriverException) as exc:
                last_error = exc
                if attempt < self.args.retries:
                    time.sleep(min(2 ** (attempt + 1), 8))
        raise RuntimeError(f"页面加载失败：{url}") from last_error

    def _robot_challenge_visible(self) -> bool:
        current_url = self.driver.current_url.casefold()
        title = self.driver.title.casefold()
        if "captcha" in current_url or "showcaptcha" in current_url or "captcha" in title:
            return True
        return bool(
            self.driver.execute_script(
                """
                const text = (document.body?.innerText || '').toLowerCase();
                return !!document.querySelector(
                    'form[action*="captcha"], iframe[src*="captcha"], ' +
                    '[class*="Captcha"], [class*="captcha"], [data-testid*="captcha"]'
                ) || text.includes('я не робот') ||
                    text.includes('подтвердите, что запросы отправляли вы') ||
                    text.includes('подтвердите, что вы не робот');
                """
            )
        )

    def _handle_robot_challenge(self) -> None:
        if not self._robot_challenge_visible():
            return
        if self.args.headless or not sys.stdin.isatty():
            raise RuntimeError(
                "检测到机器人验证；本页未写入，checkpoint 已保留。"
                "请在可交互的非 headless 模式下重新运行并手动完成验证"
            )
        input("检测到机器人验证。请在 Chrome 中手动完成，然后按 Enter 继续：")
        if self._robot_challenge_visible():
            raise RuntimeError("机器人验证仍然存在；本页未写入，checkpoint 已保留")

    def _cards(self) -> list[WebElement]:
        roots = self.driver.find_elements(By.XPATH, CARD_ROOT_XPATH)
        if not roots:
            return []
        return roots[0].find_elements(By.XPATH, "./a")

    def _detect_total_pages(self, current_page: int) -> int:
        if self.args.total_pages:
            return self.args.total_pages

        page_numbers = {current_page}
        for anchor in self.driver.find_elements(By.CSS_SELECTOR, "a[href]"):
            href = anchor.get_attribute("href") or ""
            query = dict(parse_qsl(urlsplit(href).query, keep_blank_values=True))
            if str(query.get("page", "")).isdigit():
                page_numbers.add(int(query["page"]))

        # 某些分页链接只存在于 hydration 数据或尚未显示的 DOM 中。
        for match in re.finditer(
            r"(?:[?&]|&amp;)page(?:=|%3D)(\d+)",
            self.driver.page_source,
            re.IGNORECASE,
        ):
            page_numbers.add(int(match.group(1)))

        total_pages = max(page_numbers)
        if total_pages <= current_page:
            raise RuntimeError(
                f"未能在第 {current_page} 页识别总页数；checkpoint 未推进。"
                "请检查分页区域，或用 --total-pages 明确指定"
            )
        return total_pages

    def _wait_for_cards(self, page: int, url: str) -> list[WebElement] | None:
        cards = self._cards()
        if cards:
            return cards
        return self._ask_retry_or_skip(page, url, "没有找到卡片")

    def _ask_retry_or_skip(
        self, page: int, url: str, reason: str
    ) -> list[WebElement] | None:
        while True:
            message = (
                f"第 {page} 页{reason}，checkpoint 仍停留在本页。\n"
                "输入 r 重试当前页，或输入 s 跳过该页："
            )
            if self.args.headless or not sys.stdin.isatty():
                raise RuntimeError(message)
            choice = input(message).strip().casefold()
            if choice in {"s", "skip"}:
                return None
            if choice not in {"r", "retry", ""}:
                print("请输入 r 或 s。", flush=True)
                continue
            self._open_page(url, pause_after_load=False)
            cards = self._cards()
            if cards:
                return cards

    def _extract_card(
        self, card: WebElement, rank: int, source_page_url: str
    ) -> dict[str, object]:
        image_node = self._child(card, "./div[1]")
        href = clean_text(card.get_attribute("href"))
        return {
            "Rank": rank,
            "Model": self._text(card, "./div[2]"),
            "Sale": self._text(card, "./span[1]"),
            "Price": self._text(card, "./span[2]"),
            "link_url": urljoin(self.driver.current_url, href) if href else "",
            "page_url": source_page_url,
            "image_url": self._image_url(image_node),
        }

    @staticmethod
    def _child(card: WebElement, xpath: str) -> WebElement | None:
        nodes = card.find_elements(By.XPATH, xpath)
        return nodes[0] if nodes else None

    def _text(self, card: WebElement, xpath: str) -> str:
        node = self._child(card, xpath)
        return clean_text(node.text if node else "")

    def _image_url(self, node: WebElement | None) -> str:
        if node is None:
            return ""
        candidates = [node, *node.find_elements(By.TAG_NAME, "img")]
        for candidate in candidates:
            for attribute in ("src", "data-src", "data-lazy-src", "data-original"):
                value = clean_text(candidate.get_attribute(attribute))
                if value and not value.startswith("data:"):
                    return urljoin(self.driver.current_url, value)
        style = node.value_of_css_property("background-image") or node.get_attribute("style")
        match = CSS_URL_RE.search(style or "")
        return urljoin(self.driver.current_url, match.group(1)) if match else ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按分页顺序抓取 Auto.ru 目录卡片排行")
    parser.add_argument("--url", default=START_URL, help="目录起始网址")
    parser.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "tsv" / "auto_ru_catalog_rank.tsv"),
        help="输出 TSV 路径",
    )
    parser.add_argument("--checkpoint", help="checkpoint 路径，默认与 TSV 同目录")
    parser.add_argument(
        "--restart", action="store_true", help="备份既有输出和 checkpoint 后从头重抓"
    )
    parser.add_argument("--start-page", type=int, default=1, help="起始页码")
    parser.add_argument(
        "--total-pages", type=int, help="手动指定总页数；默认从首次加载的分页区域识别"
    )
    parser.add_argument("--max-pages", type=int, default=1000, help="最多访问的页数")
    parser.add_argument("--timeout", type=float, default=25, help="页面加载超时秒数")
    parser.add_argument("--delay", type=float, default=1, help="每页加载后的等待秒数")
    parser.add_argument("--retries", type=int, default=2, help="页面加载重试次数")
    parser.add_argument("--headless", action="store_true", help="无界面模式")
    parser.add_argument("--no-start-pause", action="store_true", help="首次打开后不暂停")
    parser.add_argument(
        "--profile-dir",
        default=str(PROJECT_ROOT / ".auto_ru_selenium_profile"),
        help="Chrome 用户数据目录",
    )
    parser.add_argument("--keep-open", action="store_true", help="运行后保留浏览器")
    args = parser.parse_args()
    if args.start_page < 1 or args.max_pages < 1:
        parser.error("--start-page 和 --max-pages 必须大于 0")
    if args.total_pages is not None and args.total_pages < args.start_page:
        parser.error("--total-pages 不能小于 --start-page")
    if args.timeout <= 0 or args.delay < 0 or args.retries < 0:
        parser.error("timeout 必须大于 0；delay 和 retries 不能小于 0")
    return args


def make_driver(args: argparse.Namespace) -> webdriver.Chrome:
    options = Options()
    options.page_load_strategy = "eager"
    options.add_argument(f"--user-data-dir={Path(args.profile_dir).resolve()}")
    options.add_argument("--lang=ru-RU")
    options.add_argument("--window-size=1440,1000")
    if args.headless:
        options.add_argument("--headless=new")
    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(args.timeout)
    return driver


def main() -> int:
    args = parse_args()
    driver: webdriver.Chrome | None = None
    try:
        driver = make_driver(args)
        CatalogRankScraper(driver, args).run()
        return 0
    except KeyboardInterrupt:
        print("\n已中断；已完成页面的数据保留在 TSV 中。")
        return 130
    except Exception as exc:
        print(f"错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if driver is not None and not args.keep_open:
            driver.quit()


if __name__ == "__main__":
    raise SystemExit(main())
