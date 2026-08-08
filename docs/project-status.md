# ReproTrace 项目状态

更新时间：2026-08-08

## 仓库快照

- 仓库：`https://github.com/YaoYang-123456/ReproTrace`
- 本地工作目录：`E:\codex-work\ReproTrace-local`
- 分支：`main`
- HEAD：`531d65653690ee1342fa5b87745dc5b605f90b43`
- 本地 `main` 与 `origin/main`：当前一致
- 本阶段开始时工作树：干净

## 当前已实现

CLI 已实现四个命令：

- `run`：解析 manifest、记录执行前证据、顺序执行步骤、收集产物和指标并验证 bundle；
- `verify`：重新检查 source、输入/产物哈希、命令状态和指标容差；
- `diff`：比较两个 evidence bundle 的 source、environment、inputs、commands、artifacts 和 metrics；
- `report`：从机器可读证据重新生成 Markdown 报告。

证据链为：

```text
source -> environment -> inputs -> commands -> logs -> artifacts -> metrics -> verification -> report
```

当前能力包括 CSV 与日志正则指标提取、SHA-256 输入和产物校验、dry-run 预检、argv 数组执行，以及敏感环境变量脱敏。

## C5 Stage 4 assurance verifier

新运行使用 `run.json.schema_version=1`，并在 producer records 完成后生成
canonical `evidence.index.json`。索引只包含 verifier 的实际依赖闭包：核心记录、
source patch/status、已尝试命令的 stdout/stderr、bundle-local inputs/artifacts
以及 raw metric sources；`verification.json`、`report.md` 和索引自身不参与。

Schema-1 verifier 不访问 input、artifact、command 或 metric source 的 origin
绝对路径。它检查安全 bundle-relative 路径、索引闭包、每个文件的大小与 SHA-256，
并从 ordered raw metric evidence 重算指标。重算结果与 `metrics.json` 使用严格数值
一致性比较；实验的 expected/atol/rtol 只以 resolved manifest 为权威。

Assurance 与结果保持正交：完整索引达到 `bundle_integrity_checked`；至少一个指标且
所有派生一致时达到 `metric_derivations_recomputed`。科学容差未满足只产生
`result_status=not_matched`，不会降低 assurance。零指标和 dry-run 的完整 planning
bundle 可达到 bundle integrity，但结果为 `not_evaluated`。Coverage 中
`metric_sources.captured` 统计完整验证 source set 的 metric 数，
`source_files_captured` 单独统计文件数。

Legacy run schema 0 继续可读，最高 assurance 固定为 `recorded`、result 固定为
`not_evaluated`，且不重新读取旧 input/artifact origin 路径。所有级别仍明确不建立
execution authenticity、independent replay 或 scientific reproduction。

Stage 4 本地验证结果：默认编码与 `python -X utf8=0`（cp1252）完整套件均为
`130 passed, 4 skipped`。四个 skip 是当前 Windows 账户没有普通 file/directory
symlink 创建权限；Windows junction 逃逸测试实际执行并通过。真实 tiny CPU bundle
使用 run schema 1，16 个 closure entries 全部验证，最终为
`verification_status=complete`、`assurance_level=metric_derivations_recomputed`、
`execution_record_status=recorded_success`、`result_status=matched`。

## 测试状态

当前测试套件包含原有功能测试和 C4 source evidence 回归测试。覆盖：

- manifest 基本校验、shell 字符串拒绝和路径穿越拒绝；
- tiny CPU 实验端到端运行；
- 产物篡改检测；
- dry-run 不执行命令；
- 相同运行 diff 为 identical；
- seed 和产物变化的 diff；
- CSV 与日志正则指标提取；
- source ref 预检失败。

## C5 Stage 4.1 declaration closure 与 Stage 5 presentation

Stage 4.1 已关闭 manifest declaration false-uplift 路径：schema-1 verifier 以
`manifest.resolved.yaml` 为 input 与 artifact declaration authority，拒绝记录缺失、额外、
重复以及 declaration 字段修改。即使攻击者同步重建 records 与 evidence index，也不能删除
manifest 声明并继续获得 bundle integrity assurance。`{run_dir}` artifact 不能谎报为 external；
正常运行中 artifact pattern 合法匹配零文件不属于 canonical schema failure。

Stage 5 将 Stage 1–4 的现有语义呈现到 CLI 与 `report.md`，不新增 assurance 能力：

- CLI/report 分开显示 `verification_status`、`checks_passed`、`assurance_level`、
  `execution_record_status` 和 `result_status`；不再以 deprecated `passed` 作为 schema-1 主结论；
- `verification=complete`、checks PASS 与 `result_status=not_matched` 可以同时成立；CLI 因声明目标
  未达到返回 1，但不会把 expectation miss 写成 verification failure；
