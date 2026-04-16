# 远程参数调优运行方案

参数调优的 trial 数量较大，不建议在本地长时间运行。当前推荐流程是：

1. 本地只负责把代码和配置同步到远程服务器。
2. 用户登录远程服务器手动执行扫描命令。
3. 扫描完成后，本地从远程拉取结果并运行分析脚本。

本地脚本的 `run` 动作保留为应急入口，但不作为默认运行方式，避免从本地误启动长时间后台任务。

## 前提

- 本地可以通过 SSH 连接远程服务器，例如 `ssh user@host`。
- 远程服务器已经安装 Python 和项目运行依赖。
- 远程服务器上有完整行情数据目录，默认路径由 `config/backtest.default.json` 的 `data.data_root` 决定。
- 大型 `data/` 和 `results/` 默认不从本地同步，避免传输时间和磁盘占用失控。

## 配置

复制远程配置模板：

```bash
cp config/remote.example.env config/remote.env
```

编辑 `config/remote.env`：

```bash
REMOTE_HOST="user@host"
REMOTE_PROJECT_DIR="/home/user/gamma_scalping_v5"
REMOTE_PYTHON="python3"
REMOTE_VENV=".venv"

OPT_SPACE="config/optimization.default.json"
OPT_STAGE="vol_timing"
OPT_STUDY_ID="remote_vol_timing"
OPT_MAX_TRIALS=""
```

关键字段：

- `REMOTE_HOST`: SSH 目标，等同于 `ssh` 后面的主机参数。
- `REMOTE_PROJECT_DIR`: 项目同步到远程服务器的绝对路径。
- `REMOTE_VENV`: 远程虚拟环境路径，支持相对 `REMOTE_PROJECT_DIR` 的 `.venv`，也支持绝对路径；留空则直接使用 `REMOTE_PYTHON`。
- `OPT_SPACE`: 远程运行时使用的优化搜索空间文件。
- `OPT_STAGE`: 传给 `scripts/run_optimization.py --stage`。
- `OPT_STUDY_ID`: 远程结果目录名。
- `OPT_MAX_TRIALS`: smoke test 或抽样运行时限制 trial 数，正式运行留空。

`config/remote.env` 包含机器和账号信息，不应提交到 git。

## 首次远程准备

在远程服务器上创建项目目录和虚拟环境。以下命令只需要执行一次：

```bash
ssh user@host
mkdir -p /home/user/gamma_scalping_v5
cd /home/user/gamma_scalping_v5
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
```

随后按项目依赖情况安装运行依赖。若远程环境已经具备依赖，可以跳过虚拟环境步骤，并在 `config/remote.env` 中把 `REMOTE_VENV` 留空。

## 同步代码

```bash
scripts/remote_optimization.sh sync
```

该命令使用 `rsync` 同步当前项目到远程 `REMOTE_PROJECT_DIR`，默认排除：

- `.git/`
- `.venv/`
- `data/`
- `results/`
- Python 缓存和构建产物
- `config/remote.env`

若只用于很小的数据样本 smoke test，可以临时同步 `data/`：

```bash
SYNC_DATA=1 scripts/remote_optimization.sh sync
```

正式大规模调优不建议同步本地 `data/`，应直接把数据放在远程服务器。

## Smoke Test

先在远程跑一个 trial，验证代码、数据路径和依赖：

```bash
scripts/remote_optimization.sh smoke
```

查看远程日志：

```bash
scripts/remote_optimization.sh status
```

如果需要指定 smoke test 的 study：

```bash
OPT_STAGE=smoke OPT_STUDY_ID=remote_smoke OPT_MAX_TRIALS=1 scripts/remote_optimization.sh smoke
```

## 推荐运行流程

### 1. 本地同步项目

```bash
REMOTE_HOST=yangziqi@172.16.128.67 \
REMOTE_PROJECT_DIR=/home/yangziqi/strategy/gamma_scalping_v5 \
scripts/remote_optimization.sh sync
```

该命令只同步代码和配置，默认不上传 `data/` 和 `results/`。

### 2. 远程手动运行

登录远程服务器：

```bash
ssh yangziqi@172.16.128.67
cd /home/yangziqi/strategy/gamma_scalping_v5
```

如果远程使用系统 Python 且依赖已安装，直接运行：

```bash
python3 scripts/run_optimization.py \
  --space config/optimization.fine.json \
  --stage vol_entry_screen \
  --study-id vol_entry_screen_2018_2025
```

