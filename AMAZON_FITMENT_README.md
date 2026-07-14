# Amazon.de 五级车型下拉框遍历脚本

脚本：`amazon_de_fitment_scraper.py`

目标商品页：

`https://www.amazon.de/Protection-Tarpaulin-Price-Performance-Protect-Polyester/dp/B07RLG4NZV/?th=1`

目标弹窗优先使用用户给出的 XPath：`//*[@id="a-popover-2"]`。由于 Amazon 的
`a-popover-N` 编号可能变化，脚本也会根据“Enter a new vehicle”文案后备识别。

## 安装

在本目录打开 PowerShell：

```powershell
python -m pip install -r requirements-amazon.txt
```

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
