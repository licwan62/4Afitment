# 4AFitment Vehicle Dropdown Scraper

这个小项目用于从 `https://car.4afitment.com/search/vc` 遍历车型兼容搜索页里的下拉列表：

- 年份范围固定选择 `1896` 到 `2027`
- 遍历制造商 / 品牌
- 嵌套遍历车型
- 每个组合点击搜索
- 点击“复制车型数据”
- 把复制出来的内容追加保存为 Markdown

它不会把账号密码写进代码。第一次运行登录脚本时，会打开浏览器，你手动登录后按回车，项目会把登录状态保存在本机 `.auth/profile` 目录里。

## 使用

在 PowerShell 里进入项目目录：

```powershell
cd D:\Home\Scripts\4Afitment
```

第一次先登录：

```powershell
.\run.ps1 src/login.js
```

浏览器打开后手动登录 4AFitment。登录完成并能看到车辆兼容搜索页后，回到 PowerShell 按回车。

然后开始抓取：

```powershell
.\run.ps1 src/scrape.js
```

结果会输出到：

- `output/fitment_data.md`
- `output/checkpoint.json`
- `output/network.jsonl`

## 按 TSV 清单抓取

推荐按 project 放输入文件。比如你当前这个任务：

```text
projects\brandlimit_0617\input\carlist.tsv
```

每个 project 都有自己的 `input` 和 `output`，避免不同任务的结果混在一起。

默认 project 根目录由 `config.json` 的 `projectsDir` 控制：

```json
{
  "projectsDir": "projects"
}
```

在当前项目里，它对应：

```text
D:\Home\Scripts\4Afitment\projects
```

支持两种格式：

```tsv
make
Acura
BMW
```

只有品牌列时，会遍历该品牌下的所有车型。

```tsv
make	model
Acura	MDX
BMW	X5
```

有品牌和车型两列时，只抓取指定组合。列名也兼容 `brand`、`manufacturer`、`品牌`、`model`、`车型`。

运行：

```powershell
.\run.ps1 src/scrape-from-tsv.js
```

默认会自动发现 `input\<project>\*.tsv`，并把输出写到对应的 `projects\<project>\output`。你当前会使用：

- Project：`brandlimit_0617`
- 输入：`projects\brandlimit_0617\input\carlist.tsv`
- 输出：`projects\brandlimit_0617\output\from_tsv.md`
- 进度：`projects\brandlimit_0617\output\from_tsv_checkpoint.json`
- 摘要：`projects\brandlimit_0617\output\from_tsv_summary.md`

中断后继续运行同一条命令，不会覆盖已经落盘的 Markdown；脚本会读取 checkpoint，跳过已完成组合，然后继续追加。

也可以指定 project、输入和输出名称：

```powershell
.\run.ps1 src/scrape-from-tsv.js --project my_project --input carlist.tsv --output my_carlist.md
```

这里 `--input carlist.tsv` 会读取 `projects\my_project\input\carlist.tsv`；`--output my_carlist.md` 会写到 `projects\my_project\output\my_carlist.md`。

如果需要显式指定 project 根目录：

```powershell
.\run.ps1 src/scrape-from-tsv.js --projects-dir D:\Home\Scripts\4Afitment\projects --project brandlimit_0617
```

这会输出：

- `projects\my_project\output\my_carlist.md`
- 默认进度：`projects\my_project\output\from_tsv_checkpoint.json`
- 默认摘要：`projects\my_project\output\from_tsv_summary.md`

也可以完全指定路径：

```powershell
.\run.ps1 src/scrape-from-tsv.js --project carlist --input carlist.tsv --output cars.md --summary cars_summary.md --checkpoint cars_checkpoint.json
```

如果想从零重跑并清掉同名输出：

```powershell
.\run.ps1 src/scrape-from-tsv.js --project carlist --name from_tsv --reset
```

如果已经有一个 Markdown 结果文件，想跳过里面已有的品牌 / 车型组合：

```powershell
.\run.ps1 src/scrape-from-tsv.js --project brandlimit_0617 --skip-md from_tsv.md
```

`--skip-md` 会同时识别 Markdown 标题里的 `品牌 / 车型`，以及代码块 TSV 行里的 `year make model`。

也可以用 TSV 作为跳过清单。TSV 只有品牌列时，会跳过整个品牌；有品牌和车型两列时，只跳过指定车型：

```powershell
.\run.ps1 src/scrape-from-tsv.js --project .\projects\out_of_limitation_0617\ --skip-md .\projects\out_of_limitation_0617\input\carlist_toskip.tsv
```

如果没有指定 `--input`，并且当前 project 的 `input\carlist.tsv` 不存在，脚本会遍历 4AFitment 全部品牌/车型，再排除 skip 文件列出的品牌或车型。

结果会输出到：

- `projects/brandlimit_0617/output/from_tsv.md`
- `projects/brandlimit_0617/output/from_tsv_summary.md`
- `projects/brandlimit_0617/output/from_tsv_checkpoint.json`

找不到的品牌或车型会写在 `projects/brandlimit_0617/output/from_tsv_summary.md` 里说明原因。

## 如果页面控件识别不准

先运行检查脚本：

```powershell
.\run.ps1 src/inspect.js
```

它会把页面上疑似下拉控件打印出来。你可以把更准确的 CSS 选择器填到 `config.json`：

```json
{
  "selectors": {
    "yearFrom": ["select[name='year_from']"],
    "yearTo": ["select[name='year_to']"],
    "manufacturer": ["select[name='make']"],
    "model": ["select[name='model']"],
    "searchButton": ["button[type='submit']"],
    "copyButton": ["button:has-text('复制车型数据')"]
  }
}
```

脚本会优先使用这里写好的选择器；没有填写时，会按标签文字、placeholder、name、id、aria-label 自动猜测。

## 断点续跑

抓取过程中会持续写 `output/checkpoint.json`。如果中断，再运行 `src/scrape.js` 会跳过已经复制过的制造商 / 车型组合。

## 说明

4AFitment 页面通常需要登录。如果登录过期，重新运行登录脚本即可。
