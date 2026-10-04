# 遮挡、旋转与身份证异常复盘

本次使用 bankocr-test-evolution 的失败复现与回归验证流程，诊断产物按项目要求保存在 reports/test-artifacts；未修改人工真值、标注或 holdout，未自动晋级 CTE validated 资产。

## 发生了什么
截图人像面出现内部错误，旧实现不为非 HTTPException 保存记录，因此无法还原那次具体异常。国徽面记录为 paddle / review / missing_valid_period，OCR 文本无期限。
旧质量检测只看模糊、亮度、反光；当前合成遮挡或旋转图片在 OCR 字段完整时可自动通过。

## 为什么旧测试没发现
原 occlusion / rotate 测试只要求图片可处理、返回字段类型正确，未要求检测到影像异常。共享预测器测试只检查顺序调用，没有检验并发保护。

## 归因
质量规则缺少遮挡/旋转信号；前端使用笼统失败提示；非 HTTPException 缺少审计留痕。Paddle 共用实例未串行化存在并发风险，但无法将历史截图错误确定归因为并发。真实 CPU 并发实验没有复现历史 500，不能宣称确定根因。

## 回归资产
新增 tests/test_image_geometry_review.py 和 tests/test_ocr_concurrency.py。修复前几何测试 9 项失败；共享预测器隔离测试修复前失败。修复后当前 100 张正常银行卡不触发几何告警，100 张合成深色块遮挡均不自动通过。任意文件名及现场新旋转图片同样验证。

## 历史关联
与字段缺失归因事件相同，OCR 文本缺乏字段证据不能简单归为解析器错误；本次没有改动身份证字段真值。

## 验证与限制
全量 CLI pytest 1489 passed；真实 8001 Paddle 上传正常卡 pass、遮挡/旋转 review，两张身份证均正常响应并因缺失字段 review。新增误导性 500 文案修正单独做页面测试与 JavaScript 语法检查。
旋转容差 1 度，90 度侧转可检出，180 度倒置无可靠判断。旋转目录的 0001 接近零度，可正常通过。遮挡启发式覆盖大块深色矩形，手指、浅色、复杂纹理可能漏检，合法装饰也可能误报；不宣称通用遮挡识别完成。

## 可复查产物
prediction.json（执行前原预测，不覆盖）、before.json、tests-before.txt、concurrency-before.txt、full-regression-final.txt、live-final.json、backend-live.log。
