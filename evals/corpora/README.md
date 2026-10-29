# 语料（corpora）

评测用的编译数据，按角色分 `dev/` 与 `test/` 两目录，由 `evals/run.py recompile` 从源文件生成。

| 目录      | 角色       | 用途                                               |
| --------- | ---------- | -------------------------------------------------- |
| `dev/`  | 校准基线集 | 开发与校准评分标准用，可反复重编译                 |
| `test/` | 留出评分集 | 产出最终评测结果用 |

两者内部布局一致（`baselines/seeded/{source,run-evidence,wiki}`、`manifest.json`、
`runs/`），`source-eval --corpus` 按此布局读取任一角色目录。

生成与评测：

```
uv run python evals/run.py recompile --sources-dir <源文件目录> --corpus evals/corpora/dev
uv run python evals/run.py source-eval --corpus evals/corpora/dev   # 或 test
```
