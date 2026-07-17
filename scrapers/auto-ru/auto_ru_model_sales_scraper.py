#!/usr/bin/env python3
"""Read Auto.ru model URLs and extract model summary and body-type sales data."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit, urlunsplit

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.ui import WebDriverWait


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = PROJECT_ROOT / "tsv" / "auto_ru_catalog_rank.tsv"
DEFAULT_OUTPUT = PROJECT_ROOT / "tsv" / "auto_ru_model_sales.tsv"

SECTION_XPATH = "/html/body/div[1]/div/div/div[5]/div/div[2]/div[1]/section"
GENERATIONS_ROOT_XPATH = (
    "/html/body/div[1]/div/div/div[5]/div/div[2]/div[1]/div/div[1]/div[2]/div"
)

FIELD_NAMES = (
    "Rank",
    "Model",
    "link_url",
    "section",
    "generation",
    "body_type",
    "sale_detail",
    "body_type_url",
)


def clean_text(value: object) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").split())


def canonical_url(url: str) -> str:
    parts = urlsplit(clean_text(url))
    path = parts.path or "/"
    if not path.endswith("/"):
        path += "/"
    return urlunsplit((parts.scheme or "https", parts.netloc, path, "", ""))


@dataclass(frozen=True)
class ModelInput:
    rank: str
    model: str
    link_url: str


def read_model_inputs(path: Path, url_column: str) -> list[ModelInput]:
    """Read a TSV/CSV table or a plain text file containing one URL per line."""
    if not path.exists():
        raise FileNotFoundError(f"输入文件不存在：{path}")

    lines = path.read_text(encoding="utf-8-sig").splitlines()
    nonempty = [line for line in lines if clean_text(line)]
    if not nonempty:
        return []

    delimiter = "\t" if "\t" in nonempty[0] else ","
    header = next(csv.reader([nonempty[0]], delimiter=delimiter))
    if url_column in header:
        rows: Iterable[dict[str, str]] = csv.DictReader(nonempty, delimiter=delimiter)
        result = []
        for row in rows:
            url = clean_text(row.get(url_column))
            if url:
                result.append(
                    ModelInput(
                        rank=clean_text(row.get("Rank") or row.get("rank")),
                        model=clean_text(row.get("Model") or row.get("model")),
                        link_url=canonical_url(url),
                    )
                )
        return deduplicate_inputs(result)

    result = [
        ModelInput(rank="", model="", link_url=canonical_url(line))
        for line in nonempty
        if clean_text(line).startswith(("http://", "https://"))
    ]
    return deduplicate_inputs(result)


def deduplicate_inputs(items: Iterable[ModelInput]) -> list[ModelInput]:
    result: list[ModelInput] = []
    seen: set[str] = set()
    for item in items:
        if item.link_url not in seen:
            result.append(item)
            seen.add(item.link_url)
    return result


class AutoRuModelSalesScraper:
    def __init__(
        self, driver: webdriver.Chrome, args: argparse.Namespace, models: list[ModelInput]
    ) -> None:
        self.driver = driver
        self.args = args
        self.models = models
        self.wait = WebDriverWait(driver, args.timeout)
        self.output = Path(args.output).resolve()
        self.checkpoint = Path(
            args.checkpoint or self.output.with_suffix(".checkpoint.json")
        ).resolve()
        self.error_log = self.output.with_suffix(".errors.log")
        self.completed_urls: set[str] = set()
        self.seen_rows: set[tuple[str, ...]] = set()
        self.total_rows = 0
        self.processed_models = 0
        self.tsv_handle: Any = None
        self.tsv_writer: csv.DictWriter | None = None

    def run(self) -> None:
        self._load_existing_rows()
        self._load_checkpoint()
        pending = [m for m in self.models if m.link_url not in self.completed_urls]
        if self.args.max_models:
            pending = pending[: self.args.max_models]

        print(
            f"输入车型：{len(self.models)}，已完成：{len(self.completed_urls)}，"
            f"本次待处理：{len(pending)}",
            flush=True,
        )
        self._open_tsv()
        try:
            for index, model in enumerate(pending, 1):
                try:
                    self._open_page(model.link_url, pause_after_load=(index == 1))
                    rows = self._extract_rows(model)
                    if not rows:
                        raise RuntimeError("指定的代际容器中未找到 body_type 和 sale_detail")
                    saved = sum(self._save_row(row) for row in rows)
                    self.completed_urls.add(model.link_url)
                    self.processed_models += 1
                    self._write_checkpoint()
                    print(
                        f"[{index}/{len(pending)}] {model.model or model.link_url}："
                        f"解析 {len(rows)} 行，新增 {saved} 行",
                        flush=True,
                    )
                except KeyboardInterrupt:
                    self._write_checkpoint()
                    raise
                except Exception as exc:
                    self._log_error(model.link_url, exc)
                    print(
                        f"[{index}/{len(pending)}] 失败并已记录：{model.link_url}：{exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                self._maybe_cooldown()
        finally:
            if self.tsv_handle:
                self.tsv_handle.close()

        print(f"完成：{self.output}（累计 {self.total_rows} 行）")

    def _open_page(self, url: str, pause_after_load: bool = False) -> None:
        last_error: Exception | None = None
        for attempt in range(1, self.args.retries + 2):
            try:
                self.driver.get(url)
                self.wait.until(
                    lambda d: d.execute_script("return document.readyState")
                    in {"interactive", "complete"}
                )
                if pause_after_load:
                    self._pause_after_initial_load()
                self._wait_for_challenge()
                if self.args.delay:
                    time.sleep(self.args.delay)
                self.wait.until(
                    lambda d: d.find_elements(By.XPATH, GENERATIONS_ROOT_XPATH)
                )
                return
            except (TimeoutException, WebDriverException) as exc:
                last_error = exc
                if attempt <= self.args.retries:
                    time.sleep(min(2**attempt, 8))
        raise RuntimeError(f"页面加载失败：{url}") from last_error

    def _pause_after_initial_load(self) -> None:
        if self.args.no_start_pause:
            return
        if self.args.headless or not sys.stdin.isatty():
            raise RuntimeError(
                "首次打开需要人工确认；请使用交互式终端，或传入 --no-start-pause"
            )
        input("Auto.ru 已打开。请完成必要的验证，然后按 Enter 开始抓取：")

    def _challenge_visible(self) -> bool:
        url = self.driver.current_url.casefold()
        title = self.driver.title.casefold()
        return "captcha" in url or "showcaptcha" in url or "ой!" in title

    def _wait_for_challenge(self) -> None:
        if not self._challenge_visible():
            return
        if self.args.headless or not sys.stdin.isatty():
            raise RuntimeError("遇到 Auto.ru 验证页，需要在可见浏览器中手工完成")
        input("检测到验证页。请在 Chrome 中完成验证并返回车型页，然后按 Enter：")
        if self._challenge_visible():
            raise RuntimeError("验证页仍然存在")

    def _extract_rows(self, model: ModelInput) -> list[dict[str, str]]:
        section_nodes = self.driver.find_elements(By.XPATH, SECTION_XPATH)
        section = clean_text(section_nodes[0].text) if section_nodes else ""
        model_name = model.model or self._model_name(section_nodes)

        roots = self.driver.find_elements(By.XPATH, GENERATIONS_ROOT_XPATH)
        if not roots:
            return []

        result: list[dict[str, str]] = []
        for generation_node in roots[0].find_elements(By.XPATH, "./div"):
            body_rows = generation_node.find_elements(By.XPATH, "./div[3]/div")
            if not body_rows:
                body_rows = generation_node.find_elements(
                    By.XPATH, ".//div[./div/div/a and ./div/div/span]"
                )
            generation = self._generation_text(generation_node, body_rows)
            for body_row in body_rows:
                links = body_row.find_elements(By.XPATH, "./div/div/a")
                details = body_row.find_elements(By.XPATH, "./div/div/span")
                if not links:
                    continue
                body_type = clean_text(links[0].text)
                sale_detail = clean_text(details[0].text) if details else ""
                if not body_type:
                    continue
                body_url = clean_text(links[0].get_attribute("href"))
                result.append(
                    {
                        "Rank": model.rank,
                        "Model": model_name,
                        "link_url": model.link_url,
                        "section": section,
                        "generation": generation,
                        "body_type": body_type,
                        "sale_detail": sale_detail,
                        "body_type_url": urljoin(self.driver.current_url, body_url),
                    }
                )
        return result

    @staticmethod
    def _model_name(section_nodes: list[WebElement]) -> str:
        if not section_nodes:
            return ""
        headings = section_nodes[0].find_elements(By.XPATH, ".//h1")
        return clean_text(headings[0].text) if headings else ""

    def _generation_text(
        self, generation_node: WebElement, body_rows: list[WebElement]
    ) -> str:
        headings = generation_node.find_elements(By.XPATH, "./div[2]")
        if headings:
            return clean_text(headings[0].text)

        full_text = clean_text(generation_node.text)
        body_text = " ".join(clean_text(row.text) for row in body_rows)
        if body_text and full_text.endswith(body_text):
            full_text = full_text[: -len(body_text)].strip()
        return full_text

    def _load_existing_rows(self) -> None:
        if not self.output.exists() or self.output.stat().st_size == 0:
            return
        with self.output.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            if tuple(reader.fieldnames or ()) != FIELD_NAMES:
                raise RuntimeError(f"已有 TSV 表头不兼容：{self.output}")
            for row in reader:
                self.seen_rows.add(tuple(row.get(name, "") for name in FIELD_NAMES))
                self.total_rows += 1

    def _load_checkpoint(self) -> None:
        if not self.checkpoint.exists():
            return
        data = json.loads(self.checkpoint.read_text(encoding="utf-8"))
        self.completed_urls = {
            canonical_url(str(url)) for url in data.get("completed_urls", [])
        }

    def _open_tsv(self) -> None:
        self.output.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.output.exists() or self.output.stat().st_size == 0
        self.tsv_handle = self.output.open("a", encoding="utf-8-sig", newline="")
        self.tsv_writer = csv.DictWriter(
            self.tsv_handle, fieldnames=FIELD_NAMES, delimiter="\t", lineterminator="\n"
        )
        if new_file:
            self.tsv_writer.writeheader()
            self.tsv_handle.flush()

    def _save_row(self, row: dict[str, str]) -> int:
        key = tuple(row[name] for name in FIELD_NAMES)
        if key in self.seen_rows:
            return 0
        assert self.tsv_writer is not None and self.tsv_handle is not None
        self.tsv_writer.writerow(row)
        self.tsv_handle.flush()
        self.seen_rows.add(key)
        self.total_rows += 1
        return 1

    def _write_checkpoint(self) -> None:
        self.checkpoint.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.checkpoint.with_suffix(self.checkpoint.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "completed_urls": sorted(self.completed_urls),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(self.checkpoint)

    def _log_error(self, url: str, exc: Exception) -> None:
        self.error_log.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "url": url,
            "error": f"{type(exc).__name__}: {exc}",
        }
        with self.error_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _maybe_cooldown(self) -> None:
        if (
            self.args.cooldown_every
            and self.args.cooldown_seconds
            and self.processed_models
            and self.processed_models % self.args.cooldown_every == 0
        ):
            print(f"已完成 {self.processed_models} 个车型，冷却 {self.args.cooldown_seconds:g} 秒")
            time.sleep(self.args.cooldown_seconds)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="从 Auto.ru 车型 URL 列表提取车型概览、代际、车身类型和在售数量"
    )
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="输入 TSV、CSV 或纯 URL 文件")
    parser.add_argument("--url-column", default="link_url", help="输入表中的 URL 列名")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="输出 TSV 路径")
    parser.add_argument("--checkpoint", help="checkpoint 路径，默认与输出 TSV 同目录")
    parser.add_argument("--max-models", type=int, default=0, help="本次最多处理车型数；0 表示不限")
    parser.add_argument("--timeout", type=float, default=25, help="页面等待秒数")
    parser.add_argument("--delay", type=float, default=1.0, help="每页加载后的等待秒数")
    parser.add_argument("--cooldown-every", type=int, default=25, help="每处理多少车型进行冷却；0 表示关闭")
    parser.add_argument("--cooldown-seconds", type=float, default=30.0, help="每次冷却秒数")
    parser.add_argument("--retries", type=int, default=2, help="页面加载重试次数")
    parser.add_argument("--headless", action="store_true", help="无界面运行")
    parser.add_argument("--no-start-pause", action="store_true", help="首次打开后不等待人工按 Enter")
    parser.add_argument(
        "--profile-dir",
        default=str(PROJECT_ROOT / ".auto_ru_selenium_profile"),
        help="Chrome 用户数据目录",
    )
    parser.add_argument("--keep-open", action="store_true", help="运行后保留浏览器")
    args = parser.parse_args()
    if args.max_models < 0 or args.delay < 0 or args.retries < 0:
        parser.error("--max-models、--delay 和 --retries 不能小于 0")
    if args.timeout <= 0 or args.cooldown_every < 0 or args.cooldown_seconds < 0:
        parser.error("--timeout 必须大于 0；冷却参数不能小于 0")
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
        models = read_model_inputs(Path(args.input).resolve(), args.url_column)
        if not models:
            raise RuntimeError("输入文件中没有可用的车型 URL")
        driver = make_driver(args)
        AutoRuModelSalesScraper(driver, args, models).run()
        return 0
    except KeyboardInterrupt:
        print("\n已中断；已写入的 TSV 和 checkpoint 会保留。")
        return 130
    except Exception as exc:
        print(f"错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if driver is not None and not args.keep_open:
            driver.quit()


if __name__ == "__main__":
    raise SystemExit(main())
