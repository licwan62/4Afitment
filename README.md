# Scraper Monorepo

这个仓库包含三个互相独立的抓取项目。每个子项目都有自己的脚本、依赖、说明文档、运行数据和断点文件。

| 子项目 | 用途 | 技术栈 |
| --- | --- | --- |
| [`scrapers/4afitment`](scrapers/4afitment/) | 抓取 4AFitment 年份、品牌、车型及 fitment 数据 | Node.js + Playwright |
| [`scrapers/amazon-de`](scrapers/amazon-de/) | 遍历 Amazon.de 五级车型选择器 | Python + Selenium |
| [`scrapers/auto-ru`](scrapers/auto-ru/) | 抓取 Auto.ru 车型长、宽、高等规格 | Python + Selenium |

## 快速开始

进入需要运行的子项目，再按该目录的 `README.md` 安装依赖和执行脚本。

```powershell
# 4AFitment
cd .\scrapers\4afitment
.\run.ps1 src\scrape.js

# Amazon.de
cd ..\amazon-de
python -m pip install -r requirements.txt
python .\amazon_de_fitment_scraper.py

# Auto.ru
cd ..\auto-ru
python -m pip install -r requirements.txt
python .\auto_ru_dimensions_scraper.py --delay 10 --cooldown-every 20 --cooldown-seconds 20
```

三个项目的输出和浏览器状态互不混用。已有的抓取结果、checkpoint 和 profile 已迁移到对应子项目中。

## Python 脚本参数

### `amazon_de_fitment_scraper.py`

推荐直接使用默认参数：

```powershell
python .\amazon_de_fitment_scraper.py
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--url <网址>` | 脚本内置商品页 | Amazon 商品网址 |
| `--output <路径>` | `output/amazon_de_vehicles.csv` | 输出 CSV 路径 |
| `--timeout <秒>` | `20` | 等待页面控件的最长时间 |
| `--delay <秒>` | `0.55` | 每次选择后的等待时间 |
| `--max-rows <数量>` | `0` | 最多新增/保留的行数；`0` 表示不限 |
| `--profile-dir <路径>` | `.amazon_selenium_profile` | Chrome 独立用户数据目录 |
| `--inspect` | 关闭 | 只检查控件，不正式遍历 |
| `--headless` | 关闭 | 使用无界面模式运行 |
| `--keep-open` | 关闭 | 完成后不自动关闭浏览器 |

### `auto_ru_dimensions_scraper.py`

推荐使用较低的访问频率：

```powershell
python .\auto_ru_dimensions_scraper.py --delay 10 --cooldown-every 20 --cooldown-seconds 20
```

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--url <网址>` | Auto.ru 汽车目录 | 起始目录网址 |
| `--output <路径>` | `tsv/auto_ru_dimensions.tsv` | 输出 TSV 路径 |
| `--checkpoint <路径>` | 与 TSV 同目录 | 自定义 checkpoint 路径 |
| `--brand <品牌>` | 不限 | 只抓指定品牌名或 URL slug；可重复传入 |
| `--resume-from-start` | 关闭 | 忽略 TSV 最后品牌，从品牌列表开头检查 |
| `--max-models <数量>` | `0` | 本次最多完成的车型数；`0` 表示不限 |
| `--timeout <秒>` | `25` | 页面等待时间 |
| `--delay <秒>` | `1` | 每个页面加载完成后的固定间隔；推荐 `10` |
| `--cooldown-every <数量>` | `25` | 每完成多少个车型长冷却一次；推荐 `20`，`0` 表示关闭 |
| `--cooldown-seconds <秒>` | `30` | 每次长冷却时间；推荐 `20` |
| `--retries <数量>` | `2` | 页面加载失败重试次数 |
| `--profile-dir <路径>` | `.auto_ru_selenium_profile` | Chrome 独立用户数据目录 |
| `--inspect-url <网址>` | 无 | 只检查一个目录、品牌或规格页面 |
| `--no-start-pause` | 关闭 | 首次打开网页后直接开始，不等待 Enter |
| `--headless` | 关闭 | 使用无界面模式运行 |
| `--keep-open` | 关闭 | 完成后不关闭浏览器 |

更完整的运行流程和示例见各子项目的 `README.md`。
