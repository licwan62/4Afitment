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
python .\auto_ru_dimensions_scraper.py
```

三个项目的输出和浏览器状态互不混用。已有的抓取结果、checkpoint 和 profile 已迁移到对应子项目中。
