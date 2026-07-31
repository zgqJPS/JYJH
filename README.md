# Market Advisor - 自适应市场分析系统

## 系统概述
每日自动获取数据 → 分析市场 → 总结规律 → 预判走势 → 自我修正

## 核心架构（砸盘系数主导）

```
数据采集 → 砸盘系数计算(主导) → 市场周期判断 → 概念分析(选股宝) → 综合预测 → 自我修正
    ↑                                                                          ↓
    └────────────────────── 权重自适应调整 ←──────────────────────────────────────┘
```

## 砸盘系数（Smash Coefficient）- 核心主导因素

### 算法原理
- 基于连板梯度的板级晋升比率
- 公式: `smash_coef = mean(今日N板数/昨日N-1板数) × 10`
- 数值越高说明市场抛压越重，连板晋级越困难

### 市场周期判断（砸盘系数主导）
| 周期阶段 | 砸盘系数 | 最高连板 | 涨停总数 | 操作策略 |
|----------|----------|----------|----------|----------|
| 高潮期 | ≥4.5 | ≥6 | ≥60 | 积极参与龙头 |
| 主升期 | ≤3.0 | ≥4 | ≥45 | 主动进攻，提高仓位 |
| 补涨期 | ≤3.0 | ≤4 | ≥60 | 关注低位补涨 |
| 低迷/轮动 | 其他 | - | - | 保守观望 |

### 个股评分影响
- 砸盘系数 > 7: market_score -5（严控仓位、避免追高、止损放宽2%）
- 砸盘系数 < 4: market_score +3（适合主动进攻，可适当提高仓位）

### 自适应修正
- smash_factor 纳入所有预测类型的因素映射
- 通过 EWMA 指数加权移动平均平滑调整权重
- 连续误判时自动降权，连续正确时自动升权

## 模块说明

| 模块 | 文件 | 功能 |
|------|------|------|
| 数据库 | db.py | SQLite操作，含砸盘系数存储与查询 |
| 配置 | config.py | 系统配置，含SMASH_CONFIG阈值/权重 |
| 砸盘系数 | smash_coefficient.py | 核心算法：板级晋升比率、趋势分析、信号判断 |
| 数据获取 | data_fetcher.py | akshare + 选股宝双数据源 |
| 选股宝 | xgb_fetcher.py | 概念标签和涨停原因 |
| 市场分析 | market_analyzer.py | 砸盘系数分析 + 周期判断 + 概念热度 |
| 模式识别 | pattern_recognizer.py | 砸盘模式识别，周期核心参数 |
| 预测引擎 | predictor.py | 砸盘系数主导的综合预测 |
| 自我修正 | self_corrector.py | EWMA权重自适应调整 |
| 预测追踪 | prediction_tracker.py | 含砸盘系数预测验证 |
| 报告生成 | reporter.py | 含砸盘系数专属报告板块 |
| 主入口 | main.py | daily流程含Step 0（砸盘系数计算）|

## 使用方法

```bash
# 每日完整分析流程
python main.py daily

# 回测验证
python main.py backtest --max=30

# 获取实时数据
python main.py fetch
```

## 数据源
- **akshare**: 涨停基础数据（价格/封单/连板数/换手率）
- **选股宝(XuanGuBao)**: 涨停概念标签、涨停原因
- 主板股票过滤（60/00开头），排除ST概念

## 目录结构
```
market_advisor/
├── main.py                 # 主入口
├── config.py               # 配置（含SMASH_CONFIG）
├── db.py                   # 数据库操作
├── smash_coefficient.py    # 砸盘系数核心算法
├── data_fetcher.py         # 数据采集
├── xgb_fetcher.py          # 选股宝概念获取
├── market_analyzer.py      # 市场分析
├── pattern_recognizer.py   # 模式识别
├── predictor.py            # 预测引擎
├── self_corrector.py       # 自适应修正
├── prediction_tracker.py   # 预测追踪验证
├── reporter.py             # 报告生成
├── knowledge/              # 知识库
└── reports/                # 分析报告
```
