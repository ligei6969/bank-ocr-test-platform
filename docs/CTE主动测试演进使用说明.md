# CTE 主动测试演进：CMD 使用与验收

先审核后实施的结论见 [审核记录](CTE主动测试演进审核.md)。本阶段是 P5 知识层迭代；不训练模型，不自动修复生产代码，不重启服务。

## 1. 准备环境

以下全部在 CMD、项目根目录执行。新入口直接使用模型客户端，不依赖 8100 HTTP 服务。

```cmd
call C:\Users\jb\miniconda3\Scripts\activate.bat
conda activate bank
cd /d J:\job\bank-ocr-test-platform
```

## 2. 先跑工程演示

```cmd
python -m scripts.discover_cte run --task test_evolution/discovery_examples/card_number.json --demo
```

`--demo` 是明确标记 `fixture:cte-demo` 的脚本预测替身，不调用模型。但执行器真的调用当前解析器，三个重复结果必须稳定。示例比较 ASCII 数字和全角数字，检查本任务声明的 ASCII 输出契约；不验证真实银行卡、Luhn 或生产金融规则。

输出 `run_id`，文件位于 `reports/cte-discovery/runs/<run_id>/`：

- `prediction.json`：冻结模型、输入、代码版本、预算、经验快照与时间。
- `worker-input.json`：执行器只看到 target、inputs、repeats。
- `result.json`：实际结果、Oracle 来源、是否可复现、重复标记与指标。
- `reflection.json`：基于执行证据的比较，不能自动当成根因证明。

每次生成新目录，冻结包用独占创建与内容摘要保护。文件哈希防止误改，不是对本机管理员的防篡改安全承诺。

## 3. 真正调用大模型

在同一个 CMD 设置模型配置。已有配置可直接执行最后一行。

```cmd
set "LLM_PROVIDER=openai"
set "LLM_BASE_URL=https://api.deepseek.com"
set "LLM_MODEL=deepseek-flash"
set "LLM_TIMEOUT_S=60"
set /p "LLM_API_KEY=请输入密钥："
python -m scripts.discover_cte run --task test_evolution/discovery_examples/card_number.json --live --rounds 2
```

`--live` 会发生真实 API 调用。最多允许 3 轮学习；每轮都重新冻结。之前执行过的相同输入标记 `seen_before`，不计入新盲预测指标。无模型、非法 JSON、超预算都报错，不伪装成 AI 成功。代码变化会使预测失效。

第一版生成的是有约束的 JSON 测试用例，含输入与假设；不执行模型生成的 Python/shell。可选目标：`card_number`、`expiry`、`id_fields`。在任务 JSON 中声明需求、group、partition 和独立 Oracle。

另外两种目标也提供了任务文件（需要真实模型）：

```cmd
python -m scripts.discover_cte run --task test_evolution/discovery_examples/expiry.json --live
python -m scripts.discover_cte run --task test_evolution/discovery_examples/id_layout.json --live --rounds 2
```

身份证布局任务使用 O5，只收集疑似差异；需要后续人工标注才能形成确认结论。

## 4. Oracle 来源与判定范围

| 来源 | 注册规则 | 判定边界 |
|---|---|---|
| O1 明确规范 | `number_format` | 非空返回必须是 16–19 位 ASCII 数字；缺失为 inconclusive |
| O2 确定性契约 | `expiry_format` | 非空返回须为合法月份的 MM/YY；不代表未过期 |
| O3 人工确认 | `exact` | `reviewed_by` 必填，labels 用输入摘要匹配；未覆盖输入不判错 |
| O4 参考行为 | `exact` | 不同只记 suspected；相同不证明正确 |
| O5 变形关系 | `relation` | 比较空格加倍或换行；违反只记 suspected |
| O6 模型意见 | `advisory` | 永远不能独立确认缺陷 |

格式规则只证明该不变式，不证明整个字段解析正确。Oracle 来源不是可信度分数；业务规范与人工答案仍可能错误，必须可追溯。图像亮度扰动本阶段未实现。

### 人工补充答案

先查看不含预测理由的审核包：

```cmd
python -m scripts.discover_cte oracle-review --run DISC-替换为实际ID --test unicode
```

把独立确定的期望值写入 JSON 文件，例如 `reports/expected.json`，再追加 O3 注释：

```cmd
python -m scripts.discover_cte annotate --run DISC-替换为实际ID --test unicode --expected-file reports/expected.json --reviewer jb --reference "经人工核对的输出要求"
```

注释是“执行后人工审核”，不会反写冻结预测，也不会把原 O5 结果偷偷改成盲预测命中。环境错误、抖动执行不能借人工签名变成确认缺陷。

## 5. 把证据变成经验

仅 learning 分区、未重复、可复现且 Oracle 有确定结论的结果可提议。confirmed 与 passes_oracle 都可形成限定范围的经验：后者是边界/反例，不代表系统完全无 Bug。

