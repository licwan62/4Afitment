# Amazon.de 五级车型下拉框遍历脚本

脚本：`amazon_de_fitment_scraper.py`

目标商品页：

`https://www.amazon.de/Protection-Tarpaulin-Price-Performance-Protect-Polyester/dp/B07RLG4NZV/?th=1`

目标弹窗优先使用用户给出的 XPath：`//*[@id="a-popover-2"]`。由于 Amazon 的
`a-popover-N` 编号可能变化，脚本也会根据“Enter a new vehicle”文案后备识别。

## 安装

在仓库根目录打开 PowerShell，并创建共用虚拟环境（首次运行时执行）：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r .\scrapers\amazon-de\requirements.txt
cd .\scrapers\amazon-de
```

以后重新打开 PowerShell 时，只需先在仓库根目录运行
`.\.venv\Scripts\Activate.ps1`，无需重复安装依赖。

Selenium 4 会通过 Selenium Manager 自动查找 Chrome 和匹配的驱动。

## 先检查控件

```powershell
python .\amazon_de_fitment_scraper.py --inspect
```

脚本会尝试自动打开车型弹窗。如果没有成功，会提示你在浏览器里手动打开截图中的
弹窗，再回到 PowerShell 按 Enter。

## 正式遍历

```powershell
python .\amazon_de_fitment_scraper.py
```

推荐直接使用以上默认参数。脚本支持的全部命令行参数如下：

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

遍历顺序为：

1. Vehicle Type
2. Make
3. Model
4. Variant
5. Engine Type

输出：

- `output/amazon_de_vehicles.csv`：每发现一条立即追加，意外中断也不会丢失已写记录。
- `output/amazon_de_vehicles.json`：每 50 条及正常完成时更新。
- `output/amazon_de_vehicles.errors.log`：失败分支和原因。

再次运行时会读取已有 CSV 并跳过重复组合。小范围测试可用：

```powershell
python .\amazon_de_fitment_scraper.py --max-rows 20
```

如果 Amazon 显示验证码，脚本不会绕过验证码；请在可视浏览器里手动完成后按 Enter。
不要同时启动两个使用同一 `.amazon_selenium_profile` 的脚本实例。
