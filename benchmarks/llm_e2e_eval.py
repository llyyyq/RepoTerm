"""真实 LLM 端到端评测的命令行入口。"""

from __future__ import annotations

from pathlib import Path
import sys


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from repoterm.llm_e2e_eval import main


if __name__ == "__main__":
    raise SystemExit(main())