如果希望断开 SSH 后继续运行，可在远程服务器上手动使用 `nohup`：

```bash
mkdir -p logs
nohup python3 scripts/run_optimization.py \
  --space config/optimization.fine.json \
  --stage vol_entry_screen \
  --study-id vol_entry_screen_2018_2025 \
  > logs/optimization_vol_entry_screen_2018_2025.log 2>&1 &
```

查看远程进度：

```bash
tail -f logs/optimization_vol_entry_screen_2018_2025.log
```

日志会显示：

```text
optimization_start stage=vol_entry_screen total_trials=468 workers=32
optimization_progress completed=1/468 ... elapsed=... eta=...
```

停止远程扫描：

```bash
pkill -f 'scripts/run_optimization.py'
```

### 3. 本地拉取结果

扫描完成后，本地执行：

```bash
REMOTE_HOST=yangziqi@172.16.128.67 \
REMOTE_PROJECT_DIR=/home/yangziqi/strategy/gamma_scalping_v5 \
OPT_STUDY_ID=vol_entry_screen_2018_2025 \
scripts/remote_optimization.sh fetch
```

然后本地分析：

```bash
python3 scripts/analyze_optimization.py results/optimization/vol_entry_screen_2018_2025
```

## 可选：本地触发远程后台运行

默认不推荐使用本节方式。只有在明确需要由本地触发远程后台任务时，再执行：

```bash
scripts/remote_optimization.sh run
```

该命令会先同步代码，再通过 SSH 在远程后台启动：

```bash
python3 scripts/run_optimization.py \
  --space config/optimization.default.json \
  --stage "$OPT_STAGE" \
  --study-id "$OPT_STUDY_ID"
```

远程日志默认写入：

```text
{REMOTE_PROJECT_DIR}/logs/optimization_{OPT_STUDY_ID}.log
```

查看最近日志：

```bash
scripts/remote_optimization.sh status
```

## 拉取结果

```bash
scripts/remote_optimization.sh fetch
```

结果会同步到本地：

```text
results/optimization/{OPT_STUDY_ID}/
```

建议优先检查：

- `summary.csv`
- `failed.csv`
- `best.json`
- `best_sharpe.json` — 夏普比率最优 trial
- `best_return.json` — 年化收益率最优 trial
- `best_combined.json` — 综合评分最优 trial
- `analysis_report.txt` — 完整分析报告
- `runs/*/metrics.json`

## 结果分析

拉取结果后，运行分析脚本：

```bash
python3 scripts/analyze_optimization.py results/optimization/{OPT_STUDY_ID}
```

分析报告包含：

1. **按年化收益率排名的 Top trial**
2. **按夏普比率排名的 Top trial**
3. **按综合评分排名的 Top trial**
4. **帕累托前沿**（年化收益率 vs 夏普比率）
5. **参数敏感性分析** — 每个参数对各指标的影响范围
6. **推荐参数集** — 年化收益率与夏普比率的加权最优
7. **分段对比** — 如果配置了多个数据 split，用于评估参数过拟合风险；当前主扫描使用 `2018-01-01` 至 `2025-12-31` 单一区间，不区分训练集和测试集
8. **统计摘要** — 全部成功 trial 的描述性统计

评分公式（综合评分 `score`）：

```
score = annual_return + 0.5 * sharpe_ratio - 0.8 * |max_drawdown| + 0.3 * sortino_ratio
        + 0.2 * gamma_theta_pnl / initial_cash - 0.2 * |residual_pnl| / initial_cash
```

可通过 `--top-n` 控制报告中的 trial 数量，`--output` 指定报告输出路径。

## 常见覆盖

使用不同远程配置文件：

```bash
REMOTE_CONFIG=config/remote.prod.env scripts/remote_optimization.sh run
```

临时覆盖 stage 和 study id：

```bash
OPT_STAGE=vol_entry_screen OPT_STUDY_ID=vol_entry_screen_2018_2025 scripts/remote_optimization.sh run
```

限制 trial 数：

```bash
OPT_MAX_TRIALS=20 scripts/remote_optimization.sh run
```

## 运行约定

- 远程调优只通过 `scripts/run_optimization.py` 入口执行，不绕过统一优化配置。
- 本地同步只负责代码和配置，不默认同步大数据和历史结果。
- 每次正式运行使用新的 `OPT_STUDY_ID`，避免不同搜索空间写入同一个结果目录。
- `resume=true` 时，相同 study 中已成功 trial 会被跳过；修改搜索空间后建议换新的 `OPT_STUDY_ID`。
