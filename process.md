# bank-ocr-test-platform 进度记录

更新时间：2026-06-15

## 1. 当前项目概况

项目当前已具备银行卡合成数据、身份证合成数据、统一标注文件、字段解析、质量检测与规则校验的基础能力。

核心目录：

```text
bank-ocr-test-platform/
├── app/
│   ├── __init__.py
│   ├── main.py
│   ├── ocr_service.py
│   ├── quality_check.py
│   ├── field_parser.py
│   └── rule_check.py
├── scripts/
│   ├── generate_bank_card.py
│   ├── augment_images.py
│   ├── generate_id_card.py
│   ├── augment_id_card_images.py
│   ├── check_labels.py
│   ├── generate_data.py
│   ├── generate_synthetic_bank_cards.py
│   ├── generate_synthetic_id_cards.py
│   └── generate_gpl_id_cards.py
├── tests/
│   ├── test_bank_card_api.py
│   ├── test_bank_card_parser.py
│   ├── test_ocr.py
│   ├── test_quality.py
│   └── test_rule_check.py
├── data/
│   ├── annotations/
│   │   └── labels.json
│   ├── templates/
│   │   ├── bank_card/
│   │   │   └── test_bank.json
│   │   └── id_card/
│   │       └── avatar_pool/
│   ├── processed/
│   │   ├── bank_card/
│   │   └── id_card/
│   └── synthetic/
├── nailong_img/
├── third_party/
│   └── idcardgenerator/
├── requirements.txt
├── pytest.ini
└── process.md
```

说明：

- `third_party/idcardgenerator/` 保存 GPL-3.0 项目 `airob0t/idcardgenerator` 的源码和素材，保留 LICENSE。
- `nailong_img/` 当前保存 95 张本地 JPG 素材，作为身份证头像素材池的回退来源。
- `data/templates/id_card/avatar_pool/` 已创建，后续可手动放入专用头像素材；脚本会优先读取该目录。
- `Cardentify-main/` 是银行卡参考资料目录，包含真实银行卡图片；当前生成脚本没有读取真实银行卡图片，也没有使用真实银行 Logo。

## 2. 当前数据集统计

`data/annotations/labels.json` 当前统计：

```text
总标注数：2134
application_form：1
bank_card：700
id_card：33
id_card_front：700
id_card_back：700
缺失图片路径：0
```

质量类型统计：

```text
normal：310
blur：306
glare：306
occlusion：306
rotate：306
dark：300
bright：300
```

说明：

- `id_card_front` 和 `id_card_back` 是当前新身份证模块生成的主数据。
- `id_card` 的 33 条是早期 legacy 身份证合成脚本生成的历史标注，仍保留在统一 `labels.json` 中。

银行卡数据分布：

```text
data/processed/bank_card/normal/      100
data/processed/bank_card/blur/        100
data/processed/bank_card/glare/       100
data/processed/bank_card/occlusion/   100
data/processed/bank_card/rotate/      100
data/processed/bank_card/dark/        100
data/processed/bank_card/bright/      100
```

身份证数据分布：

```text
data/processed/id_card/front/normal/      100
data/processed/id_card/front/blur/        100
data/processed/id_card/front/glare/       100
data/processed/id_card/front/occlusion/   100
data/processed/id_card/front/rotate/      100
data/processed/id_card/front/dark/        100
data/processed/id_card/front/bright/      100

data/processed/id_card/back/normal/       100
data/processed/id_card/back/blur/         100
data/processed/id_card/back/glare/        100
data/processed/id_card/back/occlusion/    100
data/processed/id_card/back/rotate/       100
data/processed/id_card/back/dark/         100
data/processed/id_card/back/bright/       100
```

## 3. 已完成内容

### 银行卡合成数据

- 已实现合成银行卡 normal 图片生成：

```text
scripts/generate_bank_card.py
```

- 默认生成 100 张 `760x460` 银行卡图片。
- 输出目录：

```text
data/processed/bank_card/normal/
```

- 卡面包含测试银行名、测试卡号、持卡人姓名、有效期、芯片区域和测试水印。
- 不读取真实银行卡图片。
- 不使用真实银行 Logo。
- 不复刻真实银行卡设计。

### 银行卡异常样本增强

- 已实现：

```text
scripts/augment_images.py
```

- 从 `data/processed/bank_card/normal/` 派生异常图。
- 支持：

```text
blur / glare / occlusion / rotate / dark / bright
```

- 每张异常图片都会同步写入 `data/annotations/labels.json`。

### 身份证 GPL 模板合成数据

