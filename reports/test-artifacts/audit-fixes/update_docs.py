"""Sync current acceptance numbers only after the actual full run succeeds."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = Path(__file__).resolve().parent
run = (OUT / "full-regression.txt").read_text(encoding="utf-8", errors="replace")
match = re.search(r"(\d+) passed in ([^\r\n]+)", run)
if not match or re.search(r"\d+ failed", run):
    raise SystemExit("Full regression has not passed; refusing to publish acceptance numbers")
count, timing = match.groups()

paths = ["README.md", "docs/项目交接文档.md", "docs/项目验收报告.md", "docs/PR_描述.md", "docs/项目使用方法、亮点与成果.md"]
for relative in paths:
    path = ROOT / relative
    text = path.read_text(encoding="utf-8").replace("1268", count)
    text = text.replace("基线 1052，新增 216", f"原基线 1052，当前增加 {int(count) - 1052}")
    text = text.replace("已登记、**未修**（修法需设计选择）", "已修复、机器验证通过；CTE-006 待人工署名")
    text = text.replace("已登记、**未修**", "已修复、机器验证通过；CTE-006 待人工署名")
    text = text.replace("**未修** —— 修法需设计选择，先记录量化证据", "已修复有效期标签误选；姓名候选评分，CTE-006 待人工署名")
    text = text.replace("`EVT-006`，未修", "`EVT-006` 已修复；当前模糊姓名 55%")
    text = text.replace("EVT-006，未修", "EVT-006 已修；当前模糊姓名 55%")
    text = text.replace("`EVT-006` 未修 —— 修法有多个方向、各有取舍，属设计选择", "`EVT-006` 标签误选已修复；模糊姓名 48% → 55%，残余 OCR 漏字/粘连仍在")
    text = text.replace("- `EVT-006`：模糊银行卡姓名可能被有效期标签误识；修复方向涉及停用词、模糊匹配或版面假设，需先选策略；", "- `EVT-006` 标签误选已修复；模糊姓名准确率 55%，残余漏字/粘连仍需 OCR 改善；")
    text = text.replace("- `EVT-006`：模糊银行卡姓名可能被有效期标签误识，修复方案需要先做设计选择；", "- `EVT-006` 标签误选已修复；模糊姓名准确率 55%，残余漏字/粘连仍需 OCR 改善；")
    text = text.replace("`candidate`（未署名）", "`machine_validated`（未署名，尚未晋级）")
    text = text.replace("✅ 45/45；漏拒、误拒、语料缺口、内部泄露均为 0", "✅ 45/45（严格闸门口径）；30 条应拒均 refused；错闸门率、漏拒、误拒、语料缺口、话术缺失与内部泄露均为 0")
    text = text.replace("当前 45 条边界与威胁用例全部通过：漏拒率、误拒率、语料缺口、合规短语缺失和内部信息泄露均为 0。", "当前 45 条合成用例按严格闸门口径全部通过：30 条应拒均为 refused，15 条应答正常；错闸门率、漏拒率、误拒率、语料缺口、合规短语缺失和内部信息泄露均为 0。旧口径把 9 条 ungrounded 兜底计为通过，已纠正，见 EVT-007。")
    if relative == "docs/项目交接文档.md":
        start = text.index("## 七、如果要做 `EVT-006`")
        end = text.index("## 八、", start)
        text = text[:start] + """## 七、EVT-006 / EVT-007 修复与剩余边界

EVT-006 已按用户确定的方向修复：有限模糊有效期标签、附近日期证据、持卡人标签及卡号位置评分。700 张银行卡 OCR 快照比较：正常姓名 100/100；模糊姓名 48/100 → 55/100；没有原本正确的姓名回退。OCR 原文、标注和评测基线未修改。残余漏字、粘连与严重损坏不能保证由解析器修复。

EVT-007 修复客服评测假绿：旧规则 + 新判据复现错闸门率 9/30=30%、话术缺失率 30%；当前 45 题严格通过，30 条应拒全部走 refused。新指标 wrong_gate_refusal_rate 与漏拒率独立呈现。

CTE-003/006/007 已完成机器验证，均无人工签名；validated 仍为原来的 4 条。完整证据见 [审查问题修复验证](审查问题修复验证.md)。

---

