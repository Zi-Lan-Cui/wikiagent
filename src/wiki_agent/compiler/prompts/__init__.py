"""compiler prompt 模块组——按模式组织，模块即命名空间。

接口约定（模式间同名函数同签名——Integrator/Extractor 统一调用）:
- compile.py: 全量共享（chunk/rolling/synthesis/search/analyze/plan/new_page/update）
- refine.py:  继承 compile，覆写 plan（wiki 自编译的当前页身份语义）

模式选择在 CompilePipeline(mode=...)——pipeline 持有对应模块，
Integrator/Extractor 不感知模式差异。

不把压缩/agent 等"与代码逻辑共同演化"的 prompt 收进来——
那些 prompt 与状态变量耦合，抽出去只会把契约断成两处。
"""

from wiki_agent.compiler.prompts import compile as compile
from wiki_agent.compiler.prompts import refine as refine

__all__ = ["compile", "refine"]
