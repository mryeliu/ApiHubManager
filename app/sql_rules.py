"""API Hub SQL 生成规范校验（占位符统一 :xxx）。

与运行时的 _validate_sql（多语句 / DDL 拦截）互补：本模块实现「业务侧规范」，
在接口「创建 / 更新」时拦截不符合规范的自定义 SQL，避免上线后出事故。

规范要点：
  ✅ HTTP：SELECT 允许 GET/POST；INSERT/UPDATE/DELETE 仅 POST，禁用 GET
  ✅ 占位符：外部变量必须带 :，严禁 ? / %s / $1 等
  ✅ NULL：只用 IS NULL / IS NOT NULL，禁止 = NULL
  ✅ 高危 DML：UPDATE/DELETE 必须带 WHERE；必须加行数兜底（SQL Server TOP / MySQL LIMIT）
  ✅ SQL Server 的 TOP 必须带括号：TOP (n) / TOP (:pageSize)
  ❌ 禁止：无 WHERE 的 DELETE/UPDATE、恒成立条件（id=id / 1=1）、SQL Server TOP 不加括号

校验是「方言感知」的：dialect ∈ {mysql, sqlserver, postgresql}。
返回结构：{"errors": [...], "warnings": [...]}；errors 非空即拒绝保存。
"""
import re
from .sources.sql import _strip_string_literals

_DML = ("INSERT", "UPDATE", "DELETE")
_KIND_OF = re.compile(r"^\s*\(?\s*([A-Za-z]+)", re.IGNORECASE)


def _first_keyword(sql: str) -> str:
    s = sql.strip().rstrip(";").strip()
    m = _KIND_OF.match(s)
    return m.group(1).upper() if m else ""


def validate_api_sql(sql: str, dialect: str, methods: str = "") -> dict:
    """按 API Hub SQL 生成规范校验。dialect: mysql|sqlserver|postgresql。"""
    errors: list[str] = []
    warnings: list[str] = []

    s = (sql or "").strip().rstrip(";").strip()
    if not s:
        return {"errors": ["SQL 不能为空"], "warnings": warnings}
    # 去掉字符串字面量再做「结构类」检查，避免 ';' / 'NULL' 误判
    safe = _strip_string_literals(s)
    first = _first_keyword(safe)
    is_dml = first in _DML
    is_select = first == "SELECT"

    # ---- HTTP 方法约定 ----
    mset = [m.strip().upper() for m in (methods or "").split(",") if m.strip()]
    if is_dml:
        if "GET" in mset:
            errors.append("INSERT/UPDATE/DELETE 为高危 DML，仅允许 POST，禁止 GET（SQL 生成规范）")
        if "POST" not in mset:
            errors.append("INSERT/UPDATE/DELETE 必须使用 POST 方法（SQL 生成规范）")
    if is_select and mset and "GET" not in mset and "POST" not in mset:
        warnings.append("SELECT 建议至少允许 GET 或 POST 之一")

    # ---- 占位符：必须统一 :name ----
    if "?" in safe or "%s" in safe or re.search(r"\$\d+", safe):
        errors.append("占位符必须统一为 :name 形式（如 :pageSize），禁止 ? / %s / $1 等（SQL 生成规范）")

    # ---- NULL 语法：只能 IS NULL / IS NOT NULL ----
    # 关键：不能对 before 做 rstrip，否则会抹掉「IS 与 NULL 之间的空白」，
    # 导致 IS\s+ 无法匹配而误杀合法的 IS NULL。
    for m in re.finditer(r"NULL", safe, re.IGNORECASE):
        before = safe[: m.start()]
        if not re.search(r"IS\s+(NOT\s+)?$", before, re.IGNORECASE):
            errors.append("NULL 只能配合 IS NULL / IS NOT NULL，禁止 = NULL（SQL 生成规范）")
            break

    # ---- 恒成立条件（字段=同名字段 / 1=1 / 'x'='x'）----
    # 裸标识符相等（两侧均不含表限定符「.」，避免误杀 a.id=b.id）
    if re.search(r"(?<![\w.])(\w+)\s*=\s*\1(?!\w)", safe, re.IGNORECASE):
        errors.append("检测到恒成立条件（如 字段=同名字段 / 1=1），无意义且危险，已禁止保存（SQL 生成规范）")
    # 字符串字面量相等（如 'status'='status'）
    elif re.search(r"'(\w+)'\s*=\s*'\1'", safe, re.IGNORECASE):
        errors.append("检测到恒成立条件（如 '字段'='字段'），无意义且危险，已禁止保存（SQL 生成规范）")
    # 不等号自比（id != id）
    elif re.search(r"(?<![\w.])(\w+)\s*(!=|<>)\s*\1(?!\w)", safe, re.IGNORECASE):
        errors.append("检测到恒成立条件（如 字段!=同名字段），无意义且危险，已禁止保存（SQL 生成规范）")

    # ---- 高危 DML：WHERE + 行数兜底 ----
    if first in ("UPDATE", "DELETE"):
        if not re.search(r"\bWHERE\b", safe, re.IGNORECASE):
            errors.append(f"{first} 必须带 WHERE 条件，禁止无条件的全表操作（SQL 生成规范）")
        if dialect == "sqlserver":
            if not re.search(r"\bTOP\s*\(", safe, re.IGNORECASE):
                errors.append("SQL Server 高危 DML 必须加行数兜底：UPDATE/DELETE 需 TOP (n)（括号不能丢）")
        elif dialect == "mysql":
            if not re.search(r"\bLIMIT\b", safe, re.IGNORECASE):
                errors.append("MySQL 高危 DML 必须加行数兜底：UPDATE/DELETE 需 LIMIT n")
        elif dialect == "postgresql":
            # PG UPDATE 不支持行数限制（规范明确）；DELETE 用 LIMIT（如主键子查询）
            if first == "DELETE" and not re.search(r"\bLIMIT\b", safe, re.IGNORECASE):
                errors.append("PostgreSQL 高危 DELETE 必须加行数兜底：需用 LIMIT（如主键子查询 LIMIT n）")
        # 未识别方言：保守要求 LIMIT/TOP 任一存在
        elif not re.search(r"\b(LIMIT|TOP\s*\()", safe, re.IGNORECASE):
            errors.append("高危 DML 必须加行数兜底（LIMIT 或 TOP）")

    # ---- SQL Server TOP 必须带括号 ----
    if dialect == "sqlserver":
        # TOP 后紧跟空白但「不是」左括号 → TOP n（漏括号），报错；
        # TOP(...) / TOP (...) 均匹配不到此模式，放行。
        if re.search(r"\bTOP\s+(?!\()", safe, re.IGNORECASE):
            errors.append("SQL Server 的 TOP 必须带括号：TOP (n) / TOP (:pageSize)（括号不能丢）")

    return {"errors": errors, "warnings": warnings}
