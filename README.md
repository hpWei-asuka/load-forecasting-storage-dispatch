# 用电负荷预测与储能调度优化

一个可复现的 Python 项目：根据历史家庭用电记录预测次日负荷，在分时电价下优化电池充放电，再用真实负荷回测，比较无储能、固定规则和优化调度的购电费用与计划可执行性。

[阅读项目报告（PDF）](docs/项目报告.pdf) · [技术方案](docs/技术方案.md) · [实验记录](docs/experiments/)

## 项目做什么

程序以 UCI 公开家庭用电数据为输入，完成四个步骤：

1. **处理数据**：清洗分钟记录，聚合成小时平均功率，检查缺失和观测完整性。
2. **预测负荷**：以前一日同小时用电作为基线，预测次日 24 小时负荷。
3. **安排充放电**：通过 PuLP/CBC 求解混合整数线性规划，在电池功率、电量和效率约束下最小化预测购电费用。
4. **回测与比较**：冻结日前计划，用真实负荷检查能否执行，再比较三种策略在共同有效日期上的费用。

目标日真实负荷只参与回测。若计划放电超过真实负荷，该日记为不可执行，费用留空，并保留失败原因。

## 快速开始

安装 Python 3.12 和 [uv](https://docs.astral.sh/uv/)，在终端执行：

```powershell
git clone https://github.com/hpWei-asuka/load-forecasting-storage-dispatch.git
cd load-forecasting-storage-dispatch
uv sync --locked
uv run load-storage doctor
uv run load-storage download-data
uv run load-storage run --config configs/example.toml
```

`doctor` 检查 Python、CBC 实际求解和中文绘图。绘图需要 Microsoft YaHei 或 Noto Sans CJK SC 字体。

原始数据来自 [UCI Individual Household Electric Power Consumption](https://archive.ics.uci.edu/dataset/235/individual+household+electric+power+consumption)，约 133 MB。下载命令自动创建 `data/raw/`，已有文件不会覆盖；也可手动放入 `data/raw/household_power_consumption.txt`。下载完成后，实验可离线运行。

**默认实验会保存全部结果后返回退出码 1**，表示七天中有五天的计划无法通过真实负荷回测。这是本例的实验结果。退出码 0 表示全部请求日期成功，2 表示运行错误。

使用另一组电池参数：

```powershell
uv run load-storage run --config configs/capacity3.toml
```

每次实验写入独立的 `outputs/<run_id>/`，包含预测、调度、逐日费用、失败诊断、图表、配置和来源记录。3 kWh 情景也会返回退出码 1，其中四天至少有一种策略回测失败。CSV 使用带 BOM 的 UTF-8，可直接用表格软件打开。

## 示例结果与适用范围

以下结果由仓库代码在 2010-01-04 至 2010-01-10 的公开数据上重现。

| 默认 5 kWh 情景 | 结果 |
| --- | --- |
| 预测样本 | 7 日，168 小时 |
| 预测 MAE / RMSE | 0.701245 / 0.991260 kW |
| 三种策略共同有效日期 | 2010-01-05、2010-01-10 |
| 费用比较覆盖率 | 2/7，28.57% |
| 无储能 / 固定规则 / 优化调度的两日购电费用 | 53.861140 / 51.324298 / 50.050614 元 |
| 优化调度相对无储能的两日节省率 | 7.0747% |

**7.0747% 仅是两天共同有效样本上的条件比较，不能代表全周收益。** 默认情景的七个日前优化均求得最优解，但其中五天的放电计划超过真实负荷；预测条件下最优，并不保证实际可执行。

3 kWh 对照同时改变容量、电量上限和初始电量，不是单因素敏感性分析。电价和电池均为假设情景，费用未计投资、老化与需量费用。当前使用简单预测基线和独立日回测，尚未实现滚动控制、光伏、售电或真实设备接入。

## 文件与文档

| 路径 | 内容 |
| --- | --- |
| `src/load_storage/` | 数据、预测、优化、回测、绘图和命令行入口 |
| `configs/` | 默认 5 kWh 与 3 kWh 两份实验配置 |
| [项目报告](docs/项目报告.pdf) | 摘要、数据口径、公式、实验分析、局限和复现说明 |
| [报告源码](docs/report/main.tex) | 可编辑的 LaTeX 正文与同目录下的两张矢量图 |
| [实验记录](docs/experiments/) | 两组逐日费用与状态、小时预测、示例日计划、来源及校验摘要 |
| [技术方案](docs/技术方案.md) | 配置说明、模块设计和输出字段 |
| `tests/` | 功能、实验记录与文档一致性检查 |
| `.github/workflows/tests.yml` | Windows 和 Ubuntu 自动检查 |

仓库仅保留核对报告所需的小型实验记录。原始数据和完整运行输出按上述命令生成，不重复上传。

## 验证与报告编译

```powershell
uv run pytest
uv run -- pwsh -NoProfile -File tests/Test-TechnicalPlan.ps1
```

第二条命令需要 PowerShell 7。GitHub Actions 在 Windows 和 Ubuntu 上运行功能测试与环境检查。

报告由 XeLaTeX 编译，使用 Windows 宋体、黑体和 Times New Roman；其他系统可安装相应字体，或调整 `main.tex` 的字体设置。安装包含 `ctex` 的 TeX 发行版后执行两遍：

```powershell
cd docs/report
xelatex -interaction=nonstopmode -halt-on-error main.tex
xelatex -interaction=nonstopmode -halt-on-error main.tex
```

编译结果为该目录下的 `main.pdf`；仓库提供的阅读版为 `docs/项目报告.pdf`。
