# Bank OCR Test Platform

一个面向银行远程开户、信用卡进件场景的影像资料审核测试平台。项目基于 FastAPI，覆盖银行卡和身份证图片从上传、质量检测、OCR、字段解析、规则审核到审核记录追踪的完整测试流程，并在此之上叠加了一套**可解释、可评测、可回放的 AI 能力层**。

当前版本是用于接口测试、规则验证、OCR 适配和性能基线验证的测试开发 Demo，不是生产级银行系统。

## 这个项目最有价值的部分：AI 能力层怎么测

平台本身（OCR + 规则 + 记录）是**确定性的**，用传统接口测试就够了。
真正有方法论价值的是后加的这一层：**一个会调工具、多步决策、输出不确定的 AI 系统，要怎么测。**

围绕这个问题，项目交付了四件事，每件都对应一个具体的困难：

| 困难 | 做法 | 具体产物 |
| --- | --- | --- |
| 模型会编、会跑偏，没法断言「它做对了」 | 断言**轨迹**而不是答案：工具序列 / 参数 / 步数 / 预算 | `agentkit.Trajectory` |
| 会调模型就烧钱、且结果不确定，CI 没法跑 | **录制回放**：未命中直接失败，绝不回退联网 | `cassette.py` |
| 系统挂了不能拖垮主链路 | AI 是**独立服务**，平台侧超时 + 三态熔断 + 降级 | `ai_client.py` |
| 「答得对不对」是要量化的 | **四层指标** + 回归门禁（退化 >5% 打红 CI） | `eval/` + `scripts/evaluate_ai_review.py` |

一句话概括这套东西的立场：**AI 的失败往往是「输出看起来对、过程是错的」，
所以评测必须看过程，而且要能在不联网、不花钱的前提下重复跑。**

细节见 [`ai_service/README.md`](ai_service/README.md)。

## 第五件事：失败如何变成资产（P5 / CTE）

上面四件事解决的是「怎么测」。还剩一个问题：**系统每次发现的新 Bug、
新失败模式，能不能转化为下一轮可复用、可验证的长期测试资产？**

`test_evolution/`（Continuous Test Evolution）做这件事。它的边界写得很死：

| CTE 可以 | CTE 不可以 |
| --- | --- |
| 发现问题、生成测试、总结 Failure Pattern | 修改 Ground Truth、Evaluator |
| 提出规则修改建议、执行测试、历史回放 | 修改安全不变式、删除失败测试 |
| 生成复盘报告 | 修改 Promotion Policy、宣布候选通过 |

落地方式不是文件权限（单人仓库里那只是自欺），而是
**代码里不存在指向这些资产的写路径** + **所有 Candidate 都必须人工署名才能晋级**。

第一个闭环（CTE-1）已经跑通，用的是对外威胁集**真实抓到**的那次漏判：
「我征信上有什么问题」被判成普通咨询并作答。完整链路
`Event → 冻结历史预期 → Execute → Compare → Reflect → Candidate → Validate → Promote`
的产物全部落在 `test_evolution/` 下，晋级出的回归资产进了
`ai_service/tests/test_cte_regressions.py`。

> 口径修正：旧 CTE 的预测复制了已知事件的预期，只能算历史预期回放。新主动发现入口 `python -m scripts.discover_cte` 才通过独立模型请求与数据白名单冻结预测。**回归测试的价值在于修复前 FAIL**，而不是
> 「pytest 通过」—— 一个永远 `assert True` 的测试同样通过，但它不是资产。

细节与诚实清单见 [`test_evolution/README.md`](test_evolution/README.md)。

主动演进的审核结论与 CMD 命令见 [CTE 主动测试演进使用说明](docs/CTE主动测试演进使用说明.md)。当前实现知识层迭代；未执行模型微调，也未宣称已证明经验能提高泛化能力。

## 功能一览

**平台侧（确定性链路，Mock/PaddleOCR 双模式）**

默认使用 mock OCR，适合接口、规则、CI 和性能基线测试；设置 `OCR_MODE=paddle` 后使用真实 PaddleOCR 推理。

- 银行卡审核：解析银行卡号、有效期、持卡人姓名
- 身份证审核：自动判断正面/反面，并解析对应字段
- 审核可追踪：为每次请求生成 `request_id`，并将审核记录写入 SQLite
- 审核可解释：返回图片质量原因码和最终审核原因码
- 日志安全：银行卡号和身份证号写入日志前自动脱敏
- 测试工程：pytest、Allure、Locust 和 GitHub Actions
- 前端审核页：上传图片后展示审核结果、质量结果、字段、OCR 文本和原始 JSON
- 用户/管理端门户：登录、角色权限、CSRF 防护、审核记录查询与 AI 解释面板

**AI 能力层（独立服务 `ai_service/`，默认 127.0.0.1:8100）**

审核员看到的是原因码 `image_blur`，得自己翻文档才知道该怎么办。AI 层把这条记录变成
「为什么是这个结论、根因在哪一层、接下来该做什么」，并给出**带真实阈值和实现位置**的引用。

- **审核 Agent**：多步工具决策，白名单硬约束（4 个只读工具），三重预算（步数/token/工具调用）
- **客服 Agent**：第二个产品面，回答业务规则、材料清单、办理流程；两道出口闸门挡住越界与幻觉
- **多轮会话**：支持「那需要什么材料」这类指代追问，会话状态放调用方、服务端不留
- **MCP 接入**：以本地 stdio 暴露 `review_explain` / `knowledge_ask` 两个窄工具，复用既有脱敏、预算和合规闸门
- **可降级**：没配 API key 也能跑，降级只损失表达质量，**事实准确性不受影响**
- **可评测**：四层指标（工具/任务/解释/成本）+ judge 校准 + baseline 回归门禁
- **可回放**：cassette 录制真实模型交互，CI 离线复现，不联网不花钱

