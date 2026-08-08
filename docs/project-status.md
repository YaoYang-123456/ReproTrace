# ReproTrace 项目状态

更新时间：2026-08-08

## C5.0 acceptance snapshots

- 仓库：`https://github.com/YaoYang-123456/ReproTrace`
- acceptance 分支：`codex/c5.0-assurance-verifier`
- C5.0 基线：`main@0fc67da9bac302ec1d5c2f660b325d7225ee3067`
- Stage 6 preflight commit：`53f34b85784794d4c9ba4e7235b0a2d701acbc36`
- Stage 6.1 audit-fix branch：`codex/c5.0-audit-fixes`
- Stage 6.1 authoritative base：`921943155f04da7839a43837b8961bdc5b5f1dc0`
- snapshot 日期：2026-08-08
- Stage 6 开始时工作树：干净

以上是具名验收快照，不表示读取本文时仓库的动态 HEAD。

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

## C5.0 Stage 6 local acceptance

C5.0 已完成 canonical assurance contract、bundle-safe evidence index、raw metric
source capture、verifier-side metric re-extraction、manifest declaration closure 和
CLI/report precise semantics。正式 adversarial matrix 位于
[`docs/adversarial-acceptance.md`](adversarial-acceptance.md)，覆盖：

- A1 forged successful command record 与 command-log closure；
- A2 stdout/stderr 删除或未重建索引的修改；
- A3 cached `metrics.json` 修改并重建 index；
- A4 raw metric evidence 与 embedded/index hash 同步修改；
- A5 bundle relocation、producer 消失与 origin metadata trap；
- A6 resolved expected/atol/rtol 修改并重建 index；
- A7 traversal、POSIX/Windows absolute、drive/UNC、symlink 与 junction escape。

另外保留一项 expected-limitation fixture：恶意 producer 若同步重写 commands、logs、raw
metrics、derived metrics、resolved manifest、metadata 和 index，使 bundle 完全自洽，C5.0
可能达到 `metric_derivations_recomputed`。这不属于 C5.0 可检测范围；输出仍必须明确
`execution_authenticity=not_established`、`independent_replay=not_performed`、
`scientific_reproduction=not_established`。

2026-08-08 本地 acceptance 结果：

- assurance/adversarial suite：`37 passed`；
- CLI/report：`16 passed`；metric evidence：`7 passed`；assurance contract：`16 passed`；
- evidence index/path：`20 passed, 2 skipped`；C4/C4.1 source：`56 passed, 2 skipped`；
- 默认编码完整套件：`166 passed, 4 skipped`；
- `python -X utf8=0` 在 `encoding=cp1252` 下完整套件：`166 passed, 4 skipped`；
- 四个 skip 均为本地 Windows 账户缺少普通 file/directory symlink 创建权限；两个真实
  Windows junction escape 用例均执行并通过；
- 真实 tiny CPU run 使用 run/verification schema 1，得到 `complete`、checks PASS、
  `metric_derivations_recomputed`、`recorded_success`、`matched`，evidence root 为
  `6506f1d8e42d4e8a1b0e974264ed61886348de34db0adb9e5ad3b63173f0c5ca`；
- 独立 verify/report 退出 0；移动 bundle 并删除原位置后再次 verify/report，root、assurance
  和 result 保持相同。

本地 acceptance 未使用网络、GPU、付费基础设施，也未启动 PEFT-ViT。跨平台最终状态由
该分支 push 后的 Ubuntu Python 3.10/3.12、Windows 3.12 和 macOS 3.12 GitHub Actions
矩阵确认。

C5.0 的已知边界包括恶意 producer 完整一致伪造、execution authenticity、independent
replay、scientific reproduction，以及 external input/artifact 的 metadata-only coverage。
Evidence root 是 snapshot identifier，不是签名、attestation 或可信来源证明。

## C5.0 Stage 6.1 protocol closure hardening

独立最终审计发现的 deterministic protocol/closure gaps 已在独立 audit-fix 分支关闭：

- `manifest.resolved.yaml._reprotrace.commands` 记录 producer-finalized command protocol；
  `commands.json` 是唯一 semantic authority，逐 step 绑定 requested/resolved argv、cwd、
  environment overrides、timeout 和固定 stdout/stderr evidence identity；
- command status 严格限定为 `planned`、`completed`、`failed`、`timeout`、`launch_error`，
  return code 必须是 integer/null 且 bool 不作为 0；dry-run、完整成功和失败前缀分别受明确
  `run.status` state machine 约束；
- `commands.jsonl` 继续进入 evidence index，但角色改为 `command_archive`，不再作为 verifier
  未实际消费的第二份 semantic command record；
