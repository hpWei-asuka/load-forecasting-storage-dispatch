# 用电负荷预测与储能调度优化

Python 3.12 本地分析工具。读取 UCI 单户用电数据，用前一日同小时负荷预测次日需求，再以 PuLP/CBC 求解电池充放电计划，并与无储能、固定规则两种策略一起回测。

## 功能

| 环节 | 已实现内容 |
| --- | --- |
| 数据处理 | 显式下载公开数据，检查缺失、重复和冲突记录，按小时聚合 |
| 负荷预测 | 前一日同小时基线，按全部有效小时计算 MAE、RMSE |
| 储能调度 | PuLP/CBC 混合整数规划，与无储能、固定规则策略对照 |
| 真实回测 | 冻结计划后检查真实负荷约束，保留不可执行时段及失败原因 |
| 结果导出 | CSV、JSON、预测曲线、调度图、费用图和运行来源记录 |

## 项目材料

- [四页项目报告](最终材料/01_项目报告.pdf) · [文字版](最终材料/01_项目报告.md)
- [完整实验结果](最终材料/03_实验结果/README_结果口径.md) · [流程图与预测诊断](最终材料/05_图示与诊断/README.md)
- [最终材料压缩包](最终材料.zip) · [文件校验清单](最终材料/文件校验清单.sha256)

![预测与回测流程](最终材料/05_图示与诊断/fig_roadmap.png)

## 运行

安装 Python 3.12 和 [uv](https://docs.astral.sh/uv/)，在项目根目录执行：

```powershell
git clone https://github.com/hpWei-asuka/load-forecasting-storage-dispatch.git
cd load-forecasting-storage-dispatch
uv sync --locked
uv run load-storage doctor
uv run load-storage download-data
uv run load-storage run --config configs/example.toml
```

`doctor` 实际调用 CBC 求解已知最优值的小模型，并检查中文和负号能否绘制。缺少字体时，可安装 Microsoft YaHei 或 Noto Sans CJK SC。

`download-data` 显式联网，从 [UCI](https://archive.ics.uci.edu/dataset/235/individual+household+electric+power+consumption) 下载公开数据，不覆盖已有文件。也可手动将数据放到 `data/raw/household_power_consumption.txt`。下载记录保存来源、时间和 SHA-256；手动提供的数据只记录可核实的信息。运行实验不联网。

每次实验新建 `outputs/<实验名>_<UTC时间>_<随机标识>/`。全部请求日期正常完成时返回退出码 0；有数据缺失、求解失败或回测不可执行时，保留结果与诊断并返回非零状态。

## 配置与评估口径

日期、电价、电池和固定规则窗口均在 `configs/example.toml` 中设置。默认测试期为 2010-01-04 至 2010-01-10，仅按原始观测完整性选择，是 2010 年内首个连同前一日均完整的连续七日区间。

- 历史数据最多前向填补连续缺失的 5 分钟，不引用未来值；冲突时间戳会阻断填补。
- 每小时取分钟功率的平均值，单位 kW；乘以 1 h 后得到电量 kWh。正式回测要求一天 24 小时、每小时都有 60 个原始有效分钟观测。
- 两种储能策略只读取预测负荷。计划确定后才使用目标日真实负荷，检查约束并核算费用。
- 模型限制容量、功率、充放电互斥、效率、非负购电和初末电量相等。每天单独求解，不跨日衔接电池状态。
- 若计划放电超过真实负荷，回测记为不可执行，费用留空；不截断放电后继续使用原计划的结果。

费用汇总使用三策略共同有效的日期，节省率按总费用计算。它是事后筛选可执行日期后的条件比较，不代表整个测试期表现。预测指标单独按有效预测样本统计。没有共同有效日期时不报告费用收益，失败结果不按 0 元计入。

## 输出

| 文件 | 内容 |
| --- | --- |
| `config.toml`、`metadata.json` | 配置快照、数据来源与哈希、依赖及 CBC 版本、代码版本 |
| `data_quality.json`、`hourly.csv` | 数据质量记录、小时负荷及原始观测与填补计数 |
| `predictions.csv` | 小时预测和事后真实负荷 |
| `schedule.csv` | 充放电功率、小时首末电量、计划及实际购电功率 |
| `cost_comparison.csv`、`summary.json` | 逐日费用、有效性、失败原因、预测指标和共同日期汇总 |
| `diagnostics.json` | 逐时约束违反详情 |
| `figures/` | 预测、调度和费用图，PNG 与 SVG |
| `run.log`、`solver_logs/` | 运行与求解日志 |

CSV 使用带 BOM 的 UTF-8，便于在 Windows 表格软件中打开。实现细节见 [技术方案](docs/技术方案.md)。

## 已验证结果

在 Windows、Python 3.12.10、CBC 2.10.3 和 `uv.lock` 锁定环境下，90 项 pytest 测试通过。默认七日共 168 个小时的预测 MAE 为 0.701245 kW，RMSE 为 0.991260 kW。

默认实验只有 2010-01-05、2010-01-10 两日三策略共同有效，覆盖率为 2/7（28.57%）。这两日无储能、固定规则、优化策略的购电费用分别为 53.861140、51.324298、50.050614 元，优化节省率为 7.0747%。另外五日优化计划在预测条件下求得最优，但放电超过真实负荷，回测不可执行，因此默认实验返回退出码 1。

电价和电池均为假设情景，费用只包括购电运行费用。上述两日结果不能解释为全周收益、真实电费账单或投资回报。

## 电池参数情景对照

复制 `configs/example.toml` 为 `configs/capacity3.toml`，将 `capacity_kwh`、`max_energy_kwh`、`initial_energy_kwh` 分别改为 `3.0`、`2.5`、`1.5`，其余不变：

```powershell
uv run load-storage run --config configs/capacity3.toml
```

这次对照同时改变容量、可用电量范围和初始电量，不能视为只改变容量的敏感性分析。已保存的两组实验共有的三策略有效日期仅 2010-01-10，该日优化费用分别为 26.558437 元（5 kWh 情景）和 27.511068 元（3 kWh 情景）。比较时应使用这个共同日期，不能直接比较覆盖日不同的总费用。

## 测试与局限

```powershell
uv run pytest
./tests/Test-ProductDesign.ps1
./tests/Test-TechnicalPlan.ps1
```

测试使用人工数据检查单位换算、因果清洗、时间对齐、已知最优解、设备约束、失败处理和端到端复现。真实实验的失败明细保存在输出目录。

当前预测只是基线，单户负荷波动下的可执行覆盖率较低。项目未实现滚动控制、预测不确定性约束、光伏、售电或真实设备接入，也未计电池投资、老化和需量费用。