## 功能流程

```text
上传图片
 -> 文件校验
 -> 图片质量检测
 -> OCR 文字识别（默认 mock，可切换 PaddleOCR）
 -> 按接口类型解析字段
 -> 规则审核
 -> 生成质量原因码和审核原因码
 -> 审核记录写入 SQLite
 -> 通过 request_id 或条件查询追踪
```

核心接口：

```http
POST /bank-card/review
POST /id-card/review
GET /review-records/{request_id}
GET /review-records?doc_type=bank_card&review_result=review
```

返回内容包含：

- `request_id`：本次审核请求的唯一追踪标识
- `review_result`：最终审核结果，可能是 `pass`、`review`、`reject`
- `review_reasons`：最终审核原因码数组；无异常时为空数组
- `quality`：图片质量检测结果
- `quality.quality_reasons`：图片质量原因码数组；质量正常时为空数组
- `ocr_text`：OCR 识别出的原始文字行
- `fields`：从 OCR 文本中解析出的结构化字段
- `side`：身份证接口专用，表示 `front`、`back` 或 `unknown`

## 审核原因码

接口使用稳定的英文原因码支持人工复核、自动化断言、日志定位和审核记录查询。

| 原因码 | 含义 |
| --- | --- |
| `image_blur` | 图片模糊，需要人工复核 |
| `image_dark` | 图片过暗，需要人工复核 |
| `image_bright` | 图片过亮，需要人工复核 |
| `glare_detected` | 检测到反光，需要人工复核 |
| `image_rotated` | 多个文字区域的倾角一致且绝对值 > 1°，需要人工复核 |
| `image_occluded` | 检测到大块深色、低纹理、矩形遮挡疑似区域，需要人工复核 |
| `severe_image_blur` | 严重模糊（方差 < 30），直接拒绝 |
| `severe_image_dark` | 严重过暗（灰度 < 35），直接拒绝 |
| `severe_image_bright` | 严重过亮（灰度 > 215），直接拒绝 |
| `missing_card_number` | 未解析到银行卡号 |
| `missing_valid_date` | 未解析到银行卡有效期 |
| `evidence_missing_<字段>` | 该字段缺失**且 OCR 文本里也没有证据** —— 归因信息，见下 |
| `invalid_card_number` | 银行卡号未通过规则校验 |
| `unknown_id_card_side` | 无法判断身份证正反面 |
| `invalid_file_type` | 上传文件类型不受支持 |
| `unreadable_image` | 文件为空、损坏或不是可读取图片 |
| `invalid_ocr_mode` | 服务端 `OCR_MODE` 配置非法 |

遮挡与旋转通过 `app/image_geometry.py` 分析上传图片像素；文件名、数据目录和标注不参与判定。
即使 OCR 字段完整，命中上述两类原因码也不能自动通过，文本 AI 不会消除这两项影像证据。
旋转容差为 1°，并覆盖侧转 90°；目前不能可靠识别倒置 180°。
遮挡检测覆盖当前合成数据中的深色块，不能保证识别手指、浅色或纹理复杂的遮挡；合法深色矩形装饰也可能需要人工确认。
程序异常现在会保存文件名、OCR 模式和错误信息，可用响应中的 `request_id` 在管理端查询。

### 字段缺失的归因：`evidence_missing_*`

`missing_card_number` 这类原因码把两种**完全不同的故障**塌缩成了一个信号：

| 原因码组合 | 含义 | 该改哪里 |
| --- | --- | --- |
| `missing_x` + `evidence_missing_x` | OCR 文本里**根本没有** x 的证据 | 图像侧（采集/模型） |
| 只有 `missing_x` | 文本里有 x，但解析规则没取到 | 解析代码 |

判据见 [`app/ocr_evidence.py`](app/ocr_evidence.py)。它**刻意宽于解析器** ——
回答的是「值在不在文本里」，不是「值合不合法」：`13/45` 这种非法月份
也算有证据，否则解析器的责任又会被推给采集。

姓名、住址、签发机关这类文本字段没有可靠的字面模式，**一律按解析器责任处理**
（不产出归因码）。这个方向是刻意选的：归错给解析器的代价是一次排查，
归错给采集的代价是**改错地方**。

> 这个信号是 CTE 演进出来的：CTE-3 曾把 `id_number 0/10` 整个归因给解析器，
> 修完解析器数字没变 —— 才发现 8/10 的样本里 OCR 压根没产出那串数字。
> 有了归因码，CTE-4 立刻**自动**指出了下一个同类缺陷（`出生` 标签被截断）。

### 严重退化 → 直接拒绝

普通的质量退化（模糊/偏暗/偏亮/反光）只是「转人工」，因为重拍之外仍需人看一眼。
但**极端**退化重拍之外没有补救手段，继续走人工复核只会浪费审核工时，因此直接拒绝：

| 轴 | 转人工门槛 | 直接拒绝门槛 |
| --- | --- | --- |
| 清晰度（拉普拉斯方差） | < 80 | < 30 |
| 偏暗（灰度均值） | < 65 | < 35 |
| 偏亮（灰度均值） | > 210 | > 215 |
| 反光（最大高光连通域占比） | > 0.5% | **暂未启用**，一律转人工 |

