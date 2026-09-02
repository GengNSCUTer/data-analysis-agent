"""Canonical Spider schema/question/SQL serialization for SFT and inference.
The format is intentionally small and deterministic.  It contains table names,
column names and foreign-key relationships, but never database rows.  Keeping
this module shared prevents a base-versus-adapter comparison from accidentally
using a different prompt than the training corpus.
"""
from __future__ import annotations
import re
from collections import defaultdict
from typing import Any, Mapping

# 空白字符正则，用于把多个连续空白统一替换成单个空格
SPACE_RE = re.compile(r"\s+")
# Prompt中问题与待生成SQL之间的分隔标记
SQL_MARKER = "\n\n### SQL\n"

# v1版本：历史基线实验固定版本，不可修改，保证旧的Base/Adapter对比实验可复现
PROMPT_FORMAT_VERSION = "spider-sft-schema-question-sql-v1"
# v2版本：增强版Schema序列化格式，携带字段类型、主键、完整外键表名信息
PROMPT_FORMAT_VERSION_V2 = "spider-sft-schema-question-sql-v2"
# 支持的全部Prompt版本集合
SUPPORTED_PROMPT_FORMAT_VERSIONS = frozenset(
    {PROMPT_FORMAT_VERSION, PROMPT_FORMAT_VERSION_V2}
)


class SpiderSftFormatError(ValueError):
    """A Spider table metadata item cannot be serialized safely."""


def normalize_question(question: str) -> str:
    """Normalize formatting noise without changing benchmark semantics.
    标准化自然语言问题文本：清洗多余空白、空字符，不修改语义
    Args:
        question: 用户自然语言查询问句
    Returns:
        清洗后标准化字符串
    Raises:
        SpiderSftFormatError: 输入为空或者非字符串时报错
    """
    if not isinstance(question, str) or not question.strip():
        raise SpiderSftFormatError("question must be a non-empty string")
    # 先把空字符\x00替换为空格；再将所有连续空白字符压缩成单个空格；最后去除首尾空格
    return SPACE_RE.sub(" ", question.replace("\x00", " ")).strip()


def _normalize_schema_metadata(
    table: Mapping[str, Any], *, require_types_and_primary_keys: bool
) -> tuple[list[str], list[tuple[int, str]], list[str] | None, list[int]]:
    """Validate Spider metadata and return normalized table/column identities.
    校验Spider数据集数据库元数据，并返回标准化后的表、列信息
    Args:
        table: Spider一条数据库schema字典，包含table_names_original、column_names_original等字段
        require_types_and_primary_keys: 是否强制校验并返回列类型、主键信息（v2版本需要开启）
    Returns:
        (表名列表, 标准化列列表[(表索引,列名)], 列类型列表/None, 主键列索引列表)
    Raises:
        SpiderSftFormatError: schema元数据格式非法时报错
    """
    names = table.get("table_names_original")
    columns = table.get("column_names_original")

    # 校验表名列表合法性
    if not isinstance(names, list) or not all(isinstance(name, str) and name for name in names):
        raise SpiderSftFormatError("table_names_original must be a non-empty string list")
    # 校验列列表存在
    if not isinstance(columns, list):
        raise SpiderSftFormatError("column_names_original must be a list")

    normalized_columns: list[tuple[int, str]] = []
    for column in columns:
        # Spider列格式：每一项为 [所属表下标, 列名字符串]
        if not isinstance(column, (list, tuple)) or len(column) != 2:
            raise SpiderSftFormatError("each column_names_original entry must be a pair")
        table_index, column_name = column
        # 校验表索引与列名类型合法性
        if not isinstance(table_index, int) or not isinstance(column_name, str) or not column_name:
            raise SpiderSftFormatError("column metadata has an invalid table index or name")
        # -1代表*通配列（不属于任何表），不能超出表名数组边界
        if table_index < -1 or table_index >= len(names):
            raise SpiderSftFormatError("column metadata references an unknown table")
        normalized_columns.append((table_index, column_name))

    types = table.get("column_types")
    # v2分支：强制校验列类型、主键
    if require_types_and_primary_keys:
        if not isinstance(types, list) or len(types) != len(normalized_columns):
            raise SpiderSftFormatError("column_types must align with column_names_original")
        if not all(isinstance(column_type, str) and column_type for column_type in types):
            raise SpiderSftFormatError("column_types must be a non-empty string list")
        primary_keys = table.get("primary_keys", [])
        if not isinstance(primary_keys, list) or not all(
            isinstance(index, int) for index in primary_keys
        ):
            raise SpiderSftFormatError("primary_keys must be an integer list")
        # 校验主键下标指向合法列；不能是*通配列(table_index=-1)
        for index in primary_keys:
            if not 0 <= index < len(normalized_columns) or normalized_columns[index][0] < 0:
                raise SpiderSftFormatError("primary key references an unknown column")
        return list(names), normalized_columns, list(types), primary_keys
    # v1分支：不需要类型和主键，返回None与空主键列表
    return list(names), normalized_columns, None, []


