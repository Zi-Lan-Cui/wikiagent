"""mineru 后端：可选重依赖（含 torch）的版面解析，见 pyproject extra。"""

from .converter import MinerUConverter

__all__ = ["MinerUConverter"]