这些门槛是**在 40 条带人工结论的样本上标定的**，不是行业标准 —— 换一批样本应重新标定。
反光轴的标定间隙只有 9%（最高「该复核」0.0213 与最低「该拒绝」0.0232），
据此判拒绝属于过拟合，所以暂不启用。阈值定义见 `app/quality_check.py`，
AI 侧同口径副本见 `ai_service/thresholds.py`，两侧由一致性测试强制同步。

## 项目结构

```text
app/
  main.py           FastAPI 入口和接口定义
  ai_client.py      AI 服务客户端（超时 + 三态熔断 + 降级 + 脱敏）
  ocr_service.py    PaddleOCR 集成层
  quality_check.py  图片模糊、亮度、反光检测
  field_parser.py   银行卡字段解析
  id_card_parser.py 身份证正反面检测和字段解析
  rule_check.py     审核规则判断
  ocr_evidence.py   字段缺失归因（CTE-4：区分「OCR 没认出来」与「解析器没取到」）
  review_records.py SQLite 审核记录持久化和查询
  logging_utils.py  银行卡号、身份证号日志脱敏
  static/           银行卡审核前端页面 + 用户/管理端门户（含 AI 解释面板）

ai_service/         AI 能力层（独立进程，HTTP 调用）
  api.py            FastAPI 接口（/explain、/agent/explain、/knowledge/ask、/search、/health）
  mcp_server.py     MCP stdio 接口（审核解释 + 知识问答，默认不开网络端口）
  agent.py          审核 Agent：planner + executor 的多步工具决策循环
  knowledge/        客服 Agent：语料 / 策略 / 工具 / prompt / 会话 / 循环 / 接口
  tools.py          审核侧工具白名单与实现
  corpus.py         知识库语料（唯一事实来源，含真实阈值与实现位置）
  retrieval.py      切片 / 分词 / BM25 + 哈希向量混合检索
  tool_manager.py   工具框架：改写、并行召回、去重、重排、熔断、缓存、降级
  llm.py            可插拔 LLM 适配层（openai / anthropic / none）
  cassette.py       LLM / 工具的录制回放（未命中直接失败，绝不回退联网）
  agentkit.py       测试工具箱：轨迹断言、故障注入、cassette 装配
  eval/             评测层：golden 集、四层指标、judge 校准、回归门禁
  devtools/         协议靶子：本地假 provider，验证 HTTP / 鉴权 / usage 解析
  tests/            566 个单测，全部离线可跑
  README.md         AI 能力层的完整设计说明（推荐先读这个）

test_evolution/     CTE：失败 → 测试资产的演进闭环（孵化器，不持有测试数据主权）
  schema.py        Event / Prediction / Candidate 的契约 + 按类型的验证矩阵
  readiness.py     哪些面现在就能做演进（唯一事实来源）
  replay.py        历史重演：在已修复的系统上把旧行为跑出来
  ocr_snapshot.py  真实 OCR 的录制/回放（CTE-2：让评测用上真实字段）
  ocr_report.py    字段错误率基线（CTE-5）
  pipeline.py      闭环编排
  README.md        CTE 的设计、边界与诚实清单
docs/baseline_migrations/  基线口径变更的记录（001 真实 OCR / 002 严重度优先 / 003 全量扩样）

tests/              pytest 测试（平台侧 562 项；CTE 侧另见 test_evolution/tests，140 项）
data/               测试数据、标注数据、生成数据
reports/            测试输出、临时上传文件、OCR 模型缓存
scripts/            数据生成、处理与评测脚本（含 run_cte.py）
docs/               方案文档、阶段开发报告、人工标注操作手册
  项目交接文档.md   ← **接手先读这个**（怎么跑、东西在哪、坑在哪）
  项目现状总结.md   这是什么 / 做完什么 / 诚实的自我评价
  CTE开发报告.md    P5 的完整记录
  ocr_field_error_rates.md  全量 2100 张的字段错误率基线
```

## 环境准备

建议使用项目已有的 conda 环境：

```powershell
conda activate bank
```

安装依赖：

```powershell
python -m pip install -r requirements.txt
```

如果 PaddleOCR 相关依赖没有安装完整，可以单独安装：

```powershell
python -m pip install paddlepaddle paddleocr
```

验证 PaddlePaddle：

```powershell
python -c "import paddle; print(paddle.__version__)"
```

验证 PaddleOCR：

```powershell
python -c "from paddleocr import PaddleOCR; print('paddleocr ok')"
```

## 启动服务

默认 mock OCR 启动：

```powershell
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
```

如果要让接口和前端页面使用真实 PaddleOCR，先设置环境变量。

PowerShell：

```powershell
$env:OCR_MODE="paddle"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

cmd / Anaconda Prompt：

```cmd
set OCR_MODE=paddle
uvicorn app.main:app --host 127.0.0.1 --port 8001
```

或一行：

```cmd
set OCR_MODE=paddle && uvicorn app.main:app --host 127.0.0.1 --port 8001
```

浏览器打开：

```text
http://127.0.0.1:8001
```

银行卡前端页面：

```text
http://127.0.0.1:8001/bank-card/ui
```

接口文档：

```text
http://127.0.0.1:8001/docs
```

如果 `8000` 端口启动失败，可以换成 `8001` 或其他未占用端口。

### 启动 AI 能力层（可选）

AI 是**独立进程**，平台侧默认连 `http://127.0.0.1:8100`。
**不启动它平台照常工作** —— AI 不可用时审核链路不受影响，只是没有解释面板。

