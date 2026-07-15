# Auto.ru 车型长宽高遍历脚本

脚本：`auto_ru_dimensions_scraper.py`

遍历顺序：

1. `https://auto.ru/catalog/cars/` 中的全部品牌
2. 每个品牌页 `#models` 中的全部车型
3. 每个车型的 `specifications/` 页面
4. 规格表中每一条“修改版本 + Габариты, Д х Ш х В”记录

页面中的品牌容器和车型容器优先使用给定 XPath；动态规格区块 ID 不会写死，脚本会按
俄文表头 `Габариты, Д х Ш х В` 找到尺寸列，再拆成长度、宽度、高度。

## 安装与检查

```powershell
cd .\scrapers\auto-ru
python -m pip install -r requirements.txt
python .\auto_ru_dimensions_scraper.py --inspect-url https://auto.ru/catalog/cars/
```

如果网站显示机器人验证，脚本会自动暂停。请在打开的 Chrome 中手动完成，确认页面已
回到 Auto.ru 后，再回 PowerShell 按 Enter，脚本会继续托管。该流程不会自动规避或
代答验证。浏览器状态保存在 `.auto_ru_selenium_profile`，不要同时运行两个使用同一
profile 的实例；需要人工验证时不要使用 `--headless`。

## 正式遍历

在仓库根目录运行：

```powershell
python .\scrapers\auto-ru\auto_ru_dimensions_scraper.py `
  --delay 10 `
  --cooldown-every 20 `
  --cooldown-seconds 20
```

如果已经进入 `scrapers\auto-ru`，则将脚本路径简写为
`.\auto_ru_dimensions_scraper.py`。PowerShell 的续行反引号后不能有空格。

默认运行流程是：Chrome 首次打开 Auto.ru 后立即暂停，脚本此时不会点击或解析页面；
你在浏览器里点击需要的按钮并完成验证，然后回 PowerShell 按 Enter，脚本才开始托管。
如果已经保存了可用的浏览器状态，并且明确不需要首次暂停，可以运行：

```powershell
python .\auto_ru_dimensions_scraper.py --no-start-pause
```

输出：

- `tsv/auto_ru_dimensions.tsv`：UTF-8 BOM、Tab 分隔，每条规格即时追加
- `tsv/auto_ru_dimensions.checkpoint.json`：已完成车型，用于断点续跑
- `tsv/auto_ru_dimensions.errors.log`：加载失败、无尺寸表等错误

TSV 字段为：品牌、车型、代际/尺寸区块、当前规格块的车身描述 `body_type`、修改版本、
长/宽/高（毫米）、原始尺寸、重量（千克）以及车型和规格来源 URL。同一车型可能因
代际、车身或动力版本产生多行。`body_type` 会从每张尺寸表所属的当前块读取，例如
`Пикап Двойная кабина`。

如果目录中已有不含 `body_type` 的旧版 TSV，下次启动时脚本会先将旧 TSV 和旧
checkpoint 分别改名为带 `.before_body_type` 的备份，再用新表头从头抓取。这样不会
丢失已有结果，也不会把两种表头或重复记录混在同一个 TSV 中。

先做小范围测试：

```powershell
python .\auto_ru_dimensions_scraper.py --brand 212 --max-models 1
```

指定多个品牌时重复传入 `--brand`：

```powershell
python .\auto_ru_dimensions_scraper.py --brand BMW --brand Audi
```

## 遍历速度和冷却

`--delay` 控制每次页面加载后的固定间隔；`--cooldown-every` 和
`--cooldown-seconds` 控制周期性长冷却。默认每页等待 1 秒，并且每完成 25 个车型冷却
30 秒。当前建议每页等待 10 秒，并且每完成 20 个车型冷却 20 秒：

```powershell
python .\auto_ru_dimensions_scraper.py --delay 10 --cooldown-every 20 --cooldown-seconds 20
```

关闭周期性长冷却，但保留每页间隔：

```powershell
python .\auto_ru_dimensions_scraper.py --delay 2 --cooldown-every 0
```

这些参数用于降低访问频率和服务器负载，不会自动规避或代答网站验证。

再次运行同一命令时，脚本会读取已有 TSV 的最后一条品牌，直接把品牌列表切到该位置，
然后用 checkpoint 跳过该品牌内已经完成的车型，不再从第一个品牌逐个检查。若 TSV 中的
品牌不在本次品牌列表（例如改用了 `--brand`），会安全回退为从头检查。

如果需要恢复从第一个品牌开始检查的旧行为（例如要重试游标之前记录在 errors log 中的
失败项），使用：

```powershell
python .\auto_ru_dimensions_scraper.py --resume-from-start
```

全量目录较大，建议保留默认的每页 1 秒间隔；遇到限流或验证码时不要提高请求频率。