- 当前主脚本：

```text
scripts/generate_id_card.py
```

- 使用 GPL-3.0 项目 `airob0t/idcardgenerator` 的本地素材生成类似身份证样式：

```text
third_party/idcardgenerator/idcardgenerator/usedres/empty.png
third_party/idcardgenerator/idcardgenerator/usedres/hei.ttf
third_party/idcardgenerator/idcardgenerator/usedres/fzhei.ttf
third_party/idcardgenerator/idcardgenerator/usedres/ocrb10bt.ttf
```

- 默认生成 100 组身份证数据，每组包含：
  - 身份证正面
  - 身份证反面

- 输出目录：

```text
data/processed/id_card/front/normal/
data/processed/id_card/back/normal/
```

- 正面字段：

```text
name / gender / nation / birth / address / id_number
```

- 反面字段：

```text
issue_authority / valid_period
```

- 头像来源逻辑：
  - 优先读取 `data/templates/id_card/avatar_pool/`
  - 如果为空，读取本地 `nailong_img/`
  - 如果两者都为空，使用脚本内置占位头像

- 身份证图片带有测试水印：

```text
仅供OCR测试 非真实证件
```

- 身份证号使用 `000000...X` 测试号码，不生成真实有效身份证号。
- 标注写入统一文件 `data/annotations/labels.json`，不新建单独身份证标注文件。

### 身份证异常样本增强

- 已实现：

```text
scripts/augment_id_card_images.py
```

- 只读取 normal 图片：

```text
data/processed/id_card/front/normal/
data/processed/id_card/back/normal/
```

- 从 normal 图片派生异常样本，不重新生成身份字段。
- 支持：

```text
blur / glare / occlusion / rotate / dark / bright
```

- 输出目录：

```text
data/processed/id_card/front/{quality_type}/
data/processed/id_card/back/{quality_type}/
```

- 异常样本标注会复制 normal 样本字段，仅修改：

```text
image_path
quality_type
source = augmented_from_normal
```

- 已校验 200 张 normal 对应的 1200 张异常标注字段一致，错误数为 0。

### 统一 labels 校验脚本

- 已新增：

```text
scripts/check_labels.py
```

- 功能：
  - 读取 `data/annotations/labels.json`
  - 统计 doc_type 数量
  - 统计 quality_type 数量
  - 检查 image_path 是否存在
  - 输出缺失图片数量和前 10 条缺失路径

当前校验结果：

```text
missing_images: 0
```

### 银行卡字段解析与基础服务测试

- `app/field_parser.py` 已实现银行卡字段解析：
  - `normalize_card_number`
  - `extract_card_number`
  - `extract_valid_date`
  - `extract_cardholder_name`
  - `parse_bank_card_fields`

- 已有测试覆盖银行卡字段解析、质量检查、规则检查和银行卡 API 基础流程。

## 4. 当前可用运行命令

生成银行卡 normal 图片：

```powershell
conda run -n bank python scripts\generate_bank_card.py
```

生成银行卡异常样本：

```powershell
conda run -n bank python scripts\augment_images.py
```

生成身份证 normal 图片：

```powershell
conda run -n bank python scripts\generate_id_card.py --count 100
```

生成身份证异常样本：

```powershell
conda run -n bank python scripts\augment_id_card_images.py
```

检查统一标注文件：

```powershell
conda run -n bank python scripts\check_labels.py
```

运行全部测试：

```powershell
conda run -n bank python -m pytest
```

最近一次测试结果：

```text
18 passed, 1 warning
```

警告来自 FastAPI/Starlette TestClient 依赖弃用提示，不影响当前测试通过。

## 5. 当前限制和待完成事项

- `app/ocr_service.py` 仍是占位 OCR 服务，后续需要接入真实 OCR 引擎或模拟 OCR 流程。
- `data/annotations/labels.json` 中仍保留 33 条 legacy `doc_type = id_card` 历史标注，后续如需统一口径，可以迁移或清理为 `id_card_front/id_card_back`。
- 身份证合成使用 GPL-3.0 第三方素材，后续发布、分发或商用前需要确认 GPL 许可证边界。
- `data/templates/id_card/avatar_pool/` 当前为空，身份证头像实际回退使用 `nailong_img/` 本地图片。
- 当前身份证样式用于 OCR 测试，仍需避免被误认为真实证件；已通过无效证号和测试水印降低风险。
- `Cardentify-main/` 仍作为银行卡参考资料目录保留，当前脚本未使用其中真实银行卡图片；后续使用需注意版权和数据用途边界。
