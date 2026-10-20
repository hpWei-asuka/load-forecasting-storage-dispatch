# 图示与预测诊断

fig_roadmap.drawio为流程图源文件，PDF和PNG为导出版本。图中目标日真实负荷只进入事后回测。

fig_prediction_diagnostics对默认实验168小时预测与真实观测作比较，包含散点、边缘分布和误差直方图。配套CSV保留原始字段并增加error_kw（预测减真实），JSON记录指标和输入文件SHA-256。两份图都未加入模拟观测。

在02_项目源码目录重绘诊断图：

```powershell
uv run python examples/plot_diagnostics.py --input ../03_实验结果/01_默认5kWh全部结果/predictions.csv --output ../05_图示与诊断/fig_prediction_diagnostics
```

原程序导出的时间序列、费用和调度图仍在03_实验结果各实验的figures目录中。
