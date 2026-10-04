"""A shared Paddle predictor must not process concurrent requests at once."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from time import sleep

from app.ocr_service import recognize_text


def test_shared_predictor_is_locked_until_lazy_results_are_consumed(monkeypatch):
    state_lock = Lock()
    start = Barrier(2)
    active = 0

    class SharedEngine:
        def predict(self, image_path):
            class LazyResult:
                def json(self):
                    nonlocal active
                    with state_lock:
                        active += 1
                    try:
                        sleep(0.05)
                        with state_lock:
                            assert active == 1, "Concurrent use of shared predictor"
                        return {"rec_texts": [image_path]}
                    finally:
                        with state_lock:
                            active -= 1
            return [LazyResult()]

    engine = SharedEngine()
    monkeypatch.setattr("app.ocr_service._get_paddle_ocr_engine", lambda: engine)

    def run(path):
        start.wait(timeout=5)
        return recognize_text(path, mode="paddle")

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(run, ["front.png", "back.png"])) == [["front.png"], ["back.png"]]