```powershell
python -m ai_service                    # 起服务，默认 127.0.0.1:8100
```

**不需要任何 API key 也能跑**：没配 key 时自动切到确定性策略，
降级只影响措辞，事实内容（阈值、处置建议）完全不变。

```powershell
python -m ai_service --demo             # 自检：不联网跑一次完整链路
python -m ai_service --agent            # 审核 Agent 路径，打印轨迹/预算/token
python -m ai_service --search "反光了怎么办"   # 只看检索效果
python -m ai_service --ask "办理二类账户需要哪些材料"   # 客服 Agent
```

要看真实模型的完整链路，显式加 `--live`（不加就绝不出网、绝不花钱）：

```powershell
$env:LLM_API_KEY="..."
python -m ai_service --agent --live
```

没有 key 也想验证真实 HTTP 链路（鉴权头、响应解析、usage 抽取），
可以起一个**本地协议靶子**代替 provider：

```powershell
python -m ai_service.devtools.mock_llm --port 8137
# 另一个终端：
$env:LLM_PROVIDER="openai"; $env:LLM_API_KEY="dev"; $env:LLM_BASE_URL="http://127.0.0.1:8137/v1"
python -m ai_service --agent --live
```

> 靶子证明的是「协议通、链路通、用量能取到」，**不证明「模型答得好」**。

## 使用接口

### 使用前端页面

启动服务后打开：

```text
http://127.0.0.1:8001/bank-card/ui
```

页面会调用同源后端接口：

```http
POST /bank-card/review
```

验证步骤：

1. 点击“选择银行卡图片”，或把图片拖到上传区域
2. 推荐先选择：

```text
data/processed/bank_card/normal/bank_card_0001.png
```

3. 点击“开始审核”
4. 右侧应展示审核结果、质量结果、字段、OCR 文本和响应 JSON

正常样本预期：

- `review_result` 为 `pass`
- `quality_result` 为 `pass`
- `card_number`、`name`、`valid_date` 能解析出来

异常质量样本可用于验证人工复核：

```text
data/processed/bank_card/blur/bank_card_0001.png
data/processed/bank_card/dark/bank_card_0001.png
data/processed/bank_card/bright/bank_card_0001.png
data/processed/bank_card/glare/bank_card_0001.png
```

这些样本通常会返回 `review`，并在质量字段中显示模糊、过暗、过亮或反光。

### 使用 Swagger 文档

打开 `/docs` 后：

1. 展开要测试的接口，例如 `POST /bank-card/review` 或 `POST /id-card/review`
2. 点击 `Try it out`
3. 选择一张图片
4. 点击 `Execute`
5. 查看 `Server response`

### 银行卡接口

银行卡图片使用：

```http
POST /bank-card/review
```

银行卡接口会解析：

- `card_number`：银行卡号
- `valid_date`：有效期，格式如 `12/30`
- `name`：持卡人姓名

示例返回：

```json
{
  "request_id": "53f2d96d-0634-40ca-8fe4-12c963ef5ff0",
  "review_result": "pass",
  "review_reasons": [],
  "quality": {
    "is_blur": false,
    "brightness": "normal",
    "has_glare": false,
    "quality_result": "pass",
    "quality_reasons": []
  },
  "ocr_text": [
    "TEST BANK",
    "6222 0202 0202 0001",
    "VALID THRU 12/30",
    "ZHANG SAN"
  ],
  "fields": {
    "card_number": "6222020202020001",
    "valid_date": "12/30",
    "name": "ZHANG SAN"
  }
}
```

### 身份证接口

```http
POST /id-card/review
```

身份证接口会自动检测正反面：

- `side: "front"`：身份证正面，解析姓名、性别、民族、出生日期、住址、身份证号
- `side: "back"`：身份证反面，解析签发机关、有效期限
- `side: "unknown"`：无法判断正反面，需要人工复核

身份证正面示例返回：

```json
{
  "request_id": "d469ad56-ee44-49e1-a8e3-051594784907",
  "review_result": "pass",
  "review_reasons": [],
  "side": "front",
  "quality": {
    "is_blur": false,
    "brightness": "normal",
    "has_glare": false,
    "quality_result": "pass",
    "quality_reasons": []
  },
  "ocr_text": [
    "姓名 李雷",
    "性别 男 民族 苗",
    "出生 1986年1月22日",
    "住址 安徽省月江市城东区文昌街64号",
    "公民身份号码 110101198601220011"
  ],
  "fields": {
    "name": "李雷",
    "gender": "男",
    "nation": "苗",
    "birth": "1986-01-22",
    "address": "安徽省月江市城东区文昌街64号",
    "id_number": "110101198601220011"
  }
}
```

身份证反面示例返回：

```json
{
  "request_id": "eed3acb5-f51a-4519-8ccd-a5782c96dc22",
  "review_result": "pass",
  "review_reasons": [],
  "side": "back",
  "quality": {
    "is_blur": false,
    "brightness": "normal",
    "has_glare": false,
    "quality_result": "pass",
    "quality_reasons": []
  },
  "ocr_text": [
    "中华人民共和国",
    "居民身份证",
    "签发机关 月江市公安局",
    "有效期限 2020.01.01-2040.01.01"
  ],
  "fields": {
    "issue_authority": "月江市公安局",
    "valid_period": "2020.01.01-2040.01.01"
  }
}
```

## 审核记录与查询