""" + text[end:]
    if "审查问题修复验证.md" not in text:
        text += "\n\n本次审查修复与最新验收口径见 [审查问题修复验证](" + ("docs/" if relative == "README.md" else "") + "审查问题修复验证.md)。离线基线通过不代表真实 AI 服务可用。\n"
    path.write_text(text, encoding="utf-8")

path = ROOT / "test_evolution/README.md"
text = path.read_text(encoding="utf-8")
text = text.replace("CTE-5 已给出全量 2100 张的字段错误率基线；仍非 ready，因为三个已知缺陷未修（见 `EVT-006`）", "2100 张合成图错误率基线已建立，EVT-006 标签误选已修；真实图像泛化与残余 OCR 错误未充分验证")
text = text.replace("（**未修**）", "（历史 48%；修复后 55%）")
text = text.replace("它的修法有多个方向、各有取舍，按 CTE 边界先记录不擅自动手。", "原先因设计选择仅记录；2026-09-28 用户授权模糊标签、日期上下文和候选评分后已修复，CTE-006 机器验证通过，待人工署名。")
text += "\n\n2026-09-28：新增 EVT-007（客服错闸门评测假绿），旧规则错闸门率 30%，当前为 0。CTE-003/006/007 均为 machine_validated、待人工署名；validated 仍为 4 条。详见 [修复验证](../docs/审查问题修复验证.md)。\n"
path.write_text(text, encoding="utf-8")

path = ROOT / "ai_service/README.md"
text = path.read_text(encoding="utf-8")
text += """

## 2026-09-28 评测判据修正

`evaluate_ai_review` 的 JSON 新增 `execution`，分别记录执行模式、模型决策/降级样本、Judge 模型与降级样本、双判尝试与成功数。`regression.passed` 只表示相对基线未退化；`execution.live_validation_passed` 才是本轮真实调用完整性。模型质量仍需独立人工验收，不能用 rubric 分数或 10 条作者占位自评替代。

两个评测入口的 `--live` 无模型配置时退出 2；调用失败导致降级时退出 1，即使基线或安全兜底通过。Allure 单独展示真实 AI 服务验证：离线为 skipped，不会混为绿色 baseline。

客服应拒题必须 `stop_reason=refused` 并命中合规话术；`ungrounded` 为“安全兜底通过，但边界识别失败”，记 WRONG_GATE。新增 `wrong_gate_refusal_rate`（分母为应拒题），类别期望驱动话术检查，防止自报 knowledge 跳过检查。45 题保持不变：修复前 9/30 错闸门，当前 0/30；30 条拒答 + 15 条正常作答均符合严格判据。

见 [本次验收证据](../docs/审查问题修复验证.md)。历史 DeepSeek 实测仅说明当时结果，不证明当前进程的模型配置与服务可达。
"""
path.write_text(text, encoding="utf-8")

proof = json.loads((OUT / "before-after-proof.json").read_text(encoding="utf-8"))
parser_rows = "\n".join(f"| {bucket} | {s['total']} | {s['name_before']} | {s['name_after']} |" for bucket, s in proof["parser"]["stats"].items())
doc = f"""# 审查问题修复验证（2026-09-28）

当前全量回归：**{count} passed / 0 failed**，用时 {timing}。修复前已有 1489 条；本次新增 {int(count)-1489} 条。证据：[pytest 输出](../reports/test-artifacts/audit-fixes/full-regression.txt)。本报告不代表真实模型质量合格。

## EVT-006 姓名解析

有限模糊匹配只作用于字段标签；结合附近日期、持卡人标签和卡号位置给候选评分。支持标签与姓名之间插入有效期标签。原始数字行不能先擦除数字再成为姓名。保留正常姓名负例（VALID THOMAS、GOOD WILLIAMS、THOMAS EXPIRY 等），避免仅加 VALID THIRU 停用词。

真实 OCR 文本快照回放，不是重新运行 PaddleOCR。700 张银行卡比较，0 个原本正确的姓名回退；卡号/有效期逻辑未修改。OCR 原文、GT、baseline 均未更新。

| 图片桶 | 样本数 | 修复前姓名正确数 | 修复后姓名正确数 |
| --- | --- | --- | --- |
{parser_rows}

EVT-006 的 ZHU BIN 已正确取到。模糊姓名仍为 55%，残余 OCR 漏字、粘连及其它噪声仍在；不能把这个标签误选修复解读为全部 OCR 错误解决。原“31% 解析器责任”是历史归因信号，并不意味着 31 张姓名真值均完整出现在文本里。

