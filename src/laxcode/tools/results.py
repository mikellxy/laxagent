import json

ERROR_GUIDANCE = {
    "UNKNOWN_TOOL": "请使用当前提供的工具。",
    "INVALID_ARGUMENTS": "请按工具参数定义传入 JSON 对象，行号、字节位置和行数必须为正整数。",
    "INVALID_PATH": "请使用 workspace 内的相对路径，不允许 .. 或符号链接。",
    "FILE_NOT_FOUND": "文件不存在，请核对相对路径。",
    "ACCESS_DENIED": "文件不可读，请核对文件权限。",
    "NOT_A_FILE": "只能读取普通文件，请提供文件路径。",
    "INVALID_POSITION": "起点超出文件或不在 UTF-8 字符边界，请使用上次返回的游标；首次读取从第 1 行第 1 字节开始。",
    "NOT_UTF8_TEXT": "只支持 UTF-8 文本，不支持二进制或其他编码文件。",
    "READ_FAILED": "读取失败，请检查文件是否仍然可访问。",
}


def format_result(result: dict) -> str:
    guidance = ERROR_GUIDANCE.get(result["error_code"], "")
    return (guidance + "\n" if guidance else "") + json.dumps(result, ensure_ascii=False)