银行卡和身份证审核都会将成功或失败结果写入 SQLite，并使用响应中的 `request_id` 关联接口响应、日志和审核记录。

按 `request_id` 查询单条记录：

```http
GET /review-records/53f2d96d-0634-40ca-8fe4-12c963ef5ff0
```

按证件类型和审核结果筛选记录：

```http
GET /review-records?doc_type=bank_card&review_result=review
```

查询结果包含证件类型、文件名、OCR 模式、审核结果、质量结果、`quality_reasons`、`review_reasons`、解析字段、错误信息和创建时间。

默认数据库文件位于：

```text
reports/review_records.db
```

该数据库属于本地运行时文件，已经通过 `.gitignore` 排除，不提交到 Git。SQLite 适合当前单机测试和面试演示，不适合作为生产级银行系统的高并发审核存储。

并发写入上做了三件事：建表 / 建索引语句对每个库文件**只执行一次**（原先挂在每次读写之前，是 40 / 80 并发下 `database is locked` 的主因）、写连接在**进程内排队**、新库启用 **WAL** 并把 busy timeout 提到 30 秒。只读连接不排队，因此 WAL 的读并发不会被写队列挡住。实测 mock OCR 下 40 / 80 并发由 73 次失败降为 0，40 并发 P95 由 5073 ms 降到 712 ms。**排队只覆盖单进程** —— 多 worker 部署仍依赖 SQLite 自身的忙等待，这一点没有假装解决；完整证据与边界见 [SQLite 写锁修复验证](docs/SQLite写锁修复验证.md)。

## 日志脱敏

接口日志记录请求接收、文件校验、质量检测、OCR、字段解析、规则审核和审核记录保存等关键步骤，并携带 `request_id` 便于定位。

银行卡号和身份证号在进入日志前会脱敏，只保留用于问题定位的前后部分，例如：

```text
银行卡号：622202******0001
身份证号：110101********0011
```

完整银行卡号和完整身份证号不会写入应用日志。测试数据也应使用合成资料，不得提交真实客户影像或身份信息。

## 运行测试

运行全部测试：

```powershell
python -m pytest -v
```

当前全量测试结果为 **1539 passed / 0 failed / 0 errors**，见 [最新完整输出](reports/test-artifacts/session-fix/full-regression.txt)。

**全部离线可跑，不需要任何 API key。** AI 服务侧默认走确定性序列，
需要真实模型时必须显式加 `--live`。普通 pytest 会清理外部 `OCR_MODE` 环境变量
并使用 mock OCR，不会下载或加载真实 PaddleOCR 模型；GitHub Actions 同样固定使用 mock 路径。

只跑 AI 服务侧的测试（更快，约 10 秒）：

```powershell
python -m pytest ai_service/tests -q
```

跑 AI 评测与回归门禁：

```powershell
python -m scripts.evaluate_ai_review                 # 四层指标 + baseline 比对
python -m scripts.evaluate_ai_review --calibrate     # 额外跑 judge 校准
```

启动 MCP server（本地 stdio；由 MCP host 作为子进程拉起）：

```powershell
python -m ai_service.mcp_server
```

对外只注册两个工具：

- `review_explain`：解释审核结论；传入的证件字段和问题文本会在进入 Agent 前强制脱敏。
- `knowledge_ask`：回答公开银行业务知识并支持无状态多轮；个人数据、征信、内部口径等请求仍由原合规闸门拒答。

默认不用 Streamable HTTP，避免无意中增加一个未认证的网络入口。需要接入 MCP host 时，
把上述命令配置成 stdio server 即可。

跑 CTE 闭环：

```powershell
python -m scripts.run_cte --list                     # 列出已登记事件
python -m scripts.run_cte --event EVT-001            # 跑完整闭环（不晋级）
python -m scripts.run_cte --event EVT-001 --approve jb   # 带人工批准晋级
python -m scripts.run_cte --replay "我征信上有什么问题"    # 只看历史重演对比
```

录制 / 校验 OCR 快照（**需真实 PaddleOCR，不进 CI**）：

```powershell
python -m scripts.record_ocr_snapshot --dry-run      # 先看会录什么
python -m scripts.record_ocr_snapshot                # 录制并写入快照
python -m scripts.record_ocr_snapshot --verify       # 重跑并 diff，不覆盖
```

评测默认使用 OCR 快照作为字段输入（`--no-ocr-snapshot` 可切回标注真值做对照）。

> Windows 上如果 teardown 报 `SHFileOperationW`，加环境变量
> `CODEBUDDY_SAFE_DELETE_ENABLED=0` 再跑。

## OCR 小规模评估

项目提供银行卡 OCR 评估脚本：

```text
scripts/evaluate_bank_card_ocr.py
```

该脚本读取：

```text
data/annotations/labels.json
```

只评估 `doc_type=bank_card` 且 `quality_type=normal` 的样本，并输出：

```text
reports/bank_card_ocr_evaluation.csv
```

Mock 模式评估：

```powershell
conda run -n bank python scripts\evaluate_bank_card_ocr.py --mode mock --limit 10
```

真实 PaddleOCR 模式评估：

```powershell
conda run -n bank python scripts\evaluate_bank_card_ocr.py --mode paddle --limit 10
```

输出指标包括：

- 总样本数
- 成功推理数
- 失败数
- `card_number` 字段准确率
- `name` 字段准确率
- `valid_date` 字段准确率
- 全字段完全正确比例

注意：mock 模式固定返回一组测试 OCR 文本，用于流程回归，不代表真实识别效果。真实 PaddleOCR 首次运行可能需要下载或加载模型，耗时明显更长。