- `{run_dir}` artifact 使用 canonical POSIX segment glob：`*`/`?`/bracket 不跨 `/`，完整
  `**` segment 匹配零个或多个 segments；每个 bundle-local match 必须满足 manifest pattern，
  同一 declaration 内 duplicate canonical evidence path 被拒绝，零匹配继续合法；
- expected 必须 finite，atol/rtol 必须 finite 且非负，timeout 若存在必须 finite 且为正；
  bool 不作为数值，CSV/regex NaN/Infinity fail closed，JSON evidence writer 不输出
  NaN/Infinity token。

2026-08-08 Stage 6.1 本地验证：定向 assurance/manifest suite `70 passed`；默认编码和
`python -X utf8=0`（`encoding=cp1252`）完整套件均为 `196 passed, 4 skipped`。四个 skip
仍仅为本机普通 symlink 权限；Windows junction tests 实际执行。真实 tiny CPU bundle 的
command protocol check、index roles、独立 verify/report 均通过，最终为 `complete`、
`metric_derivations_recomputed`、`recorded_success`、`matched`，evidence root 为
`ea3addf7e1ce1e4841cb5d5ad809c4d45bb514e0ae26e6fb951479f76216bce7`。

Stage 6.1 明确没有改变 verifier-time hash/open/parse TOCTOU，也没有提供 immutable
same-object snapshot。该问题只保留为需要单独授权的 Stage 6.2 研究/实施范围。

## C5.0 Stage 6.2a verified snapshot model

Stage 6.2a 在独立分支 `codex/c5.0-snapshot-identity` 上新增 verifier-private
snapshot/session 内部模型，基线为
`f54cc49ce95caaf663d649ef4f78f729d7f36cba`。设计细节见
[`docs/verified-snapshot-model.md`](verified-snapshot-model.md)。

- `VerifiedEvidenceObject` 支持 immutable memory bytes、verifier-owned
  `SpooledTemporaryFile` 和 integrity-only 三种 retention；只有成功 acquisition、
  fingerprint validation 与 seal 后才能取得 semantic reader；
- `VerifiedBundleSnapshot` 保存 exact canonical index bytes、parsed index、candidate root、
  canonical path object map、parsed-record cache 与 root-identity placeholder；缺失、失败、
  未验证或未封存对象均阻止 established root；
- `VerificationSession` 显式拥有 snapshot 与 spool 生命周期，支持 context manager、幂等
  cleanup 和非权威 cleanup diagnostics；cleanup 错误不回溯改变 evidence correctness；
- candidate root 仍是 canonical `evidence.index.json` bytes 的 SHA-256，但只有完整 acquisition
  且 snapshot seal 后才可作为 established root 暴露；evidence-root 公式和 assurance taxonomy
  均未改变。

Stage 6.2a 专项测试 `16 passed`，Stage 6.1 assurance/manifest 回归 `70 passed`；默认编码与
`python -X utf8=0`（`encoding=cp1252`）完整套件均为 `212 passed, 4 skipped`。四个 skip
仍仅为本机普通 symlink 权限，Windows junction tests 实际执行；`git diff --check` 与
`compileall` 均通过。

本阶段没有接入现有 verifier、metrics、report 或 CLI，也没有读取 live bundle path。
因此 Stage 6.2a 完成时 verifier-time TOCTOU/H1 尚未关闭；后续能力继续按 Stage 6.2b–6.2f
独立授权和验收。

## C5.0 Stage 6.2b handle-bound single-file acquisition

Stage 6.2b 在同一 `codex/c5.0-snapshot-identity` 分支增加单个 evidence object 的
handle-bound acquisition primitive，但仍未接入生产 verifier：

- `FileIdentity` 使用 immutable structured `st_dev`、`st_ino`/file index 和 file type；
  bool/无效 id 被拒绝，identity 不可用时显式 fail closed，不退化为 pathname assurance；
- object 必须先加入已由 `VerificationSession` claim 的 snapshot，再开始 live acquisition；
  成功和失败的 memory/spool retention 都由 session 统一清理；
- acquisition 依次执行 root identity、candidate precheck、单次 read-only/non-inheritable
  `os.open`、descriptor `fstat`、root/path postcheck，然后才使用 bounded `os.read` chunks；
- 同一 descriptor stream 同时驱动 observed size/SHA-256 以及 memory、spool 或
  integrity-only retention；不执行 path hash、semantic reopen、自动 retry 或 spool fallback；
- final regular/symlink swap、parent symlink/junction redirect、root replacement、read error、
  spool error 和 fingerprint mismatch 均在对象上 fail closed，且 identity uncertainty 在读取
  semantic bytes 前失败。

