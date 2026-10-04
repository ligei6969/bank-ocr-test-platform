from concurrent.futures import ThreadPoolExecutor
import json
import traceback
from pathlib import Path
from app import ocr_service

ocr_service._configure_paddle_runtime()
from paddleocr import PaddleOCR

engine = PaddleOCR(device='cpu', use_doc_orientation_classify=False, use_doc_unwarping=False, use_textline_orientation=False, enable_mkldnn=False)
ocr_service._get_paddle_ocr_engine = lambda: engine
paths = ['data/processed/id_card/front/normal/id_front_0002.jpg', 'data/processed/id_card/back/bright/id_back_0002.jpg']


def run(path):
    try:
        return {'path': path, 'text': ocr_service.recognize_text(path, mode='paddle')}
    except Exception as e:
        return {'path': path, 'error': str(e), 'traceback': traceback.format_exc()}


results = {'sequential': [run(p) for p in paths]}
with ThreadPoolExecutor(max_workers=2) as pool:
    results['concurrent'] = list(pool.map(run, paths*2))
Path('reports/test-artifacts/geometry-review/paddle-cpu-before.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
for group, items in results.items():
    for item in items:
        print(group, item['path'], item.get('error', 'OK'))