### 实测结果（真实 PaddleOCR，100 张）

环境：PaddleOCR 3.7.0 / Paddle 3.3.0，模型 `PP-OCRv6_medium_det` + `PP-OCRv6_medium_rec`，
`--mode paddle --limit 100`（`bank_card` + `quality_type=normal` 的全部样本）。

| 指标 | 结果 |
| --- | --- |
| 推理成功 | 100 / 100 |
| `card_number` 准确率 | **100%** |
| `name` 准确率 | **100%** |
| `valid_date` 准确率 | **100%** |
| 全字段完全正确 | **100%** |

**这个数字必须连前提一起读。** 合成数据集里没有透视畸变、没有传感器噪声、
没有 JPEG 二次压缩、没有重拍屏幕，字体也只有一两种系统字体 —— 也就是说，
这批图对 OCR 而言**比真实证件照简单得多**。100% 说明的是
「平台到 OCR 的链路是通的、字段解析正确」，**不代表真实证件场景的准确率**。

要得到可对外的识别率，需要真实证件照片或至少加入透视/噪声/压缩的采集仿真。
在此之前，本项结论只作「链路可用性」证据，不作「识别能力」证据。

## CI 自动化测试

GitHub Actions workflow 位于：

```text
.github/workflows/tests.yml
```

CI 使用 Ubuntu runner 和 Python 3.10，不依赖本机 conda 环境。流程为：

1. 拉取仓库代码
2. 安装 `requirements.txt`
3. 执行 `python -m pytest`

CI 当前只运行 pytest 自动化测试，不启动真实 PaddleOCR 推理服务、不运行 Locust 压测，也不启动 Allure 服务。

本地复现 CI 测试：

```powershell
python -m pip install -r requirements.txt
python -m pytest
```

测试数据只需要 `data/processed/bank_card/` 下每类前三张样本：

```text
normal, blur, glare, occlusion, rotate, dark, bright
bank_card_0001.png, bank_card_0002.png, bank_card_0003.png
```

不要为了 CI 上传完整合成数据集；完整数据可在本地按需重新生成。

## Locust 性能测试

项目提供最小 Locust 场景：

```text
performance/locustfile.py
```

该场景使用固定合成图片：

```text
data/processed/bank_card/normal/bank_card_0001.png
```

每个虚拟用户会向 `POST /bank-card/review` 上传该图片，并校验：

- HTTP 状态码为 `200`
- 响应 JSON 包含 `review_result`

先启动被测服务，例如：