- report metric 表从同一 verifier result 展示 recorded/recomputed actual、manifest expected、
  tolerance 与结果，不运行第二套 extraction；
- report 明确展示 coverage、metadata-only 边界，以及 execution authenticity、independent replay、
  scientific reproduction 均未建立；
- `reprotrace report` 在写报告前重新运行 `verify_bundle()`，tamper 后不会沿用 stale 成功结论；
- legacy schema 0 顶部固定保守呈现 `assurance=recorded`、`result=not_evaluated`，旧
  `status/passed/preflight_passed` 只在 compatibility 区保留。

本阶段基线为分支 `codex/c5.0-assurance-verifier`、HEAD
`81238fb2079f2ad9147a2291300e94d878991ddf`。Stage 5 targeted tests 为
`16 passed`；默认编码与 `python -X utf8=0` 完整测试均为 `162 passed, 4 skipped`。
四个 skip 仍仅因当前 Windows 账户无普通 symlink 创建权限，Windows junction 两项测试实际
执行并通过。未启动 PEFT-ViT、训练或 GPU。

## C4 source evidence

C3 验证期间确认：Windows 默认 cp1252 Python 环境在解码包含中文 UTF-8
修改的 `git diff --binary` 输出时会失败。C4 将所有 Git stdout/stderr 改为
bytes 捕获，不再依赖当前 locale，并新增：

- 原子写入的 `source.patch`：保存 tracked changes 的原始、可包含二进制
  delta 的 Git patch；
- 原子写入的 `source.status`：保存 NUL 分隔的 porcelain v1 状态和原始文件名；
- `source.json` schema 1：记录 Git 版本、固定生成参数、格式、大小、SHA-256
  和明确的 partial replay coverage；
- verify 对 source evidence 的 bundle-relative 路径、路径逃逸、普通文件类型、
  大小和 SHA-256 进行检查；
- legacy source bundle 继续按 schema 0 读取，无需迁移。

未跟踪文件只记录名称和状态，不复制内容；ignored 文件和 dirty submodule
工作树内容也不捕获，因此 bundle 不代表完整源码归档。CI 覆盖 Ubuntu、
Windows 和 macOS。

## 固定调研提交

- PEFT-ViT：`5095e75ef45018baef7ccf935ba6095b6d030d9b`
- VPT：`4410440ec1b489f24f66b9fad3d9b10ff3443567`
- SSF：`e94e0e704a4ece1986a537c97a95158b46838f71`
- FreqFit：`fe72c1d653aecf39d6d6b404ea286450f2980928`

用户 PEFT-ViT 审计分支：`7e5039ca0ed63ec196cb438b6ea33b7d3778c362`。该分支只作为证据语料，不视为论文官方实现。

## PEFT-ViT 当前状态

`examples/peft-vit/reprotrace.yaml` 是 approximate adapter，不是严格论文复现配置。固定提交的只读审计已确认：

- 复现目标为官方 README/Table 1 的 CIFAR-100、LoRA `r=8`、88.27%；
- 配置路径为 `configs/lora/cifar100-r8-lr-0.005.yaml`；
- 命令形式为 `python main.py fit --config configs/lora/cifar100-r8-lr-0.005.yaml`；
- CSV 验证指标列名为 `val_acc`；
- 目标值为 `0.8827`；`atol=0.01` 是 ReproTrace 自定的验收阈值，不是论文提供的容差。

正式 GPU 运行前仍有阻塞项：显式 seed 传递、CIFAR-100 预置与哈希、DINO 权重 revision 固定，以及 precision 的明确选择。当前不凭猜测设置 precision。

在独立的 Windows CPU 环境中，固定提交的官方入口在未设置 `PYTHONPATH` 时实际以 `ModuleNotFoundError: No module named 'utils'` 失败。环境为 Python `3.10.20`、torch `2.0.1+cpu`、Lightning `2.0.2`、Transformers `4.36.0`。仅在 manifest 的训练步骤设置 `PYTHONPATH={project_root}/src` 后，`python -B main.py fit --help` 在约 4.1 秒内以退出码 0 返回完整 LightningCLI `fit` 帮助。这是 ReproTrace manifest 层的环境适配，不修改上游源码。

## PEFT-ViT 无训练 dry-run

2026-08-06 在固定 checkout `5095e75ef45018baef7ccf935ba6095b6d030d9b` 上完成真实 dry-run：

