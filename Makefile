RUN = uv run

.PHONY: test lint typecheck build eval-corpus eval-source eval-rubric eval-ci check

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

# 评分器定义自检（离线）
eval-rubric:
	$(RUN) python evals/run.py rubric-check

# 按 source 生成评测记录（需 LLM）
eval-source:
	$(RUN) python evals/run.py source-eval

# 全 source 评测；确定性拦截或判 fail 即非零退出，可作 CI gate（需 LLM）
eval-ci:
	$(RUN) python evals/run.py ci
