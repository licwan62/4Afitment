#!/usr/bin/env python3
"""遍历 Auto.ru 汽车目录并保存每个车型的长、宽、高。

遍历层级：品牌 -> 车型 -> specifications 页面中的每个规格行。
结果逐行追加到 TSV，车型完成后写 checkpoint，支持中断后继续。
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from selenium import webdriver
from selenium.common.exceptions import (
    JavascriptException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait


PROJECT_ROOT = Path(__file__).resolve().parent
START_URL = "https://auto.ru/catalog/cars/"
BRAND_ROOT_XPATH = '//*[@id="app"]/div/div/div[5]/div[2]/div[2]/div[2]/div[1]/div[2]/div[2]'
MODEL_ROOT_XPATH = '//*[@id="models"]/div[1]/div[1]/div[2]'
FIELD_NAMES = (
    "brand",
    "model",
    "section",
    "body_type",
    "modification",
    "length_mm",
    "width_mm",
    "height_mm",
    "dimensions_raw",
    "weight_kg",
    "model_url",
    "specifications_url",
)
LEGACY_FIELD_NAMES = tuple(name for name in FIELD_NAMES if name != "body_type")
DIMENSIONS_RE = re.compile(
    r"(?<!\d)(\d{3,5})\s*[xх×]\s*(\d{3,5})\s*[xх×]\s*(\d{3,5})(?!\d)",
    re.IGNORECASE,
)
AUXILIARY_MODEL_SLUGS = {
    "engine",
    "photo",
    "reviews",
    "videos",
    "discussions",
    "dealers",
}


@dataclass(frozen=True)
class CatalogLink:
    name: str
    url: str
    slug: str


@dataclass(frozen=True)
class DimensionRow:
    brand: str
    model: str
    section: str
    body_type: str
    modification: str
    length_mm: str
    width_mm: str
    height_mm: str
    dimensions_raw: str
    weight_kg: str
    model_url: str
    specifications_url: str


def clean_text(value: Any) -> str:
    return " ".join(str(value or "").replace("\xa0", " ").split())


def canonical_url(url: str) -> str:
    parts = urlsplit(url)
    path = re.sub(r"/+", "/", parts.path)
    if not path.endswith("/"):
        path += "/"
    return urlunsplit((parts.scheme or "https", parts.netloc, path, "", ""))


def slice_brands_from_output(
    brands: list[CatalogLink], last_output_brand: str
) -> tuple[list[CatalogLink], int]:
    """Return the brand list starting at the last brand written to the TSV."""
    target = clean_text(last_output_brand).casefold()
    if not target:
        return brands, 0
    for index, brand in enumerate(brands):
        if clean_text(brand.name).casefold() == target:
            return brands[index:], index
    return brands, -1


class AutoRuDimensionsScraper:
    def __init__(self, driver: webdriver.Chrome, args: argparse.Namespace) -> None:
        self.driver = driver
        self.args = args
        self.wait = WebDriverWait(driver, args.timeout)
        self.output = Path(args.output).resolve()
        self.checkpoint = Path(
            args.checkpoint or self.output.with_suffix(".checkpoint.json")
        ).resolve()
        self.error_log = self.output.with_suffix(".errors.log")
        self.completed_models: set[str] = set()
        self.seen: set[tuple[str, ...]] = set()
        self.tsv_handle: Any = None
        self.tsv_writer: csv.DictWriter | None = None
        self.total_rows = 0
        self.processed_models = 0
        self.last_output_brand = ""

    def run(self) -> None:
        self._prepare_output_schema()
        self._load_existing_rows()
        self._load_checkpoint()
        self._open_tsv()
        try:
            if self.args.inspect_url:
                self.inspect(self.args.inspect_url, pause_after_load=True)
                return

            print("[1/4] 正在读取 Auto.ru 品牌列表……")
            self._open_page(self.args.url, pause_after_load=True)
            brands = self._extract_brand_links()
            brands = self._filter_brands(brands)
            if not brands:
                raise RuntimeError("未发现品牌链接；请用 --inspect-url 检查页面或处理验证码")
            if not self.args.resume_from_start and self.last_output_brand:
                resumed_brands, skipped_count = slice_brands_from_output(
                    brands, self.last_output_brand
                )
                if skipped_count > 0:
                    brands = resumed_brands
                    print(
                        f"断点直达：TSV 最后一条是 {self.last_output_brand}，"
                        f"直接跳过前面 {skipped_count} 个品牌",
                        flush=True,
                    )
                elif skipped_count < 0:
                    print(
                        f"断点品牌 {self.last_output_brand} 不在本次品牌列表中，"
                        "安全回退为从头检查",
                        flush=True,
                    )
            print(f"[2/4] 发现 {len(brands)} 个待遍历品牌")
            print("[3/4] 开始逐品牌读取车型规格……")

            for brand_index, brand in enumerate(brands, 1):
                if self._limit_reached():
                    break
                try:
                    self._open_page(brand.url)
                    models = self._extract_model_links(brand)
                    print(
                        f"[{brand_index}/{len(brands)}] {brand.name}："
                        f"发现 {len(models)} 个车型",
                        flush=True,
                    )
                except Exception as exc:
                    self._log_error("brand", brand.url, exc)
                    print(f"  ! 品牌页读取失败，已记录：{brand.name}", file=sys.stderr)
                    continue

                for model in models:
                    if self._limit_reached():
                        break
                    specifications_url = canonical_url(
                        urljoin(model.url, "specifications/")
                    )
                    if specifications_url in self.completed_models:
                        continue
                    try:
                        self._open_page(specifications_url)
                        rows = self._extract_dimension_rows(
                            brand, model, specifications_url
                        )
                        if not rows:
                            raise RuntimeError(
                                "规格页中未找到包含“Габариты, Д х Ш х В”的尺寸行"
                            )
                        saved = sum(self._save_row(row) for row in rows)
                        self.completed_models.add(specifications_url)
                        self.processed_models += 1
                        self._write_checkpoint()
                        print(
                            f"  {model.name}：解析 {len(rows)} 行，新增 {saved} 行，"
                            f"累计 {self.total_rows}",
                            flush=True,
                        )
                        self._maybe_cooldown()
                    except KeyboardInterrupt:
                        self._write_checkpoint()
                        raise
                    except Exception as exc:
                        self._log_error("model", specifications_url, exc)
                        print(
                            f"  ! 车型读取失败，已记录并继续：{brand.name} {model.name}",
                            file=sys.stderr,
                        )

            self._write_checkpoint()
            print("[4/4] 遍历结束")
            print(f"TSV：{self.output}")
            print(f"累计记录：{self.total_rows}，本次完成车型：{self.processed_models}")
        finally:
            if self.tsv_handle:
                self.tsv_handle.close()

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
                self._handle_manual_challenge()
                self._dismiss_cookie_banner()
                if self.args.delay:
                    time.sleep(self.args.delay)
                return
            except (TimeoutException, WebDriverException) as exc:
                last_error = exc
                if attempt <= self.args.retries:
                    time.sleep(min(2**attempt, 8))
        assert last_error is not None
        raise last_error

    def _pause_after_initial_load(self) -> None:
        if self.args.no_start_pause:
            return
        if self.args.headless:
            raise RuntimeError(
                "首次进入网页需要人工确认；请去掉 --headless，"
                "或明确传入 --no-start-pause"
            )
        if not sys.stdin.isatty():
            raise RuntimeError("首次进入网页需要在交互式 PowerShell 中按 Enter")
        print(
            "\nAuto.ru 已打开，脚本现已暂停，尚未点击或解析页面。\n"
            "请在 Chrome 中点击需要的按钮并完成验证。完成后回到这个 PowerShell，\n"
            "按 Enter 开始托管遍历。",
            flush=True,
        )
        input()
        print("收到 Enter，开始托管……", flush=True)

    def _challenge_is_visible(self) -> bool:
        url = self.driver.current_url.lower()
        title = self.driver.title.casefold()
        if "showcaptcha" in url or "captcha" in url or "ой!" in title:
            return True
        try:
            return bool(
                self.driver.execute_script(
                    """
                    const text = (document.body?.innerText || '').toLowerCase();
                    const challengeNode = document.querySelector(
                        'form[action*="captcha"],iframe[src*="captcha"],' +
                        '[class*="Captcha"],[class*="captcha"],' +
                        '[data-testid*="captcha"]'
                    );
                    return !!challengeNode ||
                        text.includes('подтвердите, что запросы отправляли вы') ||
                        text.includes('подтвердите, что вы не робот') ||
                        text.includes('я не робот');
                    """
                )
            )
        except JavascriptException:
            return False

    def _handle_manual_challenge(self) -> None:
        if not self._challenge_is_visible():
            return
        if self.args.headless or not sys.stdin.isatty():
            raise RuntimeError(
                "Auto.ru 显示机器人验证；请去掉 --headless，在浏览器中手动完成"
            )
        print(
            "\n检测到 Auto.ru 机器人验证，脚本已暂停。\n"
            "请在打开的 Chrome 中手动完成验证；确认页面已回到 Auto.ru 后，\n"
            "回到这个 PowerShell 窗口按 Enter，脚本将继续托管。",
            flush=True,
        )
        input()
        if self._challenge_is_visible():
            raise RuntimeError(
                "按 Enter 后仍检测到机器人验证；请重新运行并先完整通过验证"
            )
        print("验证页面已通过，继续自动遍历……", flush=True)

    def _dismiss_cookie_banner(self) -> None:
        selectors = (
            "button[data-testid*='accept']",
            "button[class*='Cookie']",
            "button[class*='cookie']",
        )
        for selector in selectors:
            for element in self.driver.find_elements(By.CSS_SELECTOR, selector):
                try:
                    text = clean_text(element.text).casefold()
                    if element.is_displayed() and (
                        "принять" in text or "соглас" in text or not text
                    ):
                        element.click()
                        return
                except (StaleElementReferenceException, WebDriverException):
                    continue

    def _extract_brand_links(self) -> list[CatalogLink]:
        roots = self.driver.find_elements(By.XPATH, BRAND_ROOT_XPATH)
        root = roots[0] if roots else self.driver.find_element(By.TAG_NAME, "body")
        raw = self._anchors_in(root)
        links: dict[str, CatalogLink] = {}
        pattern = re.compile(r"^/catalog/cars/([^/]+)/$")
        for item in raw:
            url = canonical_url(urljoin(self.driver.current_url, item["href"]))
            match = pattern.fullmatch(urlsplit(url).path)
            name = clean_text(item["text"])
            if match and name:
                links.setdefault(url, CatalogLink(name, url, match.group(1)))
        # 给定 XPath 偶尔只覆盖可见品牌区域；不足时从整个页面补齐精确品牌链接。
        if len(links) < 20 and roots:
            for item in self._anchors_in(self.driver.find_element(By.TAG_NAME, "body")):
                url = canonical_url(urljoin(self.driver.current_url, item["href"]))
                match = pattern.fullmatch(urlsplit(url).path)
                name = clean_text(item["text"])
                if match and name:
                    links.setdefault(url, CatalogLink(name, url, match.group(1)))
        return list(links.values())

    def _extract_model_links(self, brand: CatalogLink) -> list[CatalogLink]:
        roots = self.driver.find_elements(By.XPATH, MODEL_ROOT_XPATH)
        model_sections = self.driver.find_elements(By.ID, "models")
        root = roots[0] if roots else (
            model_sections[0]
            if model_sections
            else self.driver.find_element(By.TAG_NAME, "body")
        )
        raw = self._anchors_in(root)
        links: dict[str, CatalogLink] = {}
        pattern = re.compile(
            rf"^/catalog/cars/{re.escape(brand.slug)}/([^/]+)/$", re.IGNORECASE
        )
        for item in raw:
            url = canonical_url(urljoin(self.driver.current_url, item["href"]))
            match = pattern.fullmatch(urlsplit(url).path)
            name = clean_text(item["text"])
            if match and name and match.group(1).casefold() not in AUXILIARY_MODEL_SLUGS:
                links.setdefault(url, CatalogLink(name, url, match.group(1)))
        return list(links.values())

    def _anchors_in(self, root: Any) -> list[dict[str, str]]:
        return self.driver.execute_script(
            """
            return Array.from(arguments[0].querySelectorAll('a[href]')).map(a => ({
                href: a.href || a.getAttribute('href') || '',
                text: (a.innerText || a.textContent || '')
                    .replace(/\\s+/g, ' ').trim()
            }));
            """,
            root,
        )

    def _extract_dimension_rows(
        self, brand: CatalogLink, model: CatalogLink, specifications_url: str
    ) -> list[DimensionRow]:
        raw_tables = self.driver.execute_script(
            """
            const norm = s => (s || '').replace(/\\s+/g, ' ').trim();
            const headings = Array.from(document.querySelectorAll('h2,h3'));
            const previousHeading = table => {
                let found = '';
                for (const h of headings) {
                    if (h.compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING)
                        found = norm(h.innerText || h.textContent);
                }
                return found;
            };
            return Array.from(document.querySelectorAll('table')).map(table => ({
                section: previousHeading(table),
                bodyType: norm(
                    table.closest('.SpecificationContent__configuration')
                        ?.querySelector('.SpecificationContent__thumb_firstText')
                        ?.innerText || ''
                ),
                header: norm((table.querySelector('thead') || table).innerText),
                rows: Array.from(table.querySelectorAll('tbody tr')).map(tr =>
                    Array.from(tr.querySelectorAll('th,td')).map(td =>
                        norm(td.innerText || td.textContent)))
            }));
            """
        )
        result: list[DimensionRow] = []
        for table in raw_tables:
            section = clean_text(table.get("section"))
            body_type = clean_text(table.get("bodyType"))
            for cells in table.get("rows", []):
                cells = [clean_text(cell) for cell in cells]
                if len(cells) < 2:
                    continue
                match = DIMENSIONS_RE.search(cells[1])
                if not match:
                    continue
                weight_match = re.search(r"\d+(?:[.,]\d+)?", cells[2]) if len(cells) > 2 else None
                result.append(
                    DimensionRow(
                        brand=brand.name,
                        model=model.name,
                        section=section,
                        body_type=body_type,
                        modification=cells[0],
                        length_mm=match.group(1),
                        width_mm=match.group(2),
                        height_mm=match.group(3),
                        dimensions_raw=match.group(0),
                        weight_kg=weight_match.group(0).replace(",", ".") if weight_match else "",
                        model_url=model.url,
                        specifications_url=specifications_url,
                    )
                )
        return result

    def _filter_brands(self, brands: list[CatalogLink]) -> list[CatalogLink]:
        if not self.args.brand:
            return brands
        wanted = {value.casefold() for value in self.args.brand}
        selected = [
            brand
            for brand in brands
            if brand.name.casefold() in wanted or brand.slug.casefold() in wanted
        ]
        found = {brand.name.casefold() for brand in selected} | {
            brand.slug.casefold() for brand in selected
        }
        missing = sorted(value for value in wanted if value not in found)
        if missing:
            print(f"警告：未找到品牌：{', '.join(missing)}", file=sys.stderr)
        return selected

    def _limit_reached(self) -> bool:
        return bool(
            self.args.max_models and self.processed_models >= self.args.max_models
        )

    def _maybe_cooldown(self) -> None:
        every = self.args.cooldown_every
        seconds = self.args.cooldown_seconds
        if not every or seconds <= 0 or self.processed_models % every != 0:
            return
        if self._limit_reached():
            return
        print(
            f"已完成 {self.processed_models} 个车型，冷却 {seconds:g} 秒……",
            flush=True,
        )
        time.sleep(seconds)

    def _prepare_output_schema(self) -> None:
        """安全归档缺少 body_type 的旧结果，防止新旧表头混写。"""
        if not self.output.exists() or self.output.stat().st_size == 0:
            return
        with self.output.open("r", encoding="utf-8-sig", newline="") as handle:
            fieldnames = csv.DictReader(handle, delimiter="\t").fieldnames or []
        if all(name in fieldnames for name in FIELD_NAMES):
            return
        if not all(name in fieldnames for name in LEGACY_FIELD_NAMES):
            raise RuntimeError(f"已有 TSV 表头不兼容：{self.output}")

        output_backup = self._next_backup_path(self.output, "before_body_type")
        self.output.replace(output_backup)
        checkpoint_backup: Path | None = None
        if self.checkpoint.exists():
            checkpoint_backup = self._next_backup_path(
                self.checkpoint, "before_body_type"
            )
            self.checkpoint.replace(checkpoint_backup)
        print(
            "检测到旧版 TSV（缺少 body_type），已完整备份并将用新表头重新抓取：\n"
            f"  TSV 备份：{output_backup}"
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

    def _load_existing_rows(self) -> None:
        if not self.output.exists() or self.output.stat().st_size == 0:
            return
        with self.output.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle, delimiter="\t")
            if not reader.fieldnames or not all(name in reader.fieldnames for name in FIELD_NAMES):
                raise RuntimeError(f"已有 TSV 表头不兼容：{self.output}")
            for row in reader:
                key = tuple(row[name] for name in FIELD_NAMES)
                self.seen.add(key)
                self.total_rows += 1
                brand = clean_text(row.get("brand"))
                if brand:
                    self.last_output_brand = brand
        print(f"断点续跑：已读取 {self.total_rows} 条历史 TSV 记录")

    def _load_checkpoint(self) -> None:
        if not self.checkpoint.exists():
            return
        data = json.loads(self.checkpoint.read_text(encoding="utf-8"))
        self.completed_models = {
            canonical_url(str(url)) for url in data.get("completed_models", [])
        }
        print(f"断点续跑：checkpoint 已完成 {len(self.completed_models)} 个车型")

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

    def _save_row(self, row: DimensionRow) -> int:
        values = asdict(row)
        key = tuple(values[name] for name in FIELD_NAMES)
        if key in self.seen:
            return 0
        assert self.tsv_writer is not None and self.tsv_handle is not None
        self.tsv_writer.writerow(values)
        self.tsv_handle.flush()
        self.seen.add(key)
        self.total_rows += 1
        return 1

    def _write_checkpoint(self) -> None:
        self.checkpoint.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.checkpoint.with_suffix(self.checkpoint.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "completed_models": sorted(self.completed_models),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(self.checkpoint)

    def _log_error(self, level: str, url: str, exc: Exception) -> None:
        self.error_log.parent.mkdir(parents=True, exist_ok=True)
        message = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "level": level,
            "url": url,
            "error": f"{type(exc).__name__}: {exc}",
        }
        with self.error_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(message, ensure_ascii=False) + "\n")

    def inspect(self, url: str, pause_after_load: bool = False) -> None:
        self._open_page(url, pause_after_load=pause_after_load)
        print(f"URL：{self.driver.current_url}")
        print(f"Title：{self.driver.title}")
        path = urlsplit(self.driver.current_url).path
        if re.fullmatch(r"/catalog/cars/?", path):
            links = self._extract_brand_links()
            print(f"品牌链接：{len(links)}")
            print(json.dumps([asdict(link) for link in links[:30]], ensure_ascii=False, indent=2))
            return
        match = re.fullmatch(r"/catalog/cars/([^/]+)/?", path)
        if match:
            brand = CatalogLink(match.group(1), canonical_url(url), match.group(1))
            links = self._extract_model_links(brand)
            print(f"车型链接：{len(links)}")
            print(json.dumps([asdict(link) for link in links[:30]], ensure_ascii=False, indent=2))
            return
        raw = self.driver.execute_script(
            """
            return Array.from(document.querySelectorAll('table')).map((t, i) => ({
                index: i,
                text: (t.innerText || t.textContent || '').replace(/\\s+/g, ' ').trim()
            }));
            """
        )
        print(json.dumps(raw, ensure_ascii=False, indent=2))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="遍历 Auto.ru 车型目录并抓取长宽高")
    parser.add_argument("--url", default=START_URL, help="Auto.ru 汽车目录网址")
    parser.add_argument(
        "--output",
        default=str(PROJECT_ROOT / "tsv" / "auto_ru_dimensions.tsv"),
        help="输出 TSV 路径",
    )
    parser.add_argument("--checkpoint", help="checkpoint 路径，默认与 TSV 同目录")
    parser.add_argument(
        "--brand", action="append", help="只抓指定品牌名或 URL slug，可重复传入"
    )
    parser.add_argument(
        "--resume-from-start",
        action="store_true",
        help="忽略 TSV 最后品牌，从品牌列表开头逐个检查",
    )
    parser.add_argument("--max-models", type=int, default=0, help="本次最多完成车型数")
    parser.add_argument("--timeout", type=float, default=25, help="页面等待秒数")
    parser.add_argument(
        "--delay", type=float, default=1.0, help="每个页面加载完成后的固定间隔秒数"
    )
    parser.add_argument(
        "--cooldown-every",
        type=int,
        default=25,
        help="每完成多少个车型进行一次长冷却，0 表示关闭",
    )
    parser.add_argument(
        "--cooldown-seconds",
        type=float,
        default=30.0,
        help="每次长冷却的秒数，默认 30 秒",
    )
    parser.add_argument("--retries", type=int, default=2, help="页面加载失败重试次数")
    parser.add_argument("--headless", action="store_true", help="无界面运行")
    parser.add_argument(
        "--no-start-pause",
        action="store_true",
        help="首次打开网页后不等待人工按 Enter，直接开始",
    )
    parser.add_argument("--inspect-url", help="只检查一个目录/品牌/规格页面")
    parser.add_argument(
        "--profile-dir",
        default=str(PROJECT_ROOT / ".auto_ru_selenium_profile"),
        help="Chrome 独立用户数据目录",
    )
    parser.add_argument("--keep-open", action="store_true", help="完成后不关闭浏览器")
    args = parser.parse_args()
    if args.delay < 0:
        parser.error("--delay 不能小于 0")
    if args.cooldown_every < 0:
        parser.error("--cooldown-every 不能小于 0")
    if args.cooldown_seconds < 0:
        parser.error("--cooldown-seconds 不能小于 0")
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
        AutoRuDimensionsScraper(driver, args).run()
        return 0
    except KeyboardInterrupt:
        print("\n已中断；已写入的 TSV 和 checkpoint 会保留，下次可继续。")
        return 130
    except Exception as exc:
        print(f"错误：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        if driver is not None and not args.keep_open:
            driver.quit()


if __name__ == "__main__":
    raise SystemExit(main())
