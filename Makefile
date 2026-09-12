RUN = uv run

.PHONY: test lint typecheck build eval-corpus eval-judge check

check: test lint typecheck

test:
	$(RUN) pytest -m "not live"

lint:
	$(RUN) ruff check .

typecheck:
	PYRIGHT_PYTHON_FORCE_VERSION=latest $(RUN) pyright

build:
	$(RUN) build

# 校验评测语料与判词题集（离线，不调 LLM）
eval-corpus:
	$(RUN) python evals/run.py corpus

# 运行 Judge 校准（需 LLM API；repeats=3 多数票）
eval-judge:
	$(RUN) python -m evals.commands.judge_pilot --repeats 3