- evidence bundle：`E:\codex-work\ReproTrace-local\.reprotrace\runs\20260806T081122Z-40cddc`；
- 状态：`planned`，`preflight_passed=true`；
- source：固定 commit 匹配，`dirty=false`；
- training config：存在，SHA-256 为 `41d3536e002272816e36a004c8d96d07d5e4b4da24e3861d39d3d3587679eb2c`；
- CIFAR-100：当前未预置，作为正式 GPU 运行前阻塞项保留；
- planned command 使用 `configs/lora/cifar100-r8-lr-0.005.yaml`；
- PEFT-ViT checkout 内未创建 `.reprotrace/`，工作树保持 clean；
- 未导入 PEFT-ViT 入口、未下载数据、未执行训练、未使用 GPU。

## Windows CPU baseline

本阶段使用仓库内 `.venv` 隔离环境，不污染全局 Python。验证命令为：

```powershell
python --version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\reprotrace.exe --version
.\.venv\Scripts\reprotrace.exe run examples/tiny/reprotrace.yaml
```

实际 Windows 验证结果：

- Python：`3.13.9`；解释器：`E:\codex-work\ReproTrace-local\.venv\Scripts\python.exe`；
- `.venv` 创建成功，`python -m pip install -e ".[dev]"` 成功；
- `python -m pytest`：`10 passed`；
- `reprotrace --version`：`reprotrace 0.1.0`；
- 最终 tiny run：`E:\codex-work\ReproTrace-local\.reprotrace\runs\20260806T073618Z-6ce9bc`；
- `verification.json`：`status=passed`、`passed=true`，5 项检查全部通过，失败检查数为 0；
- `report.md`：文件存在，报告决策为 `passed`。

这些命令均在已激活的仓库内 `.venv` 中执行；全局 Python 未用于安装项目依赖。

当前阶段未启动 GPU，也未启动 PEFT-ViT 训练。

## C4 Windows 验证

在显式使用 `python -X utf8=0`、`locale.getencoding()=cp1252` 的环境下完成：

- 完整测试：`40 passed, 2 skipped`；两个 skip 是本机无普通 symlink 创建权限，
  Windows junction 逃逸测试已实际通过；
- tiny CPU 端到端运行：`verification.status=passed`；
- dirty source 证据：`source.json.schema_version=1`，`source.patch` 和
  `source.status` 的大小与 SHA-256 复核通过；
- `verify` 中 `source:git_patch` 与 `source:git_status` 均通过；
- 全过程未设置 `PYTHONUTF8=1`，未再出现 cp1252 `UnicodeDecodeError`。

## C4.1 审查修正

C4 最终只读审查发现的四项问题已按最小范围修正：

- source capture 在内存中携带已确认的规范化 worktree root，output isolation
  不再二次执行 `rev-parse --show-toplevel`；`check-ignore` 仅接受退出码 0/1，
  其他错误全部在创建 evidence 目录前 fail closed；
- 首个 Git 探测失败时检查当前目录及祖先的 `.git` 文件或目录，区分普通
  non-Git 目录与已有 Git marker 的操作错误，并保留 bytes stderr 的非权威摘要；
- verify、diff、report 共用 source record 解析入口，统一接受 legacy schema 0
  和 schema 1，并以 `ConfigError` 拒绝未知版本与畸形嵌套对象；
- patch 命令固定 `--inter-hunk-context=0`，以命令级配置关闭
  `diff.suppressBlankEmpty`，并以 Git 跨平台识别的空 `/dev/null` order file
  中和 `diff.orderFile`；`.gitattributes`、EOL 等工作树语义配置仍保留。

C4.1 本地验证结果：

- source 专项：`56 passed, 2 skipped`；
- 默认编码完整测试：`70 passed, 2 skipped`；
- `python -X utf8=0`、cp1252 完整测试：`70 passed, 2 skipped`；
- 两个 skip 均因当前 Windows 用户无普通 symlink 创建权限，Windows junction
  逃逸测试实际执行并通过；
- 注入 `diff.orderFile`、`diff.interHunkContext` 和
  `diff.suppressBlankEmpty` 后，捕获的 patch bytes 与无注入基线完全一致；
- 默认编码与 cp1252 tiny CPU 均为 `verification.status=passed`，随后独立执行
  verify/report 成功，`source:git_patch` 与 `source:git_status` 检查均通过。

Windows 本地已验证 Git 接受 `/dev/null` 作为空 order file；Linux 与 macOS
由新增的同一生产路径回归测试在 GitHub Actions matrix 中继续确认。

## 下一步

下一步仅在人工批准后进入 Stage 6：adversarial suite、文档、跨平台 CI 与最终验收。
PEFT-ViT 正式 GPU 阻塞项仍为 seed、CIFAR-100 预置与哈希、DINO revision 和
precision；所有阻塞项关闭并单独确认 GPU 实验方案前，不启动训练。