Stage 6.2b 本地 Windows 专项为 `15 passed, 4 skipped`，Stage 6.2a 模型回归 `16 passed`，
Stage 6.1 assurance/manifest 回归 `70 passed`。新增 skip 为两个普通 symlink 权限用例、
POSIX-only parent symlink，以及 Windows `os.open` 共享模式无法实际执行的 post-open rename；
后者由完成真实 post-path inspection 后注入 structured identity mismatch 的 Windows fixture
覆盖。Windows parent junction replacement 实际执行并通过。

默认编码与 `python -X utf8=0`（`encoding=cp1252`）完整套件均为
`227 passed, 8 skipped`；`git diff --check` 与 `compileall` 通过。Stage 6.2b 仍没有改变
`verify_bundle`、`validate_evidence_index`、metric extraction、report、CLI、schema、assurance
taxonomy 或 evidence-root 公式，因此生产 verifier 的 H1/TOCTOU 仍保持 open，等待单独批准
Stage 6.2c 及后续集成。

## C5.0 Stage 6.2c schema-1 verified snapshot construction

Stage 6.2c 在同一 `codex/c5.0-snapshot-identity` 分支新增独立、尚未接入生产 verifier 的
schema-1 snapshot builder：

- bundle root structured identity 只捕获一次，`run.json` 与 `evidence.index.json` 各经共享的
  handle-bound engine 读取一次；schema 0 在读取 index 前以 not-applicable 停止；
- captured index 必须是 strict UTF-8、无 NaN/Infinity 且逐字节 canonical 的现有 schema 1
  index；candidate root 仍是 exact canonical index bytes 的 SHA-256；
- 已捕获的 `run.json` bytes 直接绑定其 index entry，不重新打开 live path；其余 entry 按
  canonical path order 各采集一次，且全部复用同一个 session root identity；
- 九个 core semantic filenames 使用 immutable memory retention；非 core 且带既有
  `metric_source` role 的 entry 使用 verifier-owned spool；其余 entry 仅保留 integrity state；
- 九个 core record 只从 retained snapshot bytes 解析并缓存；JSON/YAML 语法、UTF-8、
  non-finite JSON 或根容器错误均在 complete/seal 前 fail closed；
- 只有所有 indexed bytes 通过 size/SHA-256、所有对象 seal 且 core cache 完整后，candidate
  root 才成为 established root。成功后的 memory/spool 语义不依赖 live bundle 或 producer
  原路径。

该模型只声明 same indexed logical byte snapshot，不声明 filesystem-atomic directory snapshot。
本阶段没有改动 `verify_bundle`、metric extraction、report、CLI、schema、assurance taxonomy、
root 公式或 derived-output 写入，因此生产 H1/TOCTOU 仍未关闭；snapshot-backed metric
extraction、生产 verifier/report session 集成与 root-identity safe-write 继续等待独立批准。

2026-08-08 本地 Windows 验证结果：Stage 6.2c builder 专项 `28 passed`；Stage 6.2b
acquisition 回归 `17 passed, 4 skipped`；Stage 6.2a snapshot model 回归 `16 passed`；
Stage 6.1 assurance/manifest 回归 `86 passed`。默认编码与 `python -X utf8=0`
（`encoding=cp1252`）完整套件均为 `257 passed, 8 skipped`。8 个 skip 均为既有平台条件：
Windows 当前用户缺少普通 file/directory symlink 权限、一个 POSIX-only parent-symlink 场景，
以及 Windows 打开文件共享模式不允许 post-open rename；Windows junction tests 实际执行。

真实 tiny CPU bundle 随后由 Stage 6.2c builder 成功构造 `sealed` snapshot：16 个 index entries
全部获取，九个 core records 全部缓存，raw metric source 从私有 spool 可读；established root
`d4a6e3d6eda7f338d74206c239c00bc6783b929da08b3a1e2a12418c2bfe621a` 与现有 production
verification root 相同。`compileall` 与 `git diff --check` 均通过；未使用 GPU、训练、网络或
PEFT-ViT。

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

## 后续研究候选

后续仅记录为待人工研究决策的候选方向，不自动形成实施任务：

- C5.1/C6 independent replay；
- stronger provenance / trusted execution boundary；
- verification 与 reproduction claim 的系统化 evaluation。

PEFT-ViT 正式 GPU 阻塞项仍为 seed、CIFAR-100 预置与哈希、DINO revision 和
precision；所有阻塞项关闭并单独确认 GPU 实验方案前，不启动训练。
