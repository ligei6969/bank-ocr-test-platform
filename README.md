# Bank OCR Test Platform

一个面向银行远程开户、信用卡进件场景的影像资料审核测试平台。项目基于 FastAPI，覆盖银行卡和身份证图片从上传、质量检测、OCR、字段解析、规则审核到审核记录追踪的完整测试流程。

当前版本是用于接口测试、规则验证、OCR 适配和性能基线验证的测试开发 Demo，不是生产级银行系统。

当前项目已经接入 Mock/PaddleOCR 双模式。默认使用 mock OCR，适合接口、规则、CI 和性能基线测试；设置 `OCR_MODE=paddle` 后使用真实 PaddleOCR 推理。项目目前支持：

- 银行卡审核：解析银行卡号、有效期、持卡人姓名
- 身份证审核：自动判断正面/反面，并解析对应字段
- 审核可追踪：为每次请求生成 `request_id`，并将审核记录写入 SQLite
- 审核可解释：返回图片质量原因码和最终审核原因码
- 日志安全：银行卡号和身份证号写入日志前自动脱敏
- 测试工程：pytest、Allure、Locust 和 GitHub Actions
- 银行卡前端审核页：上传图片后调用后端接口并展示审核结果、质量结果、字段、OCR 文本和原始 JSON

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
| `missing_card_number` | 未解析到银行卡号 |
| `missing_valid_date` | 未解析到银行卡有效期 |
| `invalid_card_number` | 银行卡号未通过规则校验 |
| `unknown_id_card_side` | 无法判断身份证正反面 |
| `invalid_file_type` | 上传文件类型不受支持 |
| `unreadable_image` | 文件为空、损坏或不是可读取图片 |
| `invalid_ocr_mode` | 服务端 `OCR_MODE` 配置非法 |

## 项目结构

```text
app/
  main.py           FastAPI 入口和接口定义
  ocr_service.py    PaddleOCR 集成层
  quality_check.py  图片模糊、亮度、反光检测
  field_parser.py   银行卡字段解析
  id_card_parser.py 身份证正反面检测和字段解析
  rule_check.py     审核规则判断
  review_records.py SQLite 审核记录持久化和查询
  logging_utils.py  银行卡号、身份证号日志脱敏
  static/           银行卡审核前端页面

tests/              pytest 测试
data/               测试数据和生成数据
reports/            测试输出、临时上传文件、OCR 模型缓存
scripts/            数据生成和处理脚本
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

当前冻结版全量测试结果为 `136 passed`。普通 pytest 会清理外部 `OCR_MODE` 环境变量并使用 mock OCR，不会下载或加载真实 PaddleOCR 模型；GitHub Actions 同样固定使用 mock 路径。

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
- 真实 OCR 效果需要显式使用 PaddleOCR，并通过 `scripts/evaluate_bank_card_ocr.py` 等评估脚本单独验证。
- Locust 默认 mock 模式结果仅代表接口流程性能基线，不代表真实 PaddleOCR 推理性能。
- 项目没有实现生产级权限控制、数据加密、分布式存储、审批工作流和合规审计体系。

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
