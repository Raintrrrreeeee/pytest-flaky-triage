# pytest-flaky-triage

[![CI](https://github.com/Raintrrrreeeee/pytest-flaky-triage/actions/workflows/ci.yml/badge.svg)](https://github.com/Raintrrrreeeee/pytest-flaky-triage/actions/workflows/ci.yml)
[![Release](https://img.shields.io/github/v/release/Raintrrrreeeee/pytest-flaky-triage)](https://github.com/Raintrrrreeeee/pytest-flaky-triage/releases)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![pytest >= 7.4](https://img.shields.io/badge/pytest-%3E%3D7.4-0A9EDC.svg)](https://docs.pytest.org/)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey.svg)](#)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

[English](README.md) | **简体中文**

一个面向 Python `pytest` 测试套件的**三阶段不稳定测试（Flaky Test）检测、统计根因推断与 AST 静态源码归责工具**。

市面上常见的重跑插件（如 `pytest-repeat`、`pytest-flakefinder`、`pytest-random-order`）只能告诉你某个测试在多次运行中“有没有挂过”；而 **`pytest-flaky-triage`** 通过控制变量实验矩阵、前置污染源测试集合求交、以及 Python 抽象语法树（AST）跨文件静态扫描，不仅能自动诊断**测试为什么不稳定**，还能揪出**是哪个前置测试污染了环境**，并精准判定**缺陷究竟出在测试代码（`Test Side`）还是业务代码（`Code Side`）的具体哪一行**。

```text
+-------------------------------------------------------------------------+
| 第一阶段：四维控制变量矩阵执行引擎 (scripts/detect.sh)                  |
|   repeat（重复）-> hashseed（哈希）-> order（顺序）-> isolated（隔离）  |
|   Pytest Sideband Hook 实时插件 + 进程组超时保护控制器                  |
+------------------------------------+------------------------------------+
                                     |  results.csv & run_meta.csv
                                     v
+-------------------------------------------------------------------------+
| 第二阶段：统计评分与污染源因果推断 (src/analysis/)                      |
|   抗崩溃稳定性得分计算 + 正交实验条件归因                               |
|   双向隔离效应检测 + 前置污染源测试集合求交 (suspected_polluters)       |
+------------------------------------+------------------------------------+
                                     |  diagnosis.json
                                     v
+-------------------------------------------------------------------------+
| 第三阶段：Python AST 静态源码消歧与报告生成 (src/report/)               |
|   跨文件 3 层调用链追踪 + conftest.py 夹具解析 + setup_method 追踪       |
|   测试侧 vs. 业务侧责任归属 -> 生成英文或中文 Markdown 报告 (report.md) |
+-------------------------------------------------------------------------+
```

---

## 诊断报告输出预览（支持 `--lang zh` 中文报告）

```markdown
| 测试名称                                 | 判定结果 | 稳定性得分   | 重复基线 (Repeat) | 哈希种子 (Hashseed) | 执行顺序 (Order) | 单测隔离 (Isolated) | 怀疑根因                                 | 责任归属           | 判定依据   |
| :--------------------------------------- | :------: | :----------: | :---------------: | :-----------------: | :--------------: | :-----------------: | :--------------------------------------- | :----------------: | :--------- |
| `test_taxes_format`                      | `flaky`  | 0.12 (3/25)  |       0/10        |         3/8         |       0/6        |         0/1         | `hash_order_sensitivity` (哈希顺序敏感)   | 测试侧 (Test Side) | 运行时+AST |
| `test_shared_cart_has_no_prior_laptop`   | `flaky`  | 0.12 (3/25)  |       0/10        |         0/8         |       2/6        |         1/1         | `shared_state_interference` (共享状态污染) | 测试侧 (Test Side) | 运行时+AST |
| `test_get_daily_quote`                   | `flaky`  | 0.16 (4/25)  |       2/10        |         2/8         |       0/6        |         0/1         | `unseeded_randomness` (未固定随机种子)    | 业务侧 (Code Side) | AST 消歧 * |
| `test_checkout_deducts_mouse`            | `flaky`  | 0.44 (11/25) |       4/10        |         4/8         |       2/6        |         1/1         | `race_conditions` (并发时间竞态)          | 业务侧 (Code Side) | AST 消歧 * |
| `test_add_item_to_cart_is_deterministic` | `stable` | 1.00 (25/25) |       10/10       |         8/8         |       6/6        |         1/1         | —                                        |         —          | —          |
```

在汇总表下方的诊断详情中，工具还会直接列出**疑似前置污染源测试**与**具体源码位置**：
- **`shared_state_interference`（共享状态污染）**：`疑似前置污染源测试：test_new_cart_is_empty` -> `tests/test_cart.py:123 读写/修改了模块级共享对象 shared_test_cart（声明于 tests/test_cart.py:7）`
- **`unseeded_randomness`（未固定随机种子）**：`src/ecommerce/cart.py:56 调用了 random.choice(...) 且未固定随机种子`
- **`race_conditions`（并发时间竞态）**：`src/ecommerce/cart.py:66, 70, 74 使用了未同步的并发/计时原语 (time.sleep, unjoined threading.Thread)`

---

## 核心功能亮点

### 第一阶段：四维控制变量矩阵执行引擎（`flaky_triage.runner`、`scripts/detect.sh` & `flaky_plugin.py`）
- **单因子正交实验设计**：在四个正交维度（固定条件重复 `repeat`、切换哈希种子 `hashseed`、打乱执行顺序 `order`、单测独立运行 `isolated`）下运行目标测试套件，每次只改变一个变量，从实验设计层面隔离不同根因。
- **内置 Pytest Sideband Hook 插件**：直接挂载 `pytest` 生命周期钩子（`setup` / `call` / `teardown`），将每个测试的真实执行结果实时写入独立的结构化数据流，不受目标项目 `pytest.ini` 配置（如 `-q` 静默输出、进度条插件、多行报错堆栈、带空格或特殊符号的参数化测试名）干扰；同时保留加固版日志解析器作为兜底。
- **内存级 `O(N)` 顺序重排与冲突插件屏蔽**：在 `pytest_collection_modifyitems` 阶段按指定顺序文件直接重排测试，并自动屏蔽目标环境中可能干扰实验控制的第三方插件（如 `pytest-randomly`、`pytest-xdist`、`pytest-sugar`、`pytest-cov`、`pytest-rerunfailures`、`pytest-order`）。
- **macOS / Linux / Windows 跨平台进程树超时保护**：每次运行均在独立进程组中启动并关闭 `stdin`，超时后自动清理整棵进程树（退出码 `124`），在 **macOS、Linux 与 Windows（PowerShell / CMD / Git Bash）** 下均可防止死循环或残留后台线程卡死终端。
- **大型项目加速与按需筛选**：支持 `--target SUBPATH` 指定子目录或文件、`-k EXPR` 关键词过滤、`--python PATH` 指定外部虚拟环境解释器，以及 `--isolated-scope smart` 智能隔离模式（仅对在套件运行中失败或波动的测试执行单测进程隔离，大幅缩短大型项目耗时）。

### 第二阶段：统计稳定性评分与污染源定位（`src/analysis/`）
- **抗崩溃干扰的稳定性评分**：基于有效运行计算 `passed / (passed + failed + error)` 稳定性得分；当某轮运行发生中途崩溃、超时或未跑完时，已完成测试仍计入得分，但该轮会被严格排除在因果推断之外，杜绝残缺数据导致的误判。
- **双向隔离效应识别（Bidirectional Isolation）**：不仅能识别**被污染受害者**（在全量套件中失败、单独隔离运行时通过），还能识别**隐式状态依赖者**（因为其他测试顺手初始化了环境而在套件中通过，单独隔离运行时反而失败）。
- **自动锁定“污染源真凶”（`suspected_polluters`）**：记录每轮全量运行中所有测试的精确出场顺位，自动计算失败场次共同前置测试与成功场次前置测试的差集（`common_on_fail - ever_on_pass`），并结合双向互污染精炼算法，直接点名把共享状态搞脏的前置测试名称。
- **小样本防误判保护**：针对低概率随机缺陷在有限次 `repeat` 基线中碰巧全部通过的情况，引入跨扰动翻转保护，避免将其误判为确定性的哈希或顺序缺陷。

### 第三阶段：Python AST 静态源码消歧与诊断报告（`src/report/`）
- **运行时证据 + AST 静态消歧**：当运行时表现存在歧义（例如“未固定种子的随机数”和“多线程竞态条件”都会在相同种子和相同顺序下忽过忽挂）时，自动扫描测试与业务源码的抽象语法树（AST），完成根因消歧并判定责任归属（`Test Side` 测试侧 vs. `Code Side` 业务代码侧）。
- **深度跨文件调用链与夹具追踪**：
  - 支持跨模块追踪导入的函数、方法与类实例化调用链（深度可达 **3 层调用**，含相对导入）。
  - 自动解析当前测试文件及各级父目录 `conftest.py` 中的 `@pytest.fixture` 夹具函数。
  - 追踪测试类中的 `setup_method` / `setUp` 初始化逻辑与 `self.<attr>` 属性读写。
  - 同时覆盖 `Test Side` 测试模块共享变量/夹具污染与 `Code Side` 业务模块全局变量（含 `global` 关键字修改）及类属性污染。
- **真实工程特征覆盖与防误报抑制**：
  - 识别 `random.*`、`uuid.uuid4`、`secrets.*`、`os.urandom`、`numpy.random`、`threading.Thread`、`ThreadPoolExecutor` 线程池、`asyncio.gather` 等常见模式。
  - 自动排除已固定常量随机种子（如 `random.seed(42)`）、已排序集合（`sorted(...)`）、路径拼接（`os.path.join`）以及显式调用了状态重置函数（`reset_*` / `clear_*`）的安全代码。
- **双格式可追溯输出**：同时生成供 CI/CD 流水线读取的 `diagnosis.json`（包含最终消歧结论 `resolution`）和供开发者阅读的 `report.md`（优先展示失败场次日志链接，并精确标注源码 `文件名:行号`）。

---

## 安装与快速体验指南

运行环境要求：**Python 3.10+**，原生支持 **macOS**、**Linux** 与 **Windows（PowerShell / CMD / Git Bash）**。

### 方式一：仅安装命令行工具（轻量级 `pip install`，不会下载 `scenario/`）

如果你只想把 `pytest-flaky-triage` 当作命令行工具安装到自己的环境中去检测实际项目，通过 `pip install` 安装时**只会打包核心诊断引擎**（`flaky_triage`、`analysis`、`report`），**不会**把演示用的 `scenario/` 靶场或测试夹具塞进你的 Python 环境：

**macOS / Linux：**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install git+https://github.com/Raintrrrreeeee/pytest-flaky-triage.git

# 安装后即可直接对任意 pytest 项目运行诊断：
pytest-flaky-triage --project /path/to/your/project --out ./triage-out --lang zh
```

**Windows (PowerShell)：**
```powershell
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
pip install git+https://github.com/Raintrrrreeeee/pytest-flaky-triage.git

# 安装后即可直接对任意 pytest 项目运行诊断：
pytest-flaky-triage --project C:\path\to\your\project --out .\triage-out --lang zh
```

#### 可选步骤：按需单独下载 `scenario/` 官方演示靶场练手

如果你已经用上面的 `pip install` 安装好了命令，想先用官方自带的 18 个缺陷测试靶场（`scenario/`）熟悉一下工具输出，只需执行以下几行命令单独拉取 `scenario/` 文件夹即可：

```bash
git clone --depth 1 --filter=blob:none --sparse https://github.com/Raintrrrreeeee/pytest-flaky-triage.git flaky-demo
cd flaky-demo
git sparse-checkout set --no-cone /scenario /docs

# 使用已安装的 CLI 工具检测下载好的 scenario 靶场（约 10 秒，也可直接跑 pytest-flaky-triage --demo --lang zh）：
pytest-flaky-triage --project ./scenario --out ./out --lang zh
cat ./out/report.md

# 若已跑完英文版想秒切中文版（无需重跑 42 轮 pytest）：
# pytest-flaky-triage --project ./scenario --out ./out --lang zh --report-only
```

---

### 方式二：克隆完整仓库（包含源码、演示靶场与 121 项自测套件）

如果你想直接体验完整的演示脚本、阅读源码或运行本工具自带的 121 个自动化测试用例：

**macOS / Linux：**
```bash
git clone https://github.com/Raintrrrreeeee/pytest-flaky-triage.git
cd pytest-flaky-triage
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pip install -e .

# 1. 一键运行内置的 18 个测试演示靶场（约 10 秒跑完 42 轮矩阵，可通过 --lang zh 输出中文报告）：
pytest-flaky-triage --demo --lang zh

# 2. 查看生成的 Markdown 诊断报告：
cat out/report.md

# 3. 运行工具自身的 121 个单元与集成测试：
bash scripts/test.sh
```

**Windows (PowerShell)：**
```powershell
git clone https://github.com/Raintrrrreeeee/pytest-flaky-triage.git
cd pytest-flaky-triage
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -e .

# 1. 一键运行内置的 18 个测试演示靶场（约 10 秒跑完 42 轮矩阵，可通过 --lang zh 输出中文报告）：
pytest-flaky-triage --demo --lang zh

# 2. 查看生成的 Markdown 诊断报告：
Get-Content -Encoding utf8 out\report.md

# 3. 运行工具自身的 121 个单元与集成测试：
pytest tests -p no:randomly -p no:cacheprovider --ignore=tests/fixtures
```

---

## 命令行参数与实验矩阵说明

```bash
pytest-flaky-triage --project PATH [options]
# 或者在仓库根目录下运行：
scripts/detect.sh --project PATH [options]
```

四个实验条件各自只改变一个影响因子，保持其余条件恒定：

| 实验条件 (`Condition`) | 改变的变量 | 保持固定的变量 | 用于暴露的缺陷类型 |
|---|---|---|---|
| `repeat` | 不改变任何条件，原样重复执行 | 测试执行顺序、`PYTHONHASHSEED=0` | 未固定种子的随机数（`unseeded_randomness`）、并发时间竞态（`race_conditions`） |
| `hashseed` | 逐轮切换 `PYTHONHASHSEED` | 测试执行顺序 | 无序集合/字典哈希迭代顺序敏感（`hash_order_sensitivity`） |
| `order` | 逆序 + 多组固定种子的随机打乱顺序 | `PYTHONHASHSEED=0` | 测试执行顺序依赖、共享状态污染（`shared_state_interference`） |
| `isolated` | 每个测试在独立子进程中单独运行 | `PYTHONHASHSEED=0` | 共享状态干扰（被污染受害者 或 缺失初始化的状态依赖者） |

### 参数列表

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--project PATH` | （必填） | 待检测的目标 `pytest` 项目路径 |
| `--target SUBPATH` | （整个项目） | 仅收集并检测项目下指定的测试文件、子目录或节点 ID |
| `-k EXPR` | （无） | 仅运行匹配 `pytest -k EXPR` 表达式的测试 |
| `--repeats N` | `10` | `repeat` 基准重复运行轮数（`N >= 1`） |
| `--hash-seeds "S..."` | `"1 2 3 4 5 6 7 8"` | `PYTHONHASHSEED` 种子列表，每个种子运行一轮；传 `""` 可跳过 |
| `--orders K` | `5` | `order` 阶段随机打乱顺序的轮数（外加 1 轮完全逆序）；传 `0` 可跳过 |
| `--isolated N` | `1` | 每个测试单独隔离运行的轮数；传 `0` 可跳过 |
| `--isolated-scope MODE` | `all` | `all` 对所有测试执行单测隔离；`smart` 仅对前面套件运行中出现失败或波动的测试执行隔离 |
| `--timeout SECONDS` | `60` | 单轮 `pytest` 运行的最大超时秒数 |
| `--out DIR` | `out/` | 诊断产物输出目录（自动拦截 `/`、`$HOME`、软链接及包含目标项目的危险路径） |
| `--python PATH` | 自动探测 | 指定运行目标项目的 Python 解释器路径 |
| `--lang LANG` | `en` | 诊断报告 `report.md` 的输出语言：`en`（英文，默认）或 `zh`（简体中文，也可直接传给 `python -m report ... --lang zh`） |
| `--report-only` | 关闭 | 基于 `out/` 下已有的运行结果直接重新生成 `report.md`（例如秒切中英文报告，无需重跑 `pytest`） |
| `--no-report` | 关闭 | 仅生成 `results.csv` 和 `run_meta.csv`，不执行后续分析与报告生成 |

### 退出状态码（Exit Codes）

| 状态码 | 含义 |
|---|---|
| `0` | 诊断全部完成；已生成 `results.csv`、`run_meta.csv`、`diagnosis.json` 和 `report.md` |
| `2` | 命令行参数错误 |
| `3` | 未找到可用的 Python 解释器或未安装 `pytest` |
| `4` | `pytest` 无法收集目标项目测试，或未发现任何测试用例 |
| `5` | 所有运行轮次均未产出有效测试记录 |
| `6` | 第二阶段统计分析或第三阶段报告生成失败 |

### 输出目录文件说明（`out/`）

| 文件路径 | 内容说明 |
|---|---|
| `results.csv` | 每轮每个测试的观测结果：`run_id, condition, condition_value, position, test_id, outcome` |
| `run_meta.csv` | 每轮运行的元数据：`run_id, condition, condition_value, hash_seed, exit_code, status, seconds, tests_expected, tests_reported, log_path` |
| `logs/run_NNN.log` | 每一轮 `pytest` 的原始控制台输出日志 |
| `orders/` | `order` 阶段各轮所使用的测试排序清单 |
| `collected.txt`, `collect.log` | 收集到的测试节点 ID 清单及收集阶段日志 |
| `diagnosis.json` | 结构化诊断结果：稳定性得分、判定结论、运行时怀疑根因、污染源测试列表（`suspected_polluters`）及 AST 消歧最终结论（`resolution`） |
| `report.md` | 可读性强的 Markdown 诊断报告：汇总表、按根因分组的详细证据表、优先展示失败场次的日志链接以及源码行号提示 |

---

## 内置基准演示靶场（`scenario/`）

仓库内置了一个可选的**电商购物车与结账模拟系统**（位于 `scenario/src/ecommerce/` 与 `scenario/tests/test_cart.py`），包含 4 大类典型不稳定测试与 2 个稳定对照组（共 18 个测试，详细标准答案见 [docs/scenario.md](docs/scenario.md)）：

1. **哈希顺序敏感（`hash_order_sensitivity`，Test Side 测试侧，4 个测试）**：测试代码直接将无序的 Python `set` 集合用 `",".join(...)` 拼接成字符串并与固定顺序的字符串做相等断言，在不同 `PYTHONHASHSEED` 下结果翻转。
2. **未固定种子的随机性（`unseeded_randomness`，Code Side 业务侧，4 个测试）**：业务方法内部调用了未固定种子的 `random.choice(...)`，而测试断言其返回值等于某一个固定选项。
3. **并发竞态条件（`race_conditions`，Code Side 业务侧，4 个测试）**：`Cart.checkout()` 启动了后台 `threading.Thread` 线程异步扣减库存但未调用 `join()` 等待完成，与测试中紧随其后的库存断言产生时间竞态。
4. **共享状态互相污染（`shared_state_interference`，Test Side 测试侧，4 个测试）**：两组测试直接修改模块级共享购物车对象（`shared_test_cart` 与 `secondary_shared_cart`）且未清理，导致结果随执行顺序翻转，而在单测隔离下全部通过。
5. **确定性稳定对照（`stable`，2 个测试）**：完全使用局部状态、同步确定性逻辑的正常测试，在全部 42 轮运行中 100% 通过。

## 致谢（Acknowledgements）

特别感谢 **Hongguan Tan** 与 **Huizern Lim** 在项目早期原型探索与受控基准测试靶场（`scenario/`）设计过程中的宝贵合作、讨论与贡献，为本独立开源版本（v2）的架构演进奠定了基础。

## 开源协议

本项目基于 [MIT License](LICENSE) 开源。