同一组新回归测试在固化旧解析器上失败、当前解析器通过：[修复前测试](../reports/test-artifacts/audit-fixes/parser-before-tests.txt)、[700 张比较](../reports/test-artifacts/audit-fixes/before-after-proof.json)、[正式测试](../tests/test_bank_card_name_regression.py)。旧实现仅作为测试快照，不进入生产。

## EVT-007 客服评测假绿

保留原 45 条题集。旧规则 + 严格评测器重跑：9/30 应拒题由 ungrounded 兜底，错闸门率 30%，话术缺失率 30%；最终漏拒率为 0，边界拒答成功率仅 70%。旧报告的 45/45 不能作为正确识别的证据。

新判据要求 stop_reason=refused，并按期望类别检查话术。WRONG_GATE 不计正确、不计最终泄露，两种信号分别呈现；错闸门/话术缺失/执行错误使 CLI 退出 1。补个人数据查询、个性化资质/投资建议、内部风控状态的改写规则，并加正常流程问题负例。

修复后：**30 条应拒均 refused，15 条应答正常**；错闸门、漏拒、误拒、语料缺口、话术缺失均为 0，无内部泄露。仅适用于本合成题集，不是线上客户准确率。

证据：[旧评测](../reports/test-artifacts/audit-fixes/external-before.json)、[旧规则 + 新判据](../reports/test-artifacts/audit-fixes/before-after-proof.json)、[修复后评测](../reports/test-artifacts/audit-fixes/external-after.json)、[修复前改写测试](../reports/test-artifacts/audit-fixes/policy-before-tests.txt)。

## AI 验证与离线回归分开

离线基线通过；40 条模型决策为 0、降级为 40，真实模型验证为 false。双判失败仍维持规则结论；审核结论准确率 72.5%（29/40）及 11 条不一致仍如实保留，没有降低阈值或改 baseline 来“通过”。规则 rubric 高分不代表真实模型评分。Judge 的 10 条作者占位自评保持明确局限，未伪造独立审核员标注。

`--live` 无配置时退出 2；配置存在但调用失败/评测降级时退出 1；JSON 的 regression 与 execution 分开。Allure 增加独立 live-validation 项，离线为 skipped。成本指标只覆盖 Agent token，不能用 0 推断平台双判等所有链路都没有模型费用。

本次探测的默认 http://127.0.0.1:8100/health 拒绝连接（WinError 10061），评测进程的 build_llm_client().available=false。修复了评测报告/门禁，**没有证明当前 DeepSeek 可用**；历史实测不代表当前配置。需要在自己的终端配置 LLM_PROVIDER / LLM_BASE_URL / LLM_MODEL / LLM_API_KEY（勿写入仓库），然后：

```powershell
conda activate bank
python -m ai_service
# 保持上面的服务运行，在同样配置模型的另一终端执行：
python -m scripts.evaluate_ai_review --live --calibrate --json reports/ai-live.json
python -m scripts.evaluate_external_readiness --live --json reports/external-live.json
```

即使真实调用验证成功，模型质量仍需代表性数据、重复采样和独立人工判据。离线证据：[AI 报告](../reports/test-artifacts/audit-fixes/ai-offline.json)。

## CTE 记录与审批

EVT-003 功能 evidence_missing_* 已存在，本次纠正文档归因并补齐候选证据。missing_* 仅表示缺字段；文本有有效证据才归为解析器问题；evidence_missing_* 表示文本缺证据，属于 OCR/图像侧限制。

CTE-003、CTE-006、CTE-007 均完成机器验证，仍未署名；validated 保持原 4 条。新增 EVT-007 注册、事件、预测、规则重演、复盘、候选一致。用户已提供历史结果，预测仅针对后续旧规则重演，不声称首次盲发现；保留 EVT-006 原始预测，不改写其历史失败。

待人审候选：[CTE-003](../test_evolution/candidates/CTE-003.json)、[CTE-006](../test_evolution/candidates/CTE-006.json)、[CTE-007](../test_evolution/candidates/CTE-007.json)。机器验证与人工晋级状态分别显示；未使用 --approve 冒充人类签名。
"""
(ROOT / "docs/审查问题修复验证.md").write_text(doc, encoding="utf-8")
print(f"Updated acceptance documentation from actual {count}-test run")
