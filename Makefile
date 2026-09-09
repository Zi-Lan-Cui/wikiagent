# 统一评测流水线（v3）：数据集 → 自检 → 对抗 → 人工金标 → judge↔人一致率 → 报告。
# 只有 eval-snapshot / eval-agreement 需要真实 LLM（用你 env/.env 的 key，会产生费用）。
DATASET ?= workspace/evals/wiki-golden-30.json
DATE    ?= $(shell date -u +%F)
RUN     = uv run python -m

.PHONY: eval-all eval-dataset eval-validate eval-adversarial eval-label eval-label-worksheet \
        eval-apply-labels eval-agreement eval-snapshot eval-headline

# 离线：生成数据集（含自检）+ 对抗例自检（证明结构缺陷可被抓住）
eval-all: eval-dataset eval-adversarial

eval-dataset:
	$(RUN) evals.commands.build_dataset

# 校验已有数据集 schema + 参考解过确定性门禁（离线）
eval-validate:
	$(RUN) evals.commands.validate $(DATASET)

# 注入已知缺陷 → A 层结构门禁应全抓住；编造留给 judge（离线）
eval-adversarial:
	$(RUN) evals.commands.adversarial $(DATASET)

# 交互逐条引导人工金标（写 ratify.json，可续标）
eval-label:
	$(RUN) evals.commands.label $(DATASET) --interactive

eval-label-worksheet:
	$(RUN) evals.commands.label $(DATASET) --worksheet workspace/evals/label_worksheet.md

# ratify.json = {"<id>": {"human_verdict":"pass|fail|review","reviewer":"you"}}
eval-apply-labels:
	$(RUN) evals.commands.label $(DATASET) --apply workspace/evals/ratify.json --in-place

# judge ↔ 人工金标（含对抗例）→ evals/reports/$(DATE)/agreement.json   [真实 LLM]
eval-agreement:
	$(RUN) evals.commands.agreement $(DATASET) --date $(DATE)

# 对当前 wiki 跑分层判定 A∧B + pass^k（repeats=3）              [真实 LLM]
eval-snapshot:
	$(RUN) evals.commands.snapshot $(DATASET) --repeats 3 --output workspace/evals/snapshot-pass3.json

# 汇总 HEADLINE.md + manifest.json（离线）
eval-headline:
	$(RUN) evals.commands.report $(DATASET) \
	  --agreement evals/reports/$(DATE)/agreement.json \
	  --snapshot workspace/evals/snapshot-pass3.json \
	  --date $(DATE)
