"""Restricted JSON test executor. No LLM-generated Python or shell is executed."""

import json
import sys
from pathlib import Path

from test_evolution.discovery_oracles import TARGET_FILES, execute_input


def main() -> None:
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    if request["target"] not in TARGET_FILES or not 2 <= request["repeats"] <= 5 or len(request["inputs"]) > 20:
        raise ValueError("Execution request exceeds registered bounds")
    observations = [execute_input(request["target"], value, request["repeats"]) for value in request["inputs"]]
    with Path(sys.argv[2]).open("x", encoding="utf-8") as stream:
        json.dump(observations, stream, ensure_ascii=False)


if __name__ == "__main__":
    main()
