"""审核域知识库语料。

这是 AI 复核助手的「事实来源」。P0 阶段覆盖四类：

1. ``reason_code``  —— 全部审核原因码释义（含真实阈值与实现位置）
2. ``capture_guide`` —— 拍摄规范（对应用户端 ``user_home.html`` 的三条提示）
3. ``review_rule``  —— 审核判定规则的人话版本
4. ``case``         —— 典型复核案例（**合成示例**，非真实客户数据）

维护约定
--------
* 语料里的阈值必须与平台代码一致。``tests/test_ai_corpus_consistency.py``
  会读 ``app.quality_check`` / ``app.rule_check`` 的真实常量做交叉校验，
  阈值改了但语料没同步会直接测试失败。
* 原因码条目必须覆盖平台可能产出的**全部**原因码（同上测试校验）。
* 涉及证件号、姓名的示例一律使用虚构值，且写库前已脱敏。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CATEGORY_REASON_CODE = "reason_code"
CATEGORY_CAPTURE_GUIDE = "capture_guide"
CATEGORY_REVIEW_RULE = "review_rule"
CATEGORY_CASE = "case"

DOC_TYPE_BANK_CARD = "bank_card"
DOC_TYPE_ID_CARD = "id_card"
DOC_TYPE_ANY = "any"


@dataclass(frozen=True)
class KnowledgeDoc:
    """一条知识文档。

    ``reason_codes`` 让检索层能做「原因码精确命中」加权 —— 这是本域最关键的一路召回，
    因为审核记录的上下文里本来就带着原因码。
    """

    doc_id: str
    title: str
    category: str
    content: str
    reason_codes: tuple[str, ...] = ()
    doc_types: tuple[str, ...] = (DOC_TYPE_ANY,)
    tags: tuple[str, ...] = ()

    def as_metadata(self) -> dict[str, Any]:
        return {
            "doc_id": self.doc_id,
            "title": self.title,
            "category": self.category,
            "reason_codes": list(self.reason_codes),
            "doc_types": list(self.doc_types),
            "tags": list(self.tags),
        }


# ── 1. 原因码释义 ─────────────────────────────────────────────────────────────
#
# 内容结构统一为：触发条件 / 实现位置 / 业务含义 / 处置建议 / 用户话术，
# 这样既方便检索，又直接就是解释文本的素材。

REASON_CODE_DOCS: tuple[KnowledgeDoc, ...] = (
    KnowledgeDoc(
        doc_id="rc.image_blur",
        title="image_blur 图片模糊",
        category=CATEGORY_REASON_CODE,
        reason_codes=("image_blur",),
        tags=("质量检测", "模糊", "清晰度"),
        content=(
            "原因码 image_blur，含义是图片模糊。"
            "触发条件：图像清晰度过低，文字边缘无法稳定分割。"
            "实现位置：app/quality_check.py 的 detect_blur()，用 OpenCV 对灰度图求拉普拉斯算子方差，"
            "方差小于 80.0 判定为模糊。方差越小说明高频细节越少、图像越糊。"
            "业务含义：模糊会直接导致 OCR 识别错误，常见后果是卡号少位、有效期误识，"
            "属于「影像质量问题」而不是「用户资质问题」。"
            "处置建议：让用户重新拍摄，镜头对准证件保持稳定，避免逆光和抖动，"
            "不要使用微信压缩后的图片。若同一用户多次上传仍模糊，"
            "应怀疑设备摄像头或翻拍屏幕，建议改用原件扫描。"
            "用户话术：图片有些模糊，请重新拍摄，请把证件放平、镜头对准、手别抖。"
        ),
    ),
    KnowledgeDoc(
        doc_id="rc.image_dark",
        title="image_dark 图片过暗",
        category=CATEGORY_REASON_CODE,
        reason_codes=("image_dark",),
        tags=("质量检测", "亮度", "过暗"),
        content=(
            "原因码 image_dark，含义是图片过暗。"
            "触发条件：图像整体亮度低于正常范围。"
            "实现位置：app/quality_check.py 的 detect_brightness()，把图像转灰度后取全图平均亮度，"
            "平均值低于 65 判定为 dark。"
            "业务含义：过暗会让 OCR 丢失对比度低的文字，尤其身份证背面的签发机关与有效期小字。"
            "处置建议：让用户在光线充足处重拍，避免背光、避免用手遮挡光源，"
            "不要开启会压暗画面的强闪光。"
            "用户话术：照片太暗了，请在光线亮一点的地方重新拍摄。"
        ),
    ),
    KnowledgeDoc(
        doc_id="rc.image_bright",
        title="image_bright 图片过亮",
        category=CATEGORY_REASON_CODE,
        reason_codes=("image_bright",),
        tags=("质量检测", "亮度", "过亮", "曝光"),
        content=(
            "原因码 image_bright，含义是图片过亮。"
            "触发条件：图像整体亮度高于正常范围，通常由近距离闪光或过曝造成。"
            "实现位置：app/quality_check.py 的 detect_brightness()，灰度平均亮度高于 210 判定为 bright。"
            "业务含义：过曝会把文字和底纹一起冲成白色，OCR 无法提取字段。"
            "处置建议：让用户关闭闪光灯或拉开拍摄距离，避免正对灯光与屏幕高光。"
            "用户话术：照片曝光过度了，请关掉闪光灯重新拍摄。"
        ),
    ),
    KnowledgeDoc(
        doc_id="rc.glare_detected",
        title="glare_detected 检测到反光",
        category=CATEGORY_REASON_CODE,
        reason_codes=("glare_detected",),
        tags=("质量检测", "反光", "高光", "误报"),
        content=(
            "原因码 glare_detected，含义是检测到反光。"
            "触发条件：画面存在大面积高亮低饱和区域。"
            "实现位置：app/quality_check.py 的 detect_glare()，先把图像转到 HSV 空间，"
            "取亮度通道大于 245 且饱和度通道小于 45 的像素作为高光掩膜，"
            "再做连通域分析，最大连通域面积占全图比例超过 0.5% 判定为反光。"
            "业务含义：反光会遮盖局部文字，也可能被规则误判 —— "
            "银行卡本身有镭射防伪区和珠光涂层，在标准光照下也可能触发该原因码，"
            "所以这一项在人工复核队列里属于**优先怀疑误报**的一类。"
            "处置建议：先看反光位置是否压在卡号、有效期或姓名上；"
            "若压在空白/防伪区且字段已完整解析，可直接放行；"
            "若压在关键字段上，让用户变换角度避开光源重拍。"
            "用户话术：照片上有反光遮挡，请换个角度、避开灯光重新拍摄。"
        ),
    ),
    KnowledgeDoc(
        doc_id="rc.missing_card_number",
        title="missing_card_number 未解析到银行卡号",
        category=CATEGORY_REASON_CODE,
        reason_codes=("missing_card_number",),
        doc_types=(DOC_TYPE_BANK_CARD,),
        tags=("字段缺失", "卡号"),
        content=(
            "原因码 missing_card_number，含义是未解析到银行卡号。"
            "触发条件：OCR 文本经 app/field_parser.py 解析后，字段 card_number 为空。"
            "实现位置：app/rule_check.py，REQUIRED_BANK_CARD_FIELDS 包含 card_number、"
            "valid_date、name，任一为空即产出 missing_<字段名> 原因码。"
            "业务含义：卡号是审核的必填主键，缺失时无法进入后续校验，只能转人工。"
            "处置建议：先排查是不是影像质量问题（常与 image_blur、glare_detected 同时出现），"
            "再看是不是拍摄时卡号被手指或卡套遮挡，或裁切掉了卡号区域。"
            "用户话术：没有识别到银行卡号，请确保卡号完整出现在画面里、且没有被遮挡。"
        ),
    ),
    KnowledgeDoc(
        doc_id="rc.missing_valid_date",
        title="missing_valid_date 未解析到银行卡有效期",
        category=CATEGORY_REASON_CODE,
        reason_codes=("missing_valid_date",),
        doc_types=(DOC_TYPE_BANK_CARD,),
        tags=("字段缺失", "有效期"),
        content=(
            "原因码 missing_valid_date，含义是未解析到银行卡有效期。"
            "触发条件：字段 valid_date 为空。"
            "实现位置：app/rule_check.py 的必填字段校验。"
            "业务含义：有效期用于判断卡是否过期，缺失时无法自动放行。"
            "处置建议：有效期一般印在卡面月份/年份小字处，字号最小、最容易被模糊和反光吃掉。"
            "优先检查是否同时存在 image_blur 或 glare_detected；"
            "若只是裁切掉了卡面右侧，提示用户把整张卡拍全。"
            "用户话术：没有识别到有效期，请把整张卡拍进画面，注意卡号下方的小字也要清晰。"
        ),
    ),
    KnowledgeDoc(
        doc_id="rc.missing_name",
        title="missing_name 未解析到持卡人姓名",
        category=CATEGORY_REASON_CODE,
        reason_codes=("missing_name",),
        doc_types=(DOC_TYPE_BANK_CARD, DOC_TYPE_ID_CARD),
        tags=("字段缺失", "姓名"),
        content=(
            "原因码 missing_name，含义是未解析到姓名。"
            "触发条件：银行卡场景下字段 name 为空；身份证人像面场景下字段 name 为空。"
            "实现位置：app/rule_check.py（银行卡）与 app/main.py 的 "
            "review_id_card_with_reasons()（身份证人像面必填 name、gender、nation、"
            "birth、address、id_number）。"
            "业务含义：姓名是身份核验的核心字段，缺失无法完成比对。"
            "处置建议：银行卡场景下确认持卡人姓名是否被卡片工艺（如凸字磨损）影响；"
            "身份证场景下确认上传的是人像面而不是国徽面。"
            "用户话术：没有识别到姓名，请确认上传的是证件正面并且文字清晰完整。"
        ),
    ),
    KnowledgeDoc(
        doc_id="rc.invalid_card_number",
        title="invalid_card_number 银行卡号未通过规则校验",
        category=CATEGORY_REASON_CODE,
        reason_codes=("invalid_card_number",),
        doc_types=(DOC_TYPE_BANK_CARD,),
        tags=("规则校验", "卡号", "reject"),
        content=(
            "原因码 invalid_card_number，含义是银行卡号格式未通过规则校验。"
            "触发条件：解析出的 card_number 不满足 16 到 19 位纯数字。"
            "实现位置：app/rule_check.py 的 is_valid_card_number()，"
            "正则 fullmatch(r\"\\d{16,19}\")。"
            "业务含义：**这是唯一会直接产出 reject 的原因码**，"
            "因为满足「已解析出字段但长度结构明显非法」时，"
            "更像伪造或严重误识，而不是单纯拍得不好。"
            "处置建议：人工必须看到卡号本身再决定。"
            "若同时存在 image_blur，更可能是 OCR 把部分数字漏识或误识（例如 8 认成 3），"
            "此时应优先怀疑误识而不是假卡；"
            "若影像清晰、字段完整但位数依然非法，则按拒绝处理并留痕。"
            "用户话术：银行卡号未通过校验，请确认拍摄的是银行卡正面且卡号清晰完整。"
        ),
    ),
    KnowledgeDoc(
        doc_id="rc.invalid_valid_date",
        title="invalid_valid_date 有效期格式不合法",
        category=CATEGORY_REASON_CODE,
        reason_codes=("invalid_valid_date",),
        doc_types=(DOC_TYPE_BANK_CARD,),
        tags=("规则校验", "有效期"),
        content=(
            "原因码 invalid_valid_date，含义是有效期格式不合法。"
            "触发条件：valid_date 不满足 MM/YY 且月份在 01 到 12 之间。"
            "实现位置：app/rule_check.py 的 is_valid_expiry()，"
            "正则 fullmatch(r\"(0[1-9]|1[0-2])/\\d{2}\")。"
            "业务含义：字段解析出来了但格式不对，通常是 OCR 把斜杠识别成其他符号、"
            "或把相邻数字串进有效期。"
            "处置建议：核对 OCR 原文里有效期的实际形态；"
            "若只是分隔符误识（如 08-29 被识别为 08/29 之外的形式），"
            "属于 OCR 层问题，应记为误识而非用户问题。"
            "用户话术：有效期识别不准确，请重新拍摄卡面有效期区域。"
        ),
    ),
    KnowledgeDoc(
        doc_id="rc.unknown_id_card_side",
        title="unknown_id_card_side 无法判断身份证正反面",
        category=CATEGORY_REASON_CODE,
        reason_codes=("unknown_id_card_side",),
        doc_types=(DOC_TYPE_ID_CARD,),
        tags=("身份证", "正反面"),
        content=(
            "原因码 unknown_id_card_side，含义是无法判断身份证上传的是哪一面。"
            "触发条件：解析结果里 side 为 unknown，既不像人像面也不像国徽面。"
            "实现位置：app/id_card_parser.py 的正反面判断，"
            "由 app/main.py 的 review_id_card_with_reasons() 产出该原因码。"
            "业务含义：正反面判断失败说明影像根本没有包含可识别的身份证版面，"
            "常见原因是上传了身份证以外的证件、照片过于模糊、或只拍了证件一角。"
            "处置建议：让用户确认上传的是二代居民身份证，并完整拍摄单面、"
            "四边留出少量边距。"
            "用户话术：无法识别证件类型，请上传二代身份证单面照片，人像面与国徽面分开上传。"
        ),
    ),
    KnowledgeDoc(
        doc_id="rc.missing_id_front_fields",
        title="身份证人像面字段缺失 missing_gender / missing_nation / missing_birth / missing_address / missing_id_number",
        category=CATEGORY_REASON_CODE,
        reason_codes=(
            "missing_gender",
            "missing_nation",
            "missing_birth",
            "missing_address",
            "missing_id_number",
        ),
        doc_types=(DOC_TYPE_ID_CARD,),
        tags=("身份证", "字段缺失", "人像面"),
        content=(
            "原因码 missing_gender、missing_nation、missing_birth、missing_address、"
            "missing_id_number，含义是身份证人像面必填字段缺失。"
            "触发条件：app/main.py 的 review_id_card_with_reasons() 在 side 为 front 时，"
            "要求 name、gender、nation、birth、address、id_number 全部非空，"
            "缺少哪个就产出对应的 missing_<字段名>。"
            "业务含义：人像面是身份信息主体，任一字段缺失都无法完成核验。"
            "地址字段地址行最长、最容易断行，是这一组里最高频的缺失项。"
            "处置建议：优先排查 image_blur；地址缺失时确认是否把地址行裁切掉了。"
            "用户话术：身份证信息识别不完整，请确保证件四边都在画面内、文字清晰。"
        ),
    ),
    KnowledgeDoc(
        doc_id="rc.missing_id_back_fields",
        title="身份证国徽面字段缺失 missing_issue_authority / missing_valid_period",
        category=CATEGORY_REASON_CODE,
        reason_codes=("missing_issue_authority", "missing_valid_period"),
        doc_types=(DOC_TYPE_ID_CARD,),
        tags=("身份证", "字段缺失", "国徽面"),
        content=(
            "原因码 missing_issue_authority、missing_valid_period，"
            "含义是身份证国徽面的签发机关或有效期限缺失。"
            "触发条件：side 为 back 时，要求 issue_authority 与 valid_period 均非空。"
            "实现位置：app/main.py 的 review_id_card_with_reasons()。"
            "业务含义：这两项是证件有效性的判断依据。"
            "国徽面文字较小且多为深色印刷，容易受 image_dark 与 image_blur 影响。"
            "处置建议：确认是否上传的是国徽面；"
            "若有效期限是长期，注意原文可能是「长期」而非日期形态，需人工确认。"
            "用户话术：国徽面文字识别不完整，请在光线充足处重新拍摄国徽面。"
        ),
    ),
    KnowledgeDoc(
        doc_id="rc.invalid_file_type",
        title="invalid_file_type 上传文件类型不受支持",
        category=CATEGORY_REASON_CODE,
        reason_codes=("invalid_file_type",),
        tags=("文件校验",),
        content=(
            "原因码 invalid_file_type，含义是上传文件类型不受支持。"
            "触发条件：文件后缀不是 .png、.jpg、.jpeg。"
            "实现位置：app/main.py 的 validate_bank_card_upload()，"
            "允许后缀集合 ALLOWED_BANK_CARD_IMAGE_SUFFIXES。"
            "业务含义：这是**入口校验失败，不是影像质量问题**，"
            "审核记录里 review_result 为 error，不应进入人工复核队列。"
            "处置建议：提示用户改用 PNG 或 JPEG 格式，"
            "常见坑是手机截图直接存成 HEIC、WEBP 或 PDF。"
            "用户话术：仅支持 PNG 与 JPG 格式，请转换格式后重新上传。"
        ),
    ),
    KnowledgeDoc(
        doc_id="rc.unreadable_image",
        title="unreadable_image 文件为空、损坏或不是可读取图片",
        category=CATEGORY_REASON_CODE,
        reason_codes=("unreadable_image",),
        tags=("文件校验",),
        content=(
            "原因码 unreadable_image，含义是文件为空、损坏或不是可读取图片。"
            "触发条件：文件字节数为 0，或 Pillow 打开并 verify() 时抛错。"
            "实现位置：app/main.py 的 validate_readable_image()，"
            "以及 check_image_quality() 抛出的 ValueError 分支。"
            "业务含义：同样是入口/文件层问题，不是影像质量问题。"
            "常见原因是上传中断导致文件截断、把非图片文件改名成 .png。"
            "处置建议：让用户重新上传；服务端排查上传体积上限与超时。"
            "用户话术：文件无法读取，请重新选择图片上传。"
        ),
    ),
    KnowledgeDoc(
        doc_id="rc.invalid_ocr_mode",
        title="invalid_ocr_mode 服务端 OCR 模式配置非法",
        category=CATEGORY_REASON_CODE,
        reason_codes=("invalid_ocr_mode",),
        tags=("配置", "运维"),
        content=(
            "原因码 invalid_ocr_mode，含义是服务端 OCR_MODE 配置非法。"
            "触发条件：环境变量 OCR_MODE 不在 mock 或 paddle 之内。"
            "实现位置：app/main.py 的 get_ocr_mode()，"
            "允许取值 ALLOWED_OCR_MODES = {'mock', 'paddle'}。"
            "业务含义：**这是服务端配置问题，与用户和影像都无关**，"
            "出现时应立即报障而不是给用户任何重拍提示。"
            "处置建议：检查部署环境变量 OCR_MODE，默认应为 mock；"
            "生产切 paddle 前先在预发验证模型加载耗时。"
            "用户话术：无需提示用户，服务端修正配置后重试。"
        ),
    ),
    KnowledgeDoc(
        doc_id="rc.invalid_request",
        title="invalid_request 请求格式不合法",
        category=CATEGORY_REASON_CODE,
        reason_codes=("invalid_request",),
        tags=("请求校验",),
        content=(
            "原因码 invalid_request，含义是请求本身不合法。"
            "触发条件：请求校验失败（HTTP 422）或路径不属于受支持的审核接口。"
            "实现位置：app/main.py 的 RequestValidationError 处理器与审核请求中间件。"
            "业务含义：调用方问题，不是影像质量问题。"
            "处置建议：核对必填字段与 Content-Type，"
            "常见坑是漏传 file 字段、没带 CSRF 头、或 multipart 边界被代理改写。"
            "用户话术：无需提示用户，属调用方修复范围。"
        ),
    ),
    KnowledgeDoc(
        doc_id="rc.internal_error",
        title="internal_error 服务端内部错误",
        category=CATEGORY_REASON_CODE,
        reason_codes=("internal_error",),
        tags=("服务端", "异常"),
        content=(
            "原因码 internal_error，含义是审核过程中抛出未捕获异常。"
            "触发条件：未被 HTTPException 覆盖的异常，或 HTTP 5xx。"
            "实现位置：app/main.py 的 review_unhandled_exception_handler。"
            "业务含义：服务端缺陷或依赖故障，与用户影像无关。"
            "处置建议：用记录里的 request_id 去日志里检索，"
            "异常信息已按 mask_sensitive_data 脱敏，可直接定位堆栈。"
            "用户话术：提示用户稍后重试，同时按 request_id 报障。"
        ),
    ),
)


# ── 2. 拍摄规范 ───────────────────────────────────────────────────────────────

CAPTURE_GUIDE_DOCS: tuple[KnowledgeDoc, ...] = (
    KnowledgeDoc(
        doc_id="cg.edges",
        title="拍摄规范：边缘完整",
        category=CATEGORY_CAPTURE_GUIDE,
        tags=("拍摄规范", "裁切", "边缘"),
        content=(
            "拍摄规范第一条，边缘完整。"
            "要求证件四条边都位于画面之内，不裁切、不遮挡。"
            "为什么重要：卡号和有效期分别位于卡面的左右与下方边缘，"
            "裁切会直接导致 missing_card_number、missing_valid_date；"
            "身份证地址行也常因下边缘裁切而缺失。"
            "常见错误：手指压住卡号、证件紧贴画面边框、翻拍时只对准文字区域。"
            "正确做法：把证件放在深色平面上，四周各留大约百分之五的边距。"
        ),
    ),
    KnowledgeDoc(
        doc_id="cg.text",
        title="拍摄规范：文字清晰",
        category=CATEGORY_CAPTURE_GUIDE,
        tags=("拍摄规范", "模糊", "对焦", "抖动"),
        content=(
            "拍摄规范第二条，文字清晰。"
            "要求镜头稳定、正确对焦，避免抖动和过度压缩。"
            "为什么重要：清晰度不达标会同时拉低多个字段的识别率，"
            "在流水线里表现为 image_blur，随后派生出 missing_valid_date、"
            "invalid_card_number 等一串下游原因码。"
            "常见错误：单手拍摄导致抖糊、隔着玻璃或塑料卡套拍、"
            "用聊天软件传输被二次压缩。"
            "正确做法：双手持稳或以桌面支撑，先在取景框里点按证件让镜头对焦，"
            "再用原图上传。"
        ),
    ),
    KnowledgeDoc(
        doc_id="cg.light",
        title="拍摄规范：光线均匀",
        category=CATEGORY_CAPTURE_GUIDE,
        tags=("拍摄规范", "光线", "反光", "阴影", "过曝"),
        content=(
            "拍摄规范第三条，光线均匀。"
            "要求避免强光反射、明显阴影，以及整体过亮或过暗。"
            "为什么重要：不均匀的光照会分别触发三条质量原因码 —— "
            "过暗对应 image_dark，过亮对应 image_bright，"
            "高亮低饱和的大面积光斑对应 glare_detected。"
            "常见错误：正对顶灯拍摄产生反光、背光拍摄导致卡面发暗、"
            "开启闪光灯近距离拍摄造成过曝。"
            "正确做法：让光线从侧上方来，身体或纸张挡住直射光源，"
            "关闭闪光灯，避免在玻璃桌面上拍摄。"
        ),
    ),
)


# ── 3. 审核规则（人话版）──────────────────────────────────────────────────────

REVIEW_RULE_DOCS: tuple[KnowledgeDoc, ...] = (
    KnowledgeDoc(
        doc_id="rr.bank_card_order",
        title="审核规则：银行卡判定顺序",
        category=CATEGORY_REVIEW_RULE,
        doc_types=(DOC_TYPE_BANK_CARD,),
        tags=("审核规则", "银行卡", "reject", "pass"),
        content=(
            "银行卡审核判定按固定顺序执行，前一步不通过就不会走下一步。"
            "第一步，检查必填字段 card_number、valid_date、name 是否都已解析出来；"
            "只要缺任意一个，直接判 review 并把 missing_<字段名> 和全部质量原因码一起返回。"
            "第二步，字段齐全后校验卡号格式，不满足 16 到 19 位纯数字则判 reject，"
            "这是全流程唯一会产出 reject 的分支。"
            "第三步，校验有效期格式是否为 MM/YY 且月份在 01 到 12，不满足判 review。"
            "第四步，前面都通过但存在任一质量原因码、或质量结果为 review 时判 review。"
            "第五步，全部通过才判 pass 并返回空的原因码数组。"
            "理解这个顺序很重要：它决定了 reject 优先级高于一切质量问题 —— "
            "一张既模糊又卡号非法的图，结论是 reject 而不是 review。"
        ),
    ),
    KnowledgeDoc(
        doc_id="rr.id_card_order",
        title="审核规则：身份证判定顺序",
        category=CATEGORY_REVIEW_RULE,
        doc_types=(DOC_TYPE_ID_CARD,),
        tags=("审核规则", "身份证", "正反面"),
        content=(
            "身份证审核先判断正反面，再按面别的必填字段校验。"
            "先看 side：为 unknown 时产出 unknown_id_card_side。"
            "人像面即 front，必填 name、gender、nation、birth、address、id_number 六个字段，"
            "缺哪个产出对应的 missing_<字段名>。"
            "国徽面即 back，必填 issue_authority 与 valid_period 两个字段。"
            "最后合并质量原因码，去重后返回。"
            "只要原因码非空或质量结果不是 pass，结论就是 review。"
            "注意：身份证链路**没有 reject 分支**，所有问题都走人工复核。"
        ),
    ),
    KnowledgeDoc(
        doc_id="rr.quality_vs_field",
        title="审核规则：质量原因码与字段原因码的区别",
        category=CATEGORY_REVIEW_RULE,
        tags=("审核规则", "归因", "根因分析"),
        content=(
            "审核原因码分两大类，处置方向完全不同，这是复核时最先要分清的。"
            "第一类是质量原因码，来自 app/quality_check.py，"
            "包括 image_blur、image_dark、image_bright、glare_detected —— "
            "根因在用户拍摄，处置是让用户重拍。"
            "第二类是字段原因码，来自解析与规则校验，"
            "包括 missing_<字段名>、invalid_card_number、invalid_valid_date —— "
            "根因可能是拍摄质量，也可能是 OCR 误识，也可能是确实不合规。"
            "还有第三类是基础设施原因码，包括 invalid_file_type、unreadable_image、"
            "invalid_ocr_mode、invalid_request、internal_error —— "
            "根因在服务端或调用方，**不应给用户任何重拍提示**。"
            "实务判据：如果质量原因码非空，字段原因码大概率是它的下游后果，"
            "应优先修影像质量；如果质量全绿但字段缺失或非法，才去怀疑 OCR 与合规性。"
        ),
    ),
)


# ── 4. 典型复核案例（合成示例）────────────────────────────────────────────────

CASE_DOCS: tuple[KnowledgeDoc, ...] = (
    KnowledgeDoc(
        doc_id="case.blur_cascade",
        title="案例：模糊导致有效期缺失的连锁反应",
        category=CATEGORY_CASE,
        doc_types=(DOC_TYPE_BANK_CARD,),
        tags=("案例", "模糊", "连锁"),
        content=(
            "典型复核案例（合成示例）。"
            "记录形态：review_result 为 review，"
            "quality_reasons 为 image_blur，"
            "review_reasons 为 missing_valid_date、image_blur；"
            "但卡号与姓名都正常解析出来。"
            "判读：卡号字号大、余量大，即使轻微模糊也能识别；"
            "有效期字号最小，最先被模糊吃掉。所以出现「只有有效期缺失」"
            "这种形态时，根因几乎可以确定是清晰度不足，而不是用户没拍有效期。"
            "处置：直接要求重拍，**不要**去追问用户有效期是多少 —— "
            "让用户念有效期既增加摩擦，也拿不到可用于核验的可信证据。"
        ),
    ),
    KnowledgeDoc(
        doc_id="case.glare_false_positive",
        title="案例：卡面镭射区反光的误报",
        category=CATEGORY_CASE,
        doc_types=(DOC_TYPE_BANK_CARD,),
        tags=("案例", "反光", "误报", "放行"),
        content=(
            "典型复核案例（合成示例）。"
            "记录形态：review_result 为 review，"
            "quality_reasons 与 review_reasons 都只有 glare_detected；"
            "card_number、valid_date、name 三个字段全部完整且通过格式校验。"
            "判读：字段全绿说明反光没有遮挡任何关键区域，"
            "大概率是银行卡镭射防伪区或珠光涂层在正常光照下产生的天然高光，"
            "属于规则的已知误报形态。"
            "处置：核对一下高光位置确实不覆盖卡号、有效期、姓名，即可放行，"
            "并把该记录标记为规则误报样本 —— "
            "这类样本积累起来，就是后续调整 glare 检测阈值或引入第二判定的依据。"
        ),
    ),
    KnowledgeDoc(
        doc_id="case.card_number_misread",
        title="案例：卡号非法的两种根因分流",
        category=CATEGORY_CASE,
        doc_types=(DOC_TYPE_BANK_CARD,),
        tags=("案例", "卡号", "reject", "OCR误识"),
        content=(
            "典型复核案例（合成示例）。"
            "记录形态：review_result 为 reject，review_reasons 含 invalid_card_number。"
            "第一种形态，同时存在 image_blur 或 glare_detected："
            "多半是 OCR 把个别数字认错或漏识（例如 0 与 8、1 与 7 混淆，"
            "或其中一位被光斑覆盖），应判为**误识**，按 review 处理并让用户重拍，"
            "不应直接拒绝。"
            "第二种形态，影像清晰、质量全绿，卡号位数与结构依然非法："
            "这属于实质性问题，按 reject 处理并留痕。"
            "区分这两者的唯一可靠依据是**质量原因码是否为空**，"
            "所以复核时不要只看 review_result，一定要并排看 quality_reasons。"
        ),
    ),
)


DEFAULT_DOCS: tuple[KnowledgeDoc, ...] = (
    REASON_CODE_DOCS + CAPTURE_GUIDE_DOCS + REVIEW_RULE_DOCS + CASE_DOCS
)


def all_reason_codes() -> tuple[str, ...]:
    """语料中已覆盖的全部原因码。"""
    codes: list[str] = []
    for doc in DEFAULT_DOCS:
        for code in doc.reason_codes:
            if code not in codes:
                codes.append(code)
    return tuple(sorted(codes))


def reason_code_lookup() -> dict[str, KnowledgeDoc]:
    """原因码 → 释义文档。一个原因码可能被多条文档覆盖时取第一条。"""
    lookup: dict[str, KnowledgeDoc] = {}
    for doc in DEFAULT_DOCS:
        for code in doc.reason_codes:
            lookup.setdefault(code, doc)
    return lookup


def load_documents() -> list[KnowledgeDoc]:
    """返回默认语料。预留：P1 可在此处合并数据库导出的历史案例。"""
    return list(DEFAULT_DOCS)
