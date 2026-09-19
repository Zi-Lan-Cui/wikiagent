"""compiler prompt 模块组——按模式组织，模块即命名空间。

模式间同名 prompt 共享签名，供编译链路统一调用；
refine 只覆写 plan（wiki 自编译的当前页身份语义）。
模式选择在流水线组装时确定，编译阶段不感知模式差异。

与状态变量耦合的 prompt（上下文压缩、agent 回合）留在对应模块旁，
不收进这里——拆开会断契约。
"""

from wiki_agent.compiler.prompts import compile as compile
from wiki_agent.compiler.prompts import refine as refine

__all__ = ["compile", "refine"]