def _normalized_foreign_keys(
    table: Mapping[str, Any], normalized_columns: list[tuple[int, str]]
) -> list[tuple[int, int]]:
    """解析并校验外键关系，返回标准化后的外键列索引对
    Args:
        table: Spider数据库schema字典
        normalized_columns: _normalize_schema_metadata输出的标准化列列表
    Returns:
        外键列表，每一项 (源列索引, 被引用目标列索引)
    Raises:
        SpiderSftFormatError: 外键格式非法时报错
    """
    foreign_keys = table.get("foreign_keys", [])
    if not isinstance(foreign_keys, list):
        raise SpiderSftFormatError("foreign_keys must be a list")

    normalized: list[tuple[int, int]] = []
    for foreign_key in foreign_keys:
        # Spider外键格式：一对列索引 [源列下标, 目标列下标]
        if not isinstance(foreign_key, (list, tuple)) or len(foreign_key) != 2:
            raise SpiderSftFormatError("each foreign key must be a column-index pair")
        left, right = foreign_key
        if not isinstance(left, int) or not isinstance(right, int):
            raise SpiderSftFormatError("foreign-key indexes must be integers")
        # 列索引必须落在合法范围内
        if not 0 <= left < len(normalized_columns) or not 0 <= right < len(normalized_columns):
            raise SpiderSftFormatError("foreign key references an unknown column")
        # 外键不能引用不属于任何表的*通配列(table_index=-1)
        if normalized_columns[left][0] < 0 or normalized_columns[right][0] < 0:
            raise SpiderSftFormatError("foreign key cannot reference the wildcard column")
        normalized.append((left, right))
    return normalized


def serialize_spider_schema(table: Mapping[str, Any]) -> str:
    """Render the immutable v1 Spider metadata representation.
    V1版本Schema序列化：生成精简文本格式，不含字段类型、主键标记
    输出示例：
    TABLE student: id,name,age
    FOREIGN_KEYS: class_id -> id
    Args:
        table: Spider数据库schema字典
    Returns:
        拼接好的schema提示文本字符串
    """
    names, normalized_columns, _, _ = _normalize_schema_metadata(
        table, require_types_and_primary_keys=False
    )
    # 将列按照所属表下标分组
    grouped: dict[int, list[str]] = defaultdict(list)
    for table_index, column_name in normalized_columns:
        if table_index >= 0:
            grouped[table_index].append(column_name)

    # 逐一生成TABLE行文本
    lines = [
        f"TABLE {table_name}: {', '.join(grouped.get(index, [])) or '<no_columns>'}"
        for index, table_name in enumerate(names)
    ]
    # 添加外键文本行
    foreign_keys = _normalized_foreign_keys(table, normalized_columns)
    if foreign_keys:
        references: list[str] = []
        for left, right in foreign_keys:
            references.append(f"{normalized_columns[left][1]} -> {normalized_columns[right][1]}")
        lines.append("FOREIGN_KEYS: " + "; ".join(references))
    return "\n".join(lines)