```powershell
conda run -n bank python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

启动 Locust：

```powershell
conda run -n bank locust -f performance/locustfile.py
```

如果服务端口不是 `8000`，可显式指定：

```powershell
conda run -n bank locust -f performance/locustfile.py --host http://127.0.0.1:8001
```

建议测试场景：

```powershell
conda run -n bank locust -f performance/locustfile.py --headless -u 1 -r 1 --run-time 30s --host http://127.0.0.1:8000
conda run -n bank locust -f performance/locustfile.py --headless -u 10 -r 2 --run-time 1m --host http://127.0.0.1:8000
conda run -n bank locust -f performance/locustfile.py --headless -u 50 -r 5 --run-time 2m --host http://127.0.0.1:8000
```

可选生成 HTML 报告：

```powershell
conda run -n bank locust -f performance/locustfile.py --headless -u 10 -r 2 --run-time 1m --host http://127.0.0.1:8000 --html reports/locust/bank-card-review.html
```

关注指标：

- 请求总数：Locust `Requests`，表示本次压测完成的请求数量。
- 失败率：Locust `Failures` 百分比，非 200、非 JSON 或缺少 `review_result` 都会被记录为失败。
- 平均响应时间：Locust `Average`，单位毫秒，表示接口平均耗时。
- P95 响应时间：Locust `95%ile`，表示 95% 请求在该耗时内完成。
- 吞吐量：Locust `Current RPS` 或 `Requests/s`，表示每秒完成请求数。

注意：当前 Locust 场景默认用于 mock OCR 环境下的接口性能测试，只反映接口链路和审核流程的性能基线，不能代表真实 PaddleOCR 推理性能。只有显式以 `OCR_MODE=paddle` 启动服务时，压测结果才会包含真实 OCR 推理耗时，且应单独解释。

运行单个测试文件：

```powershell
python -m pytest tests/test_bank_card_api.py
python -m pytest tests/test_id_card_api.py
```

测试中会 mock OCR 结果，因此单元测试不依赖真实模型推理。

## OCR 模型缓存

PaddleOCR 第一次运行会下载模型。项目把模型缓存和临时目录放在：

```text
reports/paddlex-runtime-cache/
reports/ocr-temp/
```

这些目录是运行时缓存，不应该作为业务代码提交。若模型缓存损坏或出现权限问题，可以关闭服务后删除缓存目录，再重新启动服务让 PaddleOCR 重新下载：

```powershell
rmdir /s /q reports\paddlex-runtime-cache
rmdir /s /q reports\ocr-temp
```

## 项目限制

- 当前项目是测试开发 Demo，用于展示影像审核测试思路，不是生产级银行业务系统。
- SQLite 只适合单机测试、自动化验证和面试演示，不适合生产环境的并发、容灾和审计要求。
- mock OCR 用于稳定验证接口、字段解析和规则流程，不代表真实图片识别效果。
- 真实 OCR 已在合成集上实测（100 张，字段准确率 100%，见「OCR 小规模评估」），
  但**合成集不含透视畸变/噪声/JPEG 压缩，比真实证件照简单得多** ——
  该数字只作链路可用性证据，真实证件场景的识别率仍未测。
- Locust 默认 mock 模式结果仅代表接口流程性能基线，不代表真实 PaddleOCR 推理性能。
- 项目没有实现生产级权限控制、数据加密、分布式存储、审批工作流和合规审计体系。

**AI 能力层特有的边界**（这几条被刻意写在文档里而不是藏起来）：

- ~~**两件事被数据缺口卡住，不是代码没写完**~~ —— **已解除（CTE-2，2026-09-27）**。
  此前 `fields` 是 labels.json 的**标注真值**、不是 OCR 实际输出，所以
  「字段全部解析成功」恒成立、信号没有区分度。现在评测改用
  `data/annotations/ocr_outputs.json`：50 张 golden 图过真实 PaddleOCR
  录下的观测（含原始文本行、解析字段、质量指标），CI 离线回放。

  **一录就暴露了三类此前完全不可见的缺陷**：模糊图上 `name` 被识别成
  `VALIDTHIRU`（有效期那行串进姓名）；反光图上卡号**单字符**误识
  （`5282448378463572` vs `...573`，正是双判该抓的那类错误）；
  以及**身份证正反面字段解析 100% 失败** —— `id_card_parser` 要求
  「标签 值」同行，而真实 PaddleOCR 把它们检测成两个独立文本框，
  mock 把整段拼成一行所以这个假设从未被检验。
  也就是说，此前所有身份证正面的字段结论都建立在 mock 的拼接行为上。

  完整记录见 [`docs/baseline_migrations/001_real_ocr_fields.md`](docs/baseline_migrations/001_real_ocr_fields.md)。

  **CTE-3 已修掉其中两条**（见下），`verdict_accuracy` 0.775 → 0.675 → **0.725**。

### CTE-3：修掉 CTE-2 暴露出来的缺陷

| 事件 | 结论 | 修复 |
| --- | --- | --- |
| `EVT-002` | 身份证解析要求标签与值同行 | `name`/`address` **0/10 → 9/10**；另修 `id_number` 前导零、`valid_period` 跨行 |
| `EVT-004` | 严重退化被字段缺失降级成转人工 | 判定顺序调整；`verdict_accuracy` **0.675 → 0.725** |
| `EVT-003` | 分不清「OCR 没认出来」与「解析器没取到」 | CTE-4 落地为 `evidence_missing_*` 归因码 |
| `EVT-005` | 出生标签被截断（`出1996年1月12日`） | 判据与住址统一；**由归因信号自动指出** |
| `EVT-006` | 银行卡模糊样本姓名被认成 `VALID THIRU` | 已修复有效期标签误选；姓名候选评分，CTE-006 待人工署名 |

### CTE-5：把错误率变成基线

CTE-2 的快照只覆盖每桶 5 张 —— 一张图就是 20 个百分点，模式看不出来。
CTE-5 扩到全量 **2100 张**，产出 [`docs/ocr_field_error_rates.md`](docs/ocr_field_error_rates.md)
（脚本生成，纯离线可重跑）。扩完之后立刻显形两个系统性缺陷，其中一个已修：

| 发现 | 证据 | 状态 |
| --- | --- | --- |
| 身份证姓名的**值有时在标签之前** | 316/700 张；`name` 52/100 → **97/100** | 已修 |
| 号码与相邻行数字粘连导致漏取 | 住址行尾 `...215` + 号码行 `000000199...` | 已修 |
| 银行卡模糊姓名被认成 `VALID THIRU` | `bank_card/blur` name 48%，31% 解析器责任 | `EVT-006` 已修复；当前模糊姓名 55% |

三条可以直接读出来的结论：

- **银行卡只有 blur 是真退化**：其余桶 85% 以上，blur 掉到 61%
- **身份证基线本身就低**（正常样本 73.4%）：由 `id_number`、`valid_period`
  两个在**未退化样本上就 0/100** 的字段主导 —— 成像问题（号码区被水印覆盖），
  **不是模型能力问题**
- **反直觉**：blur 反而是身份证号唯一能认出来的桶（26/100）——
  模糊把标签与号码两个文本框糊成了一行

两处修复都不是「顺手改的」，而是各自被 CTE 事件记录、验证、再落地。
`EVT-004` 尤其值得看：**一条自称「严重度优先」的测试，断言却在为一个
丢弃严重度的实现背书**，于是缺陷一直是绿的 ——
[`docs/baseline_migrations/002_severity_outranks_missing.md`](docs/baseline_migrations/002_severity_outranks_missing.md)。

仍未解决的两项（都写在这里而不是藏起来）：

- **`id_number` 在真实 OCR 下上限是 2/10** —— 另外 8 张的文本里
  根本没有那串数字，这是 OCR 的局限，不是解析器能解决的。
- **反光严重度阈值仍然停用** —— 剩下 7 条「该拒却转人工」全是反光样本。
  本次记录了新证据（比值分布 0.0232–0.0881，比当初标定用的范围更宽），
  但启用它需要产品口径决策。

- ~~**真实 provider 未实测**~~ —— **已补**：2026-09-26 用 `deepseek-chat`
  跑了全量 40 条 `--live` 评测（含真实 judge）。结果与解读见
  [`ai_service/README.md`](ai_service/README.md) 第 7、8 节。简言之：
  离线那套 5.0/5.0 的解释层分数**偏高**（确定性 judge 打的是同源模板文本），
  真实模型 2.45–4.38 **更低也更可信**；工具序列 1.0 → 0.225 是真实发现
  （离线走固定序列，真模型自主选择，两者测的不是同一件事）。
- **真实模型只跑过一轮、只试过一个模型** —— 40 条单次运行不足以断言稳定性，
  也没有多模型对比。见 `ai_service/README.md` 第 9 节。
- ~~**决策层指标缺人工标注**~~ —— **已补**：`data/annotations/review_verdicts.json`
  有 40 条人工结论标注（署名 `jb（开发）`），评测改为调用平台真实规则引擎，
  `结论正确率` 已可用。CTE-2 起字段输入改为真实 OCR 观测，CTE-3 修复了
  严重度判定顺序，该指标为 **0.725**（口径演变：0.775 标注真值 → 0.675 真实 OCR → 0.725 修复后，不可直接比较）。
  样本量仍小，数字只作趋势参考。
- **judge 校准集是占位标注** —— 管线可用、能暴露 judge 的系统性偏差方向，
  但标注是项目作者自评的，**数字暂无统计意义**，替换成真实审核员标注后才有对外引用价值。
- **越界判定是规则表不是分类器** —— 可解释、可复现，代价是新句式要补关键词。
- **不做双判** —— 本服务只解释不改判。

## 常见问题

### `ModuleNotFoundError: No module named 'fastapi'`

说明当前 Python 环境没有安装项目依赖。先进入正确环境：

```powershell
conda activate bank
python -m pip install -r requirements.txt
```

### `ModuleNotFoundError: No module named 'app'`

不要直接运行：

```powershell
python app/main.py
```

应在项目根目录运行：

```powershell
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8001
```

### 端口被占用

如果启动时报：

```text
[Errno 10048] error while attempting to bind on address ('127.0.0.1', 8001)
```

说明 `8001` 已经有服务在运行。可以直接访问：

```text
http://127.0.0.1:8001/bank-card/ui
```

也可以查看占用进程：

```cmd
netstat -ano | findstr :8001
```

结束指定进程：

```cmd
taskkill /PID <PID> /F
```

或者换一个端口：

```cmd
set OCR_MODE=paddle && uvicorn app.main:app --host 127.0.0.1 --port 8002
```

### 上传图片返回 500

先看运行 Uvicorn 的终端里 traceback 最下面几行。常见原因：

- PaddleOCR 模型第一次下载失败
- `reports/paddlex-runtime-cache` 缓存权限异常
- PaddlePaddle/PaddleOCR 依赖未安装完整

可先关闭服务，删除缓存目录后重试。

### 返回 `review` 不一定是失败

`review` 表示需要人工复核。常见原因：

- 图片过亮或过暗
- 图片有反光
- 图片模糊
- 必填字段没有解析出来

## 开发说明

- 修改接口逻辑：优先看 `app/main.py`
- 修改 OCR 接入：看 `app/ocr_service.py`
- 修改图片质量判断：看 `app/quality_check.py`
- 修改字段提取规则：看 `app/field_parser.py`
- 修改身份证字段解析：看 `app/id_card_parser.py`
- 修改审核规则：看 `app/rule_check.py`

改动后建议运行：

```powershell
python -m pytest
```

## 安全说明

不要提交真实银行卡、身份证、客户资料或密钥。测试图片应使用合成数据或明确标记的测试数据。

## OCR 模式

`app/ocr_service.py` 支持两种 OCR 模式：

- `mock`：默认模式，返回稳定的合成 OCR 文本。用于接口测试、字段解析测试、规则回归测试、CI 自动化测试和性能测试基线，不加载 PaddleOCR 模型。
- `paddle`：真实 PaddleOCR 模式。仅在显式传入 `mode="paddle"` 时延迟加载 PaddleOCR，用于验证真实图片识别效果。

示例：

```python
from app.ocr_service import recognize_text

