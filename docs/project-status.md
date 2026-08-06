# ReproTrace 项目状态

更新时间：2026-08-06

## 仓库快照

- 仓库：`https://github.com/YaoYang-123456/ReproTrace`
- 本地工作目录：`E:\codex-work\ReproTrace-local`
- 分支：`main`
- HEAD：`a7e317c410ffe860a98eec8af9d0061e520ba600`
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

## 测试状态

当前测试套件包含 10 项测试，覆盖：

- manifest 基本校验、shell 字符串拒绝和路径穿越拒绝；
- tiny CPU 实验端到端运行；
- 产物篡改检测；
- dry-run 不执行命令；
- 相同运行 diff 为 identical；
- seed 和产物变化的 diff；
- CSV 与日志正则指标提取；
- source ref 预检失败。

Windows CPU baseline 的实际结果将在本阶段验证完成后补充。

## 固定调研提交

- PEFT-ViT：`5095e75ef45018baef7ccf935ba6095b6d030d9b`
- VPT：`4410440ec1b489f24f66b9fad3d9b10ff3443567`
- SSF：`e94e0e704a4ece1986a537c97a95158b46838f71`
- FreqFit：`fe72c1d653aecf39d6d6b404ea286450f2980928`

用户 PEFT-ViT 审计分支：`7e5039ca0ed63ec196cb438b6ea33b7d3778c362`。该分支只作为证据语料，不视为论文官方实现。

## PEFT-ViT 当前状态

`examples/peft-vit/reprotrace.yaml` 目前只是待核验的 approximate adapter，不是已经确认的严格论文复现配置。真实 checkout 中必须再次确认：

- 配置路径：`configs/lora/cifar100-r8-lr-0.05.yaml`；
- 命令形式：`python main.py fit --config configs/lora/cifar100-r8-lr-0.05.yaml`；
- 指标列名：`val_acc`；
- 目标值：`0.8827`，当前 manifest 容差为 `atol=0.01`、`rtol=0.0`。

在固定提交的真实 checkout、输入文件和输出格式完成核对前，不把这些字段当作已验证事实。

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

## 下一步

完成 Windows CPU baseline 后，下一步是对 PEFT-ViT 固定提交 `5095e75ef45018baef7ccf935ba6095b6d030d9b` 的真实 checkout 执行 dry-run。dry-run 通过前不设计并启动长时间 GPU 训练；dry-run 通过后仍需单独确认 GPU 实验方案。