```cmd
python -m scripts.discover_cte propose --run DISC-替换为实际ID --test unicode --lesson "Unicode 数字匹配可能不满足 ASCII 输出契约，需显式检查字符集" --preconditions "合成全角数字，适用于证据所记录的解析器版本" --scope "银行卡号输出格式"
```

命令生成 `EXP-...` 和 `review_digest`。审核 lesson 是否被证据支持，再执行：

```cmd
python -m scripts.discover_cte promote --experience EXP-替换为实际ID --reviewer jb --digest 替换为完整review_digest
python -m scripts.discover_cte list
```

只有晋级后的 active verified 经验进入默认检索。署名是本地人工操作记录，不是登录认证；候选修改后旧 digest 失效。

如果经验总结超出了证据支持的范围，拒绝并归档候选：

```cmd
python -m scripts.discover_cte reject --experience EXP-替换为实际ID --reviewer jb --digest 替换为完整review_digest --reason "结论过度泛化，需要新证据"
```

被拒候选保留原文及拒绝理由，不能再次晋级；修正后重新提议。已晋级经验使用下方的 `invalidate` 退出检索。

后续调用 `propose` 可带 `--operation specialize|generalize|merge|add_counterexample`、重复 `--parent EXP-...`，以及 `--counterexample "反例描述"`。每次修订需要新执行证据和重新批准；merge 至少两个父版本。批准后旧版本追加 superseded 记录，历史文件保留。所有祖先的 source group 会传递，不能通过合并洗掉 holdout 排除条件。

失效的经验可退出检索：

```cmd
python -m scripts.discover_cte invalidate --experience EXP-替换为实际ID --reviewer jb --reason "新证据推翻了适用范围"
```

## 6. 导入项目已有经验

旧 `test_evolution/validated/*.md` 有具体样本与历史结果，不能整体塞进盲预测。先确认候选已 validated、检查通过且有人署名，再审核一条不含具体当前答案的概括：

```cmd
python -m scripts.discover_cte import-legacy --candidate CTE-002 --target id_fields --group id-label-layout --lesson "字段标签与值可能来自不同 OCR 文本行；生成布局变体，并验证字段关联假设" --reviewer jb
```

旧证据仅作为历史经验种子，不计为本次 AI 主动发现。导入会绑定源候选和知识文件摘要；源证据变化后必须重新审核。目标迁移是人工确认的适用性判断，不自动证明所有模块都有同一 Bug。

## 7. 导出回归资产

```cmd
python -m scripts.discover_cte export --experience EXP-替换为实际ID
```

输出受控模板生成的 pytest 文件，位于 `reports/cte-discovery/regression/`。按命令返回的路径运行：

```cmd
python -m pytest reports/cte-discovery/regression/test_EXP_实际ID.py -q
```

确认缺陷未修复时，该测试应该 FAIL。修复后验证 PASS，再经代码评审纳入现有 `tests/`；不是另建一套正式回归集。测试不依赖历史结果文件的位置，独立嵌入已审核的输入和 Oracle。

## 8. 无记忆与 RAG 对照

```cmd
python -m scripts.discover_cte compare --task test_evolution/discovery_examples/card_number_holdout.json --demo
```

真正实验将 `--demo` 换成 `--live`，并准备独立的任务组。A/B 都冻结后才执行，使用相同模型、测试数量上限、输出 token 上限、输入字符上限、超时和重复次数，B 使用冻结的 verified 经验快照。RAG 会增加输入 token；报告真实用量，不声称两组实际 token 消耗相等，也不伪称没有 tokenizer 时有精确总 token 门禁。

样例 holdout 故意与学习示例同 group，用来验证排除规则，**不是独立科研评估集**。若 `memory_count=0`，B 没有可用经验，该轮不能用来说明 RAG 有效。

validation/holdout 禁止多轮反馈、晋级和训练经验；修订谱系中的相同 group 也排除。数据作者必须按缺陷家族/模块/时间分组及语义去重，不能只改 task_id。所有已观察过的输入标为 seen_before。真正从最终评估集学习后，需要换新的最终评估集。

指标：可执行率、确定结论中的确认失败率、FPR、false discovery rate、重复率、已见输入率、重复执行一致率、token/确认失败输入。零分母为 null；独立新缺陷族数和货币成本默认 null，待人工分族和定价信息。测试执行一致性不等于缺陷因果证明。

要证明 Evolution，需多轮独立任务、多次运行、明确样本量和不确定性；当前 CLI 提供实验记录，不自动宣布“能力提升”。

## 9. 验证命令

```cmd
python -m pytest test_evolution/tests/test_discovery.py -q
python -m pytest -q
```

真实模型调用与离线 fixture 测试分别报告。此前历史 CTE 的 `blind_predict()` 已明确标为历史预期回放，不纳入新指标。
