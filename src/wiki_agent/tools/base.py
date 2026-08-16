from abc import abstractmethod,ABC
from typing import ClassVar
from wiki_agent.log import get_logger, emit_event
from wiki_agent.errors import FatalError, WikiAgentError, translate_generic_error

logger=get_logger("TOOL")

class BaseTool(ABC):
    name:ClassVar[str]
    description:ClassVar[str]
    # schema 是嵌套 dict（type/properties/required）——不是 str。
    # 旧 property description 已删: 与子类 ClassVar 同名的 property
    # 是死代码 + 递归陷阱（property 体内访问 self.description）；
    # 现役工具正常只因子类重定义了 ClassVar 遮蔽它。
    parameters:ClassVar[dict]

    async def execute(self,**kwargs)->str:
        """
        工具执行的入口——工具边界。

        边界规则:
        - FatalError（编程 bug/配置错/未知异常）→ 记录 + 事件 + 转"内部错误"文本。
          不向 LLM 泄漏内部细节，也不静默吞掉——日志可排查。
        - 其他 WikiAgentError（可预期的业务错误）→ 转可读错误文本给 LLM，
          LLM 能根据错误调整策略（换参数/换工具）。
        """
        try:
            result= await self._execute(**kwargs)
        except WikiAgentError as e:
            # 已分类的异常——按类型走边界决策
            if isinstance(e, FatalError):
                logger.exception("工具 %s 内部致命错误", self.name)
                # 机器通道不截断（截断是给人看的习惯）
                emit_event("tool_error", tool=self.name, error_type="fatal", error=str(e))
                result=f"Error: 工具 {self.name} 内部错误——请检查服务端日志"
            else:
                result=f"Error: {self.name} - {e}"
        except Exception as e:
            # 未分类异常 → 翻译 → 递归走上面的分支（translate 默认 Fatal）
            translated = translate_generic_error(e, context=f"工具 {self.name}")
            if isinstance(translated, FatalError):
                logger.exception("工具 %s 内部致命错误", self.name)
                emit_event("tool_error", tool=self.name, error_type="fatal", error=str(e))
                result=f"Error: 工具 {self.name} 内部错误——请检查服务端日志"
            else:
                result=f"Error: {self.name} - {translated}"
        return result

    @abstractmethod
    async def _execute(**kwargs)->str:
        """
        真正的执行逻辑，工具子类必须重写这个函数
        """
        pass

    @classmethod
    def openai_schema(cls):
        return {
            "type":"function",
            "function":{
                "name":cls.name,
                "description":cls.description,
                "parameters":cls.parameters
            }
        }
