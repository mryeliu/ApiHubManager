# 导入各数据源适配实现，触发 register() 注册（否则 get_adapter_class 找不到类型）。
from . import sql  # noqa: F401  (注册 mysql/postgresql/sqlserver)