mock_text = recognize_text("data/processed/bank_card/normal/bank_card_0001.png")
paddle_text = recognize_text("data/processed/bank_card/normal/bank_card_0001.png", mode="paddle")
```

普通 `python -m pytest` 和 GitHub Actions CI 只覆盖 mock 和适配层行为，不运行真实 PaddleOCR 推理，也不会下载模型。真实识别效果验证需要在本地安装 PaddleOCR 后单独执行 `mode="paddle"` 路径。

### 服务端 OCR_MODE

FastAPI 接口不会从请求参数切换 OCR 模式，只读取服务端环境变量：

- 未设置 `OCR_MODE`：默认 `mock`
- `OCR_MODE=mock`：使用稳定的 mock OCR 文本
- `OCR_MODE=paddle`：使用真实 PaddleOCR
- 其他值：接口返回明确错误，不会静默回退

PowerShell 设置方式：

```powershell
$env:OCR_MODE="paddle"
python -m uvicorn app.main:app --host 127.0.0.1 --port 8001
```

cmd / Anaconda Prompt 设置方式：

```cmd
set OCR_MODE=paddle
uvicorn app.main:app --host 127.0.0.1 --port 8001
```

注意：`$env:OCR_MODE="paddle"` 是 PowerShell 语法，在 cmd / Anaconda Prompt 中会报“文件名、目录名或卷标语法不正确”。cmd 中应使用 `set OCR_MODE=paddle`。


本次审查修复与最新验收口径见 [审查问题修复验证](docs/审查问题修复验证.md)。离线基线通过不代表真实 AI 服务可用。
