# OKR 自动推进器 Implementation Plan

**Goal:** 只用 Python3 标准库和现有脚本，提供每 30 分钟可重复执行的 KR 扫描、限额派单、监控、验收收口与展板刷新。

**Architecture:** OKR Markdown 与 inflight.json 是只读输入；稳定 batch 名关联 KR。推进器自己的 JSON 状态保存日配额、重派次数、启动日志与验收观测，用 flock 防重入、原子替换保存。所有外部命令采用参数列表，不经 shell。默认 dry-run，只有显式 --execute 才允许真实状态变更。

**Tech Stack:** Python3 标准库（argparse / dataclasses / json / fcntl / subprocess / unittest）。

## 全局约束

- 本轮只实现、自测、dry-run，待用户确认才允许 --execute 和安装 cron。
- 不读环境变量、.env、凭据文件；不修改 wiki、OKR、已有门禁与其他执行者产物。
- 子单仅允许本地只读核查，写入专属报告，不授权 wiki 改写、索引恢复、删内容、付费或模型配置。
- 每轮最多 2、每天最多 20；每 KR 最多 1 running；每执行者最多 2 running；每 batch 最多自动重派 2 次。
- 现状：dispatch-guard.py 未实现 --check-before-retry，而 inflight-check.py 有；不得替换门禁静默绕过，接口不符必须 fail-closed。
- 现状：Hermes script-only 入口必须位于 ~/.hermes/scripts/；确认后通过薄包装调用项目脚本，使用 hermes cron create，不手改 jobs.json。
- 不派子智能体、不联网、无 Git 仓库不提交；与用户要求冲突的通用技能派单模板不采用。

## Task 1：真源解析（RED → GREEN）

创建 test_okr_auto_pilot.py 的单条解析测试，执行 `python3 -B -m unittest discover -s projects/进度展板 -p test_okr_auto_pilot.py -v`，确认缺模块失败。实现 okr_auto_pilot.py 的 KR 数据类、9 行表格解析、状态/owner/证据/关联举措及人工标记，重跑测试。

## Task 2：决策护栏（RED → GREEN）

逐组加测试并运行到失败，再实现危险正则、人工标记、批次别名边界、running/failed 覆盖、执行者容量、轮/日配额、持久重派计数与 needs_human 升级。危险样本至少包括 rm -rf、密钥、DROP。

## Task 3：监控与验收（RED → GREEN）

先测 PID/进程组存活、空文件/目录/错误内容/旧文件不收口。实现产物真实大小、UTF-8 内容与关键词统计、前后签名稳定性；运行中不得自动收口。未知身份/宽泛目录/数据库/日志只给人工处理，不将存在当达标。

## Task 4：执行链与幂等（RED → GREEN）

先测 dry-run 不调用写命令、guard 不支持/拒绝即不派、启动登记顺序、失败限次、关闭后复读状态、重复运行不重派、异常写一行日志。实现真通道参数、持久预留后启动、台账 --add / --close、刷新展板、仅变更输出摘要。

## Task 5：真实 dry-run 与交付

运行全部新测试及现有展板回归；手动 dry-run 扫真实 9 KR 与在途账本；运行 `--self-test` 三条危险名。将真实 stdout 用 tee 写 scratch/task-okr-pilot.log（并同步用户派单登记的 ~/.hermes/cache/scratch/task-okr-pilot.log）。复核真源/账本/cron 字节哈希未变化。交付实测输出、接口阻塞、未安装 cron 及无 next_run 的事实。
