#!/usr/bin/env python3
"""Extract categorized images from gallery links on Auto.ru model pages."""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.ui import WebDriverWait

from auto_ru_model_sales_scraper import (
    DEFAULT_INPUT,
    PROJECT_ROOT,
    ModelInput,
    canonical_url,
    clean_text,
    generation_and_years,
    make_driver,
    read_model_inputs,
)


DEFAULT_OUTPUT = PROJECT_ROOT / "tsv" / "auto_ru_gallery_images.tsv"
DEFAULT_LOG = PROJECT_ROOT / "log" / "auto_ru_gallery_images.log"
GENERATIONS_ROOT_XPATH = "//div[@data-seo='generation-list']"
GALLERY_LINK_XPATH = (
    ".//a[contains(@class, 'CatalogGenerationListItemGallery-') "
    "and contains(@class, 'CatalogGenerationsListItem__gallery-')]"
)
GENERATION_ITEM_XPATH = (
    "./ancestor::div[parent::div[@data-seo='generation-list']][1]"
)
BODY_GALLERY_XPATH = (
    "/html/body/div[1]/div/div/div[5]/div/div[2]/div[1]/section/div[1]/div/ul"
)
BODY_GALLERY_FALLBACK_XPATH = (
    "//section[contains(@class, 'ContentGenerationGallery-')]"
)
IMAGE_IDS = ("main", "side", "front", "back", "3_4_behind")
SCHEMA_VERSION = 7
FIELD_NAMES = (
    "Model",
    "link_url",
    "generation",
    "Years",
    "gallery_url",
    "main_image_url",
    "side_image_url",
    "front_image_url",
    "back_image_url",
    "3_4_behind_image_url",
    "remark",
)
WITH_RANK_FIELD_NAMES = ("Rank", *FIELD_NAMES)
LEGACY_FIELD_NAMES = tuple(name for name in FIELD_NAMES if name != "Years")
LEGACY_WITH_RANK_FIELD_NAMES = ("Rank", *LEGACY_FIELD_NAMES)