def serialize_spider_schema_v2(table: Mapping[str, Any]) -> str:
    """Render qualified table-column identities, types, PKs and full FKs.
    This format is deliberately separate from v2.  It makes foreign-key sides
    unambiguous and exposes key/type cues that are useful for schema linking,
    while still excluding database rows and values.
    V2增强版Schema序列化：完整输出表名.列名、字段类型、主键标记、带双表名的外键关系
    输出示例：
    TABLE student
      student.id: INTEGER [PRIMARY KEY]
      student.name: TEXT
    FOREIGN_KEYS
      student.class_id -> class.id
    Args:
        table: Spider数据库schema字典
    Returns:
        增强版schema提示文本字符串
    """
    names, normalized_columns, types, primary_keys = _normalize_schema_metadata(
        table, require_types_and_primary_keys=True
    )
    assert types is not None  # Narrowed by require_types_and_primary_keys.

    # key:表下标，value:列表[(全局列索引,列名)]
    grouped: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for column_index, (table_index, column_name) in enumerate(normalized_columns):
        if table_index >= 0:
            grouped[table_index].append((column_index, column_name))

    primary_key_set = set(primary_keys)
    lines: list[str] = []

    # 遍历每张表，打印表名+带类型、主键标记的每一列
    for table_index, table_name in enumerate(names):
        lines.append(f"TABLE {table_name}")
        columns = grouped.get(table_index, [])
        if not columns:
            lines.append("  <no_columns>")
            continue
        for column_index, column_name in columns:
            key_marker = " [PRIMARY KEY]" if column_index in primary_key_set else ""
            lines.append(f"  {table_name}.{column_name}: {types[column_index]}{key_marker}")

    # 打印带完整表限定名的外键
    foreign_keys = _normalized_foreign_keys(table, normalized_columns)
    if foreign_keys:
        lines.append("FOREIGN_KEYS")
        for left, right in foreign_keys:
            left_table, left_column = normalized_columns[left]
            right_table, right_column = normalized_columns[right]
            lines.append(
                f"  {names[left_table]}.{left_column} -> {names[right_table]}.{right_column}"
            )
    return "\n".join(lines)


def serialize_spider_schema_for_version(
    table: Mapping[str, Any], prompt_format_version: str
) -> str:
    """Select a serializer without silently changing historical prompt contracts.
    根据指定版本号分发调用对应的schema序列化函数，防止旧实验Prompt格式被静默修改
    Args:
        table: Spider数据库schema字典
        prompt_format_version: 版本字符串 v1 / v2
    Returns:
        对应版本序列化后的schema文本
    Raises:
        SpiderSftFormatError: 传入不支持的版本时报错
    """
    if prompt_format_version == PROMPT_FORMAT_VERSION:
        return serialize_spider_schema(table)
    if prompt_format_version == PROMPT_FORMAT_VERSION_V2:
        return serialize_spider_schema_v2(table)
    raise SpiderSftFormatError(f"unsupported prompt format version: {prompt_format_version}")


def render_sft_prompt(
    question: str, schema: str, prompt_format_version: str = PROMPT_FORMAT_VERSION
) -> str:
    """Render the common inference prefix ending at the SQL completion point.
    生成推理阶段给大模型的输入Prompt前缀（不包含目标SQL），模型需要在### SQL\n后面续写SQL
    Args:
        question: 自然语言问句
        schema: 已经序列化好的schema文本字符串
        prompt_format_version: prompt版本号
    Returns:
        拼接完成的推理提示词
    """
    if not isinstance(schema, str) or not schema.strip():
        raise SpiderSftFormatError("schema must be a non-empty string")
    if prompt_format_version not in SUPPORTED_PROMPT_FORMAT_VERSIONS:
        raise SpiderSftFormatError(f"unsupported prompt format version: {prompt_format_version}")
    return f"### SQLite schema\n{schema}\n\n### Question\n{normalize_question(question)}{SQL_MARKER}"


def render_sft_training_text(
    question: str,
    schema: str,
    sql: str,
    prompt_format_version: str = PROMPT_FORMAT_VERSION,
) -> str:
    """Render one SFT text record, retaining the target only outside Git.
    生成完整SFT训练样本文本 = Prompt前缀 + 标准答案SQL，用于监督微调数据集
    Args:
        question: 自然语言问句
        schema: 序列化后的schema文本
        sql: 目标标准答案SQL语句
        prompt_format_version: prompt版本号
    Returns:
        整条训练用长文本
    """
    if not isinstance(sql, str) or not sql.strip():
        raise SpiderSftFormatError("SQL target must be a non-empty string")
    return render_sft_prompt(question, schema, prompt_format_version) + sql.strip()
