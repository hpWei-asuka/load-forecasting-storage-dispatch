# 用电负荷预测与储能调度优化

基于 Python 3.12 的命令行工具：用前一日同小时负荷预测次日用电，以 PuLP/CBC 优化电池充放电，并回测无储能、固定规则和优化策略。

## 快速开始

安装 Python 3.12 和 [uv](https://docs.astral.sh/uv/)，执行：

```powershell
git clone https://github.com/hpWei-asuka/load-forecasting-storage-dispatch.git
cd load-forecasting-storage-dispatch
uv sync --locked
uv run load-storage doctor
uv run load-storage download-data
uv run load-storage run --config configs/example.toml
```

`doctor` 检查 Python、CBC 实际求解和中文绘图。绘图需要 Microsoft YaHei 或 Noto Sans CJK SC 字体。

原始数据来自 [UCI Individual Household Electric Power Consumption](https://archive.ics.uci.edu/dataset/235/individual+household+electric+power+consumption)，约 133 MB。下载命令自动创建 `data/raw/`，已有文件不会覆盖；也可手动放入 `data/raw/household_power_consumption.txt`。完成下载后，实验可离线运行。

默认测试期为 2010-01-04 至 2010-01-10。默认情景有五日回测不可执行，程序保存结果后返回退出码 1；全部请求日期成功时返回 0。

## 配置与输出

- `configs/example.toml`：默认 5 kWh 电池情景，包含日期、电价、设备参数和固定规则时段。
- `configs/capacity3.toml`：3 kWh 参数对照，运行时替换 `--config` 路径。
- 每次实验写入独立的 `outputs/<run_id>/`，包含预测、调度、逐日费用、失败诊断、图表、配置和来源记录。

数据和运行输出由程序生成，不纳入仓库。CSV 使用带 BOM 的 UTF-8，可用表格软件直接打开。

## 项目结构

| 路径 | 内容 |
| --- | --- |
| `src/load_storage/` | 数据清洗、预测、优化、回测、绘图和命令行入口 |
| `configs/` | 两份可运行的实验配置 |
| [技术方案](docs/技术方案.md) | 数据口径、模型公式、配置示例和输出说明 |
| `tests/` | 功能测试与技术文档检查 |
| `.github/workflows/tests.yml` | Windows 和 Ubuntu 自动检查 |
| `pyproject.toml`、`uv.lock` | 项目入口、依赖声明和版本锁定 |

## 评估口径与局限

历史只做最多 5 分钟的因果前向填补；正式回测要求每天 24 小时、每小时有 60 个原始有效分钟。预测和计划只使用目标日前已知的信息，真实负荷仅用于计划确定后的回测。

放电超过真实负荷时，该日计划记为不可执行，费用留空。费用只在三种策略共同有效的日期汇总，并报告覆盖率，不能将失败日当作零费用。

此前默认实验的 168 小时预测 MAE 为 0.701245 kW，RMSE 为 0.991260 kW。共同有效日仅为 2010-01-05、2010-01-10（2/7，28.57%）；两日优化购电费用 50.050614 元，相对无储能的 53.861140 元减少 7.0747%。这是事后筛选有效日期的条件比较，不能代表全周收益。可按上述命令重新生成实验结果。

3 kWh 对照同时改变容量、电量上限和初始电量，不是单因素敏感性分析。电价和电池均为假设情景；费用未计投资、老化与需量费用，当前未实现滚动控制、光伏、售电或真实设备接入。

## 验证

```powershell
uv run pytest
uv run -- pwsh -NoProfile -File tests/Test-TechnicalPlan.ps1
```

第二条命令用于 Windows，需要 PowerShell 7。GitHub Actions 在 Windows 和 Ubuntu 上运行功能测试与环境检查。