class AutoRuGalleryImageScraper:
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
        self.log_path = Path(args.log).resolve()
        self.completed_urls: set[str] = set()
        self.seen_rows: set[tuple[str, ...]] = set()
        self.image_cache: dict[str, dict[str, str]] = {}
        self.total_rows = 0
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
                    rows = self._extract_gallery_rows(model)
                    for row_index, row in enumerate(rows, 1):
                        row.update(self._images_for(row["gallery_url"]))
                        print(
                            f"  图片 [{row_index}/{len(rows)}] "
                            f"{row['generation']} / {row['gallery_url']}",
                            flush=True,
                        )
                    saved = sum(self._save_row(row) for row in rows)
                    self.completed_urls.add(model.link_url)
                    self._write_checkpoint()
                    print(
                        f"[{index}/{len(pending)}] {model.model or model.link_url}："
                        f"gallery {len(rows)}，新增 {saved} 行",
                        flush=True,
                    )
                    self._log_event(
                        "info",
                        "model_completed",
                        model.link_url,
                        {"gallery_rows": len(rows), "saved_rows": saved},
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
        input("Auto.ru 已打开。请完成必要的验证，然后按 Enter 开始抓图：")

    def _challenge_visible(self) -> bool:
        url = self.driver.current_url.casefold()
        title = self.driver.title.casefold()
        return "captcha" in url or "showcaptcha" in url or "ой!" in title

    def _wait_for_challenge(self) -> None:
        if not self._challenge_visible():
            return
        if self.args.headless or not sys.stdin.isatty():
            raise RuntimeError("遇到 Auto.ru 验证页，需要在可见浏览器中手工完成")
        input("检测到验证页。请完成验证并返回车型页，然后按 Enter：")
        if self._challenge_visible():
            raise RuntimeError("验证页仍然存在")

    def _extract_gallery_rows(self, model: ModelInput) -> list[dict[str, str]]:
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
        seen: set[str] = set()
        gallery_links = roots[0].find_elements(By.XPATH, GALLERY_LINK_XPATH)
        if not gallery_links:
            self._log_event(
                "warning",
                "gallery_links_missing",
                model.link_url,
                "generation-list 中未找到匹配 gallery class 的 A",
            )
        for gallery_link in gallery_links:
            href = clean_text(gallery_link.get_attribute("href"))
            if not href:
                continue
            gallery_url = urljoin(self.driver.current_url, href)
            key = canonical_url(gallery_url)
            if key in seen:
                continue
            seen.add(key)
            generation_nodes = gallery_link.find_elements(
                By.XPATH, GENERATION_ITEM_XPATH
            )
            if not generation_nodes:
                self._log_event(
                    "warning",
                    "gallery_generation_missing",
                    model.link_url,
                    {"gallery_url": gallery_url},
                )
            generation, years = (
                generation_and_years(generation_nodes[0])
                if generation_nodes
                else ("", "")
            )
            if generation_nodes and not generation and not years:
                self._log_event(
                    "warning",
                    "generation_title_missing",
                    model.link_url,
                    {"gallery_url": gallery_url},
                )
            result.append(
                {
                    "Model": model.model,
                    "link_url": model.link_url,
                    "generation": generation,
                    "Years": years,
                    "gallery_url": gallery_url,
                    **self._empty_image_fields(),
                }
            )
        return result

    def _images_for(self, gallery_url: str) -> dict[str, str]:
        if gallery_url in self.image_cache:
            return self.image_cache[gallery_url]
        self._open_page(gallery_url)
        result = self._empty_image_fields()
        found_picture_ids: set[str] = set()
        galleries = self.driver.find_elements(By.XPATH, BODY_GALLERY_XPATH)
        gallery_locator = BODY_GALLERY_XPATH
        if not galleries:
            galleries = self.driver.find_elements(
                By.XPATH, BODY_GALLERY_FALLBACK_XPATH
            )
            gallery_locator = BODY_GALLERY_FALLBACK_XPATH
            if galleries:
                self._log_event(
                    "info",
                    "image_gallery_fallback_used",
                    gallery_url,
                    {"locator": BODY_GALLERY_FALLBACK_XPATH},
                )
        if galleries:
            pictures = galleries[0].find_elements(
                By.XPATH, ".//picture[@data-id]"
            )
            for picture in pictures:
                image_id = clean_text(picture.get_attribute("data-id"))
                field = f"{image_id}_image_url"
                if image_id not in IMAGE_IDS or result[field]:
                    continue
                found_picture_ids.add(image_id)
                source = self._picture_image_url(picture)
                if source:
                    result[field] = urljoin(self.driver.current_url, source)
            if not pictures:
                self._log_event(
                    "warning",
                    "gallery_pictures_missing",
                    gallery_url,
                    {"locator": gallery_locator},
                )
        else:
            self._log_event(
                "warning",
                "image_gallery_missing",
                gallery_url,
                {
                    "primary_locator": BODY_GALLERY_XPATH,
                    "fallback_locator": BODY_GALLERY_FALLBACK_XPATH,
                },
            )
        missing = [x for x in IMAGE_IDS if not result[f"{x}_image_url"]]
        result["remark"] = f"{'/'.join(missing)}缺失" if missing else ""
        if missing:
            self._log_event(
                "warning",
                "image_types_missing",
                gallery_url,
                {
                    "missing": {
                        image_id: (
                            "img_src_missing"
                            if image_id in found_picture_ids
                            else "picture_data_id_missing"
                        )
                        for image_id in missing
                    }
                },
            )
        self.image_cache[gallery_url] = result
        return result

    @staticmethod
    def _picture_image_url(picture: WebElement) -> str:
        images = picture.find_elements(By.XPATH, "./img")
        if images:
            for attribute in ("src", "currentSrc", "srcset", "data-src"):
                value = clean_text(images[0].get_attribute(attribute))
                source = AutoRuGalleryImageScraper._srcset_first_url(value)
                if source and not source.startswith("data:"):
                    return source

        source_nodes = picture.find_elements(
            By.XPATH, "./source[contains(@media, 'min-width')]"
        )
        source_nodes.extend(picture.find_elements(By.XPATH, "./source"))
        seen_sources: set[str] = set()
        for source_node in source_nodes:
            source_key = source_node.id
            if source_key in seen_sources:
                continue
            seen_sources.add(source_key)
            for attribute in ("srcset", "srcSet", "src"):
                value = clean_text(source_node.get_attribute(attribute))
                source = AutoRuGalleryImageScraper._srcset_first_url(value)
                if source and not source.startswith("data:"):
                    return source
        return ""

    @staticmethod
    def _srcset_first_url(value: str) -> str:
        if not value:
            return ""
        first_candidate = value.split(",", 1)[0].strip()
        return first_candidate.split(None, 1)[0] if first_candidate else ""

    @staticmethod
    def _empty_image_fields() -> dict[str, str]:
        return {
            **{f"{image_id}_image_url": "" for image_id in IMAGE_IDS},
            "remark": "",
        }

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

    def _prepare_output_schema(self) -> None:
        if not self.output.exists() or self.output.stat().st_size == 0:
            return
        with self.output.open("r", encoding="utf-8-sig", newline="") as handle:
            fieldnames = tuple(csv.DictReader(handle, delimiter="\t").fieldnames or ())
        if fieldnames == FIELD_NAMES:
            if not self.checkpoint.exists():
                return
            checkpoint_data = json.loads(self.checkpoint.read_text(encoding="utf-8"))
            if checkpoint_data.get("schema_version") == SCHEMA_VERSION:
                return
        elif fieldnames not in {
            WITH_RANK_FIELD_NAMES,
            LEGACY_FIELD_NAMES,
            LEGACY_WITH_RANK_FIELD_NAMES,
        }:
            raise RuntimeError(f"已有 TSV 表头不兼容：{self.output}")
        stamp = time.strftime("%Y%m%d_%H%M%S")
        output_backup = self.output.with_name(
            f"{self.output.stem}.before_years.{stamp}{self.output.suffix}"
        )
        self.output.replace(output_backup)
        if self.checkpoint.exists():
            checkpoint_backup = self.checkpoint.with_name(
                f"{self.checkpoint.stem}.before_years.{stamp}{self.checkpoint.suffix}"
            )
            self.checkpoint.replace(checkpoint_backup)
        print(f"旧版图片结果已备份：{output_backup}", flush=True)

    def _load_checkpoint(self) -> None:
        if not self.checkpoint.exists():
            return
        data = json.loads(self.checkpoint.read_text(encoding="utf-8"))
        if data.get("schema_version") != SCHEMA_VERSION:
            return
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="从 Auto.ru gallery 链接提取五类图片")
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="输入 TSV、CSV 或纯 URL 文件")
    parser.add_argument("--url-column", default="link_url", help="输入表中的 URL 列名")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT), help="输出 TSV 路径")
    parser.add_argument("--checkpoint", help="checkpoint 路径")
    parser.add_argument("--log", default=str(DEFAULT_LOG), help="JSONL 运行日志路径")
    parser.add_argument("--max", type=int, default=0, help="只读取 input 前 N 行；0 表示全部")
    parser.add_argument("--max-models", type=int, default=0, help="本次最多处理的未完成车型数")
    parser.add_argument("--timeout", type=float, default=25)
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--no-start-pause", action="store_true")
    parser.add_argument(
        "--profile-dir",
        default=str(PROJECT_ROOT / ".auto_ru_selenium_profile"),
    )
    parser.add_argument("--keep-open", action="store_true")
    args = parser.parse_args()
    if args.max < 0 or args.max_models < 0 or args.delay < 0 or args.retries < 0:
        parser.error("数量、延迟和重试参数不能小于 0")
    if args.timeout <= 0:
        parser.error("--timeout 必须大于 0")
    return args


def main() -> int:
    args = parse_args()
    driver: webdriver.Chrome | None = None
    try:
        models = read_model_inputs(Path(args.input).resolve(), args.url_column)
        if not models:
            raise RuntimeError("输入文件中没有可用的车型 URL")
        if args.max:
            models = models[: args.max]
        driver = make_driver(args)
        AutoRuGalleryImageScraper(driver, args, models).run()
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
