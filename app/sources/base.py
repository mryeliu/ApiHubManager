"""数据源适配层抽象。

所有数据源必须实现统一接口：反射表结构、执行自定义 SQL。
关系型实现见 sql.py。新类型（如未来 NoSQL）只需新增子类并注册。
"""
from abc import ABC, abstractmethod
from typing import Any, Optional


class SourceAdapter(ABC):
    source_id: str
    source_type: str

    @abstractmethod
    async def list_tables(self, q: str = "", schema: str = "",
                          page: int = 1, size: int = 50) -> dict:
        """返回匹配表名（过滤 + 分页），不一次性暴露全库（避坑 #2）。"""

    @abstractmethod
    async def get_table_meta(self, table: str) -> dict:
        """返回表字段 + 主键信息。"""

    @abstractmethod
    async def exec_sql(self, sql_template: str, params: dict, method: str) -> Any:
        """执行自定义 SQL（单条 DML/DQL，行数封顶，避坑 #29）。"""

    @abstractmethod
    async def test_connection(self) -> tuple[bool, str]:
        """返回 (是否成功, 错误信息)。错误信息用于前端排查。"""


# type -> adapter class
_REGISTRY: dict[str, type] = {}


def register(tp: str, cls: type) -> None:
    _REGISTRY[tp] = cls


def get_adapter_class(tp: str) -> type:
    if tp not in _REGISTRY:
        raise ValueError(f"不支持的数据源类型: {tp}")
    return _REGISTRY[tp]
