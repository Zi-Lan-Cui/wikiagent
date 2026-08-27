"""兼容导入：请改用 :mod:`wiki_agent.compiler.integration`。"""

from wiki_agent.compiler.integration.workflow import (
    Integrator,
    compile_integrator,
    refine_integrator,
)

__all__ = ["Integrator", "compile_integrator", "refine_integrator"]
