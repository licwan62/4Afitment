#!/usr/bin/env python3
"""Extract Auto.ru generation/type sales counts from model pages."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.ui import WebDriverWait

SCRAPERS_ROOT = Path(__file__).resolve().parents[1]
if str(SCRAPERS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRAPERS_ROOT))

from common.project_io import (  # noqa: E402
    append_json_log,
    apply_known_defaults,
    load_yaml_config,
    output_file,
    read_table_records,
)


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_INPUT = PROJECT_ROOT / "output" / "auto_ru_catalog_rank.csv"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "output"
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "auto_ru.yaml"
DEFAULT_LOG = PROJECT_ROOT / "log" / "auto_ru_model_sales.log"

SECTION_XPATH = "/html/body/div[1]/div/div/div[5]/div/div[2]/div[1]/section"
GENERATIONS_ROOT_XPATH = (
    "//div[@data-seo='generation-list']"
)
GENERATION_ITEMS_XPATH = (
    ".//div[contains(@class, 'CatalogGenerationsList__listItem-')]"
)
CONFIGURATIONS_LIST_XPATH = (
    ".//div[contains(@class, 'CatalogGenerationsListItem__configurationsList-')]"
)
TYPE_BADGE_XPATH = (
    ".//div[contains(@class, 'CatalogGenerationsListItem__badges-')]"
    "//*[contains(@class, 'Badge2-') "
    "and contains(@class, 'Badge2_type_primary-') "
    "and contains(@class, 'Badge2_color_transparent-')]"
)
GENERATION_TITLE_XPATH = (
    ".//*[contains(@class, 'CatalogGenerationsListItem__title-')]"
)
SCHEMA_VERSION = 13

FIELD_NAMES = (
    "Model",
    "link_url",
    "generation",
    "Years",
    "type",
    "sale_detail",
)
SALES_WITH_RANK_FIELD_NAMES = ("Rank", *FIELD_NAMES)
SALES_WITHOUT_YEARS_FIELD_NAMES = (
    "Rank",
    "Model",
    "link_url",
    "generation",
    "type",
    "sale_detail",
)
IMAGE_TYPE_FIELD_NAMES = (
    "Rank",
    "Model",
    "link_url",
    "generation",
    "type",
    "sale_detail",
    "body_type_url",
    "main_image_url",
    "side_image_url",
    "front_image_url",
    "back_image_url",
    "3_4_behind_image_url",
    "remark",
)
BODY_TYPE_FIELD_NAMES = (
    "Rank",
    "Model",
    "link_url",
    "generation",
    "body_type",
    "sale_detail",
    "body_type_url",
    "main_image_url",
    "side_image_url",
    "front_image_url",
    "back_image_url",
    "3_4_behind_image_url",
    "remark",
)
NO_BODY_TYPE_FIELD_NAMES = (
    "Rank",
    "Model",
    "link_url",
    "generation",
    "sale_detail",
    "body_type_url",
    "main_image_url",
    "side_image_url",
    "front_image_url",
    "back_image_url",
    "3_4_behind_image_url",
    "remark",
)
LEGACY_FIELD_NAMES = (
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


def numeric_sale_detail(value: object) -> str:
    """Return a traceable numeric sale count; missing/non-numeric means zero."""
    digits = re.sub(r"\D", "", clean_text(value))
    return digits or "0"


def canonical_url(url: str) -> str:
    parts = urlsplit(clean_text(url))
    path = parts.path or "/"
    if not path.endswith("/"):
        path += "/"
    return urlunsplit((parts.scheme or "https", parts.netloc, path, "", ""))


def generation_and_years(generation_node: WebElement) -> tuple[str, str]:
    """Extract generation descriptor and normalized years from the title spans."""
    title_nodes = generation_node.find_elements(By.XPATH, GENERATION_TITLE_XPATH)
    if not title_nodes:
        return "", ""

    title_node = title_nodes[0]
    spans = title_node.find_elements(By.XPATH, "self::span | .//span")
    span_texts = []
    for span in spans:
        span_text = clean_text(
            span.get_attribute("textContent") or span.text
        )
        if span_text:
            span_texts.append(span_text)
    full_text = clean_text(
        title_node.get_attribute("textContent")
        or title_node.text
        or " ".join(span_texts)
    )

    year_match = re.search(
        r"(?i)(?:[сc]\s*)?(?:19|20)\d{2}"
        r"(?:\s*[-–—]\s*(?:(?:19|20)\d{2}|н\.?\s*в\.?))?"
        r"\s*(?:года?|гг\.?)?",
        full_text,
    )
    years = clean_text(year_match.group(0)) if year_match else ""

    generation_parts = [text for text in span_texts if not re.search(r"\d{4}", text)]
    generation = clean_text(" ".join(generation_parts))
    if not generation:
        generation = (
            full_text[: year_match.start()] + " " + full_text[year_match.end() :]
            if year_match
            else full_text
        )
        generation = clean_text(generation)
    return generation, years


@dataclass(frozen=True)
class ModelInput:
    rank: str
    model: str
    link_url: str


def read_model_inputs(
    path: Path, url_column: str, sheetname: str | None = None
) -> list[ModelInput]:
    """Read model URLs from TSV, CSV, XLSX, or a directory containing one."""
    _resolved, rows = read_table_records(path, sheetname)
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
        self.output = output_file(args.output, "auto_ru_model_sales.csv")
        self.checkpoint = Path(
            args.checkpoint or self.output.with_suffix(".checkpoint.json")
        ).resolve()
        self.log_path = Path(args.log).resolve()
        self.completed_urls: set[str] = set()
        self.seen_rows: set[tuple[str, ...]] = set()
        self.total_rows = 0
        self.processed_models = 0
        self.tsv_handle: Any = None
        self.tsv_writer: csv.DictWriter | None = None

    def run(self) -> None:
        self._prepare_output_schema()
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
        self._log_event(
            "info",
            "run_started",
            "",
            {
                "input_models": len(self.models),
                "completed_models": len(self.completed_urls),
                "pending_models": len(pending),
            },
        )
        self._open_tsv()
        try:
            for index, model in enumerate(pending, 1):
                try:
                    self._open_page(
                        model.link_url,
                        pause_after_load=(index == 1),
                    )
                    rows = self._extract_rows(model)
                    if not rows:
                        self._log_event(
                            "warning",
                            "no_sales_rows",
                            model.link_url,
                            "车型页没有可输出的 generation/Years 数据；按正常完成处理",
                        )
                    saved = sum(self._save_row(row) for row in rows)
                    self.completed_urls.add(model.link_url)
                    self.processed_models += 1
                    self._write_checkpoint()
                    print(
                        f"[{index}/{len(pending)}] {model.model or model.link_url}："
                        f"解析 {len(rows)} 行，新增 {saved} 行",
                        flush=True,
                    )
                    self._log_event(
                        "info",
                        "model_completed",
                        model.link_url,
                        {"parsed_rows": len(rows), "saved_rows": saved},
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

    def _open_page(
        self,
        url: str,
        pause_after_load: bool = False,
        required_xpath: str | None = None,
    ) -> None:
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
                if required_xpath:
                    self.wait.until(
                        lambda d: d.find_elements(By.XPATH, required_xpath)
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
        model_name = model.model or self._model_name(section_nodes)

        roots = self.driver.find_elements(By.XPATH, GENERATIONS_ROOT_XPATH)
        if not roots:
            self._log_event(
                "warning",
                "generation_list_missing",
                model.link_url,
                f"未找到 {GENERATIONS_ROOT_XPATH}",
            )
            return []

        result: list[dict[str, str]] = []
        seen_generation_body_types: set[tuple[str, str, str]] = set()
        generation_nodes = roots[0].find_elements(
            By.XPATH, GENERATION_ITEMS_XPATH
        )
        if not generation_nodes:
            self._log_event(
                "warning",
                "generation_items_missing",
                model.link_url,
                f"未找到 {GENERATION_ITEMS_XPATH}",
            )
        for generation_node in generation_nodes:
            generation, years = generation_and_years(generation_node)
            if not generation and not years:
                self._log_event(
                    "warning",
                    "generation_title_missing",
                    model.link_url,
                    f"未找到 {GENERATION_TITLE_XPATH} 或其中 span 没有可解析文本",
                )
                continue
            elif not years:
                title_nodes = generation_node.find_elements(
                    By.XPATH, GENERATION_TITLE_XPATH
                )
                raw_title = (
                    clean_text(
                        title_nodes[0].get_attribute("textContent")
                        or title_nodes[0].text
                    )
                    if title_nodes
                    else ""
                )
                self._log_event(
                    "warning",
                    "years_missing",
                    model.link_url,
                    {"generation": generation, "raw_title": raw_title},
                )
            details = self._configuration_details(generation_node)
            if not details:
                badge_types = self._generation_types_from_badges(generation_node)
                if not badge_types:
                    badge_types = [""]
                self._log_event(
                    "warning",
                    "configuration_links_missing",
                    model.link_url,
                    {
                        "generation": generation,
                        "Years": years,
                        "badge_types": badge_types,
                        "sale_detail": "0",
                    },
                )
                details = [(badge_type, "0") for badge_type in badge_types]
            for body_type, sale_detail in details:
                unique_key = (
                    generation.casefold(),
                    years,
                    body_type.casefold(),
                )
                if unique_key in seen_generation_body_types:
                    continue
                seen_generation_body_types.add(unique_key)
                result.append(
                    {
                        "Model": model_name,
                        "link_url": model.link_url,
                        "generation": generation,
                        "Years": years,
                        "type": body_type,
                        "sale_detail": sale_detail,
                    }
                )
        return result

    def _configuration_details(
        self, generation_node: WebElement
    ) -> list[tuple[str, str]]:
        containers = generation_node.find_elements(
            By.XPATH, CONFIGURATIONS_LIST_XPATH
        )
        if not containers:
            return []

        entries: list[tuple[str, str]] = []
        for child_index, child in enumerate(
            containers[0].find_elements(By.XPATH, "./div"), 1
        ):
            links = child.find_elements(By.XPATH, ".//a")
            name = clean_text(links[0].text) if links else ""
            count = self._sale_detail_from(child)
            if not links:
                self._log_event(
                    "warning",
                    "type_link_missing",
                    self.driver.current_url,
                    {
                        "configuration_index": child_index,
                        "sale_detail": count,
                    },
                )
            if not name:
                self._log_event(
                    "warning",
                    "type_name_missing",
                    self.driver.current_url,
                    {
                        "configuration_index": child_index,
                        "sale_detail": count,
                    },
                )
            if count == "0":
                self._log_event(
                    "warning",
                    "sale_detail_missing",
                    self.driver.current_url,
                    {
                        "configuration_index": child_index,
                        "type": name,
                        "fallback": "0",
                    },
                )
            entries.append((name, count))
        return entries

    @staticmethod
    def _sale_detail_from(root: WebElement) -> str:
        """Read a configuration's legacy sales span, retaining zero if absent."""
        spans = root.find_elements(By.XPATH, ".//span")
        if spans:
            span = spans[0]
            return numeric_sale_detail(
                span.get_attribute("textContent") or span.text
            )
        return "0"

    @staticmethod
    def _generation_types_from_badges(
        generation_node: WebElement,
    ) -> list[str]:
        """Read body types used when a generation has no configuration links."""
        result: list[str] = []
        seen: set[str] = set()
        for badge in generation_node.find_elements(By.XPATH, TYPE_BADGE_XPATH):
            name = clean_text(badge.get_attribute("textContent") or badge.text)
            key = name.casefold()
            if name and key not in seen:
                result.append(name)
                seen.add(key)
        return result

    @staticmethod
    def _model_name(section_nodes: list[WebElement]) -> str:
        if not section_nodes:
            return ""
        headings = section_nodes[0].find_elements(By.XPATH, ".//h1")
        return clean_text(headings[0].text) if headings else ""

    def _load_existing_rows(self) -> None:
        if not self.output.exists() or self.output.stat().st_size == 0:
            return
        with self.output.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != FIELD_NAMES:
                raise RuntimeError(f"已有 CSV 表头不兼容：{self.output}")
            for row in reader:
                self.seen_rows.add(tuple(row.get(name, "") for name in FIELD_NAMES))
                self.total_rows += 1

    def _prepare_output_schema(self) -> None:
        if not self.output.exists() or self.output.stat().st_size == 0:
            return
        with self.output.open("r", encoding="utf-8-sig", newline="") as handle:
            fieldnames = tuple(csv.DictReader(handle).fieldnames or ())
        if fieldnames == FIELD_NAMES:
            if not self.checkpoint.exists():
                return
            checkpoint_data = json.loads(self.checkpoint.read_text(encoding="utf-8"))
            if checkpoint_data.get("schema_version") == SCHEMA_VERSION:
                return
            self._backup_results(
                "before_generation_type_rows",
                "检测到旧版结果，已备份并将按 generation + type 逐行重抓",
            )
            return
        if fieldnames == SALES_WITH_RANK_FIELD_NAMES:
            self._backup_results(
                "before_rank_removal",
                "检测到含 Rank 的旧版销售结果，已备份并将按新表头重抓",
            )
            return
        if fieldnames == SALES_WITHOUT_YEARS_FIELD_NAMES:
            self._backup_results(
                "before_years",
                "检测到不含 Years 的旧版销售结果，已备份并将重新解析代际标题",
            )
            return
        if fieldnames == IMAGE_TYPE_FIELD_NAMES:
            self._backup_results(
                "before_sales_only",
                "检测到包含图片字段的旧版结果，已备份并将只抓取 generation + type + sale_detail",
            )
            return
        if fieldnames == NO_BODY_TYPE_FIELD_NAMES:
            self._backup_results(
                "before_type_restore",
                "检测到不含 type 的旧版结果，已备份并将按车型逐行重抓",
            )
            return
        if fieldnames == BODY_TYPE_FIELD_NAMES:
            self._backup_results(
                "before_type_rename",
                "检测到使用 body_type 字段的旧版结果，已备份并将改用 type 重抓",
            )
            return
        if fieldnames != LEGACY_FIELD_NAMES:
            raise RuntimeError(f"已有 CSV 表头不兼容：{self.output}")

        self._backup_results(
            "before_images",
            "检测到不含图片列的旧版结果，已备份并将从头抓取",
        )

    def _backup_results(self, label: str, message: str) -> None:
        output_backup = self._next_backup_path(self.output, label)
        self.output.replace(output_backup)
        checkpoint_backup: Path | None = None
        if self.checkpoint.exists():
            checkpoint_backup = self._next_backup_path(self.checkpoint, label)
            self.checkpoint.replace(checkpoint_backup)
        print(
            f"{message}：\n"
            f"  CSV 备份：{output_backup}"
            + (
                f"\n  checkpoint 备份：{checkpoint_backup}"
                if checkpoint_backup is not None
                else ""
            ),
            flush=True,
        )

    @staticmethod
    def _next_backup_path(path: Path, label: str) -> Path:
        candidate = path.with_name(f"{path.stem}.{label}{path.suffix}")
        if not candidate.exists():
            return candidate
        stamp = time.strftime("%Y%m%d_%H%M%S")
        return path.with_name(f"{path.stem}.{label}.{stamp}{path.suffix}")

    def _load_checkpoint(self) -> None:
        if not self.checkpoint.exists():
            return
        data = json.loads(self.checkpoint.read_text(encoding="utf-8"))
        if data.get("schema_version") != SCHEMA_VERSION:
            print("检测到旧版 checkpoint，将按 generation + type 从头处理。", flush=True)
            return
        self.completed_urls = {
            canonical_url(str(url)) for url in data.get("completed_urls", [])
        }

    def _open_tsv(self) -> None:
        self.output.parent.mkdir(parents=True, exist_ok=True)
        new_file = not self.output.exists() or self.output.stat().st_size == 0
        self.tsv_handle = self.output.open("a", encoding="utf-8-sig", newline="")
        self.tsv_writer = csv.DictWriter(
            self.tsv_handle, fieldnames=FIELD_NAMES, lineterminator="\n"
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
                    "schema_version": SCHEMA_VERSION,
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
        self._log_event(
            "error",
            "model_failed",
            url,
            f"{type(exc).__name__}: {exc}",
        )

    def _log_event(
        self, level: str, event: str, url: str, detail: object
    ) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "level": level,
            "event": event,
            "url": url,
            "detail": detail,
        }
        with self.log_path.open("a", encoding="utf-8") as handle:
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
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    pre_args, _unknown = pre_parser.parse_known_args()
    defaults = load_yaml_config(pre_args.config, "model_sales")

    parser = argparse.ArgumentParser(
        description="从 Auto.ru 车型 URL 列表提取 generation、type 和 sale_detail"
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="YAML 配置文件")
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="输入 TSV/CSV/XLSX 文件或目录")
    parser.add_argument("--sheetname", help="XLSX sheet 名；默认使用第一个 sheet")
    parser.add_argument("--url-column", default="link_url", help="输入表中的 URL 列名")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR), help="CSV 输出目录")
    parser.add_argument("--checkpoint", help="checkpoint 路径，默认与输出 CSV 同目录")
    parser.add_argument("--log", default=str(DEFAULT_LOG), help="JSONL 运行日志路径")
    parser.add_argument(
        "--max",
        type=int,
        default=0,
        help="只读取 input 的前 N 行参与本轮处理；0 表示读取全部",
    )
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
    apply_known_defaults(parser, defaults)
    args = parser.parse_args()
    if args.max < 0 or args.max_models < 0 or args.delay < 0 or args.retries < 0:
        parser.error("--max、--max-models、--delay 和 --retries 不能小于 0")
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
        models = read_model_inputs(
            Path(args.input).resolve(), args.url_column, args.sheetname
        )
        if not models:
            raise RuntimeError("输入文件中没有可用的车型 URL")
        if args.max:
            models = models[: args.max]
        driver = make_driver(args)
        AutoRuModelSalesScraper(driver, args, models).run()
        return 0
    except KeyboardInterrupt:
        append_json_log(args.log, "warning", "run_interrupted", "keyboard interrupt")
        print("\n已中断；已写入的 CSV 和 checkpoint 会保留。")
        return 130
    except Exception as exc:
        append_json_log(args.log, "error", "run_failed", f"{type(exc).__name__}: {exc}")
        print(f"错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if driver is not None and not args.keep_open:
            driver.quit()


if __name__ == "__main__":
    raise SystemExit(main())
