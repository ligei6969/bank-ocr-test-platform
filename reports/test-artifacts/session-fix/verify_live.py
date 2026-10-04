"""Verify sessions with live local Paddle servers; never record cookies or credentials."""
import json
import sys

import httpx

from restart_local import OUT, ROOT, restart

password = sys.stdin.readline().strip()
checks = []


def record(label, response, expected=200):
    checks.append({'check': label, 'status': response.status_code})
    assert response.status_code == expected, (label, response.status_code)


with httpx.Client(trust_env=False, timeout=90, follow_redirects=False) as client:
    first, second = 'http://127.0.0.1:8001', 'http://127.0.0.1:8002'
    token = client.get(first + '/csrf-token').json()['csrf_token']
    response = client.post(first + '/login', data={'username': 'admin', 'password': password},
                           headers={'X-CSRF-Token': token})
    record('login', response, 303)
    assert response.headers['location'] == '/admin/reviews'
    token = client.get(first + '/csrf-token').json()['csrf_token']
    record('admin before switching ports', client.get(first + '/admin/reviews'))
    record('csrf on other port', client.get(second + '/csrf-token'))
    record('admin after switching ports', client.get(first + '/admin/reviews'))
    record('admin on other port', client.get(second + '/admin/reviews'))
    for index in (1, 2):
        path = ROOT / f'data/processed/bank_card/normal/bank_card_{index:04}.png'
        with path.open('rb') as image:
            response = client.post(first + '/bank-card/review',
                                   files={'file': (path.name, image, 'image/png')},
                                   headers={'X-CSRF-Token': token})
        record(f'upload {index} without relogin', response)
        request_id = response.json()['request_id']
        record(f'detail page {index}', client.get(first + '/admin/reviews/' + request_id))
        detail = client.get(first + '/review-records/' + request_id)
        record(f'detail API {index}', detail)
        mode = detail.json()['ocr_mode']
        assert mode == 'paddle', mode
        checks.append({'check': f'upload {index} runtime OCR', 'mode': mode, 'request_id': request_id})
    runtime = json.loads((OUT / 'runtime.json').read_text(encoding='utf-8'))
    runtime[0] = restart(runtime[0]['pid'], 8002)
    (OUT / 'runtime.json').write_text(json.dumps(runtime, indent=2), encoding='utf-8')
    record('admin after actual server restart', client.get(second + '/admin/reviews'))
    assert client.get(second + '/csrf-token').json()['csrf_token'] == token
    record('original port after server restart', client.get(first + '/admin/reviews'))
    with httpx.Client(trust_env=False, timeout=5, follow_redirects=False) as anonymous:
        record('unauthenticated detail API stays protected', anonymous.get(first + '/review-records/' + request_id), 401)

(OUT / 'live-verification.json').write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding='utf-8')
print(json.dumps(checks, ensure_ascii=False))
