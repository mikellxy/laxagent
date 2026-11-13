import json

ERROR_GUIDANCE = {
    "INVALID_FILE_SCOPE": 'skill_name 不能是字符串 "null"。本次未执行文件操作。若操作 workspace 文件，请使用 JSON null（例如 "skill_name": null）后重试；若操作技能文件，请传真实技能名。',
    "INVALID_SKILL_NAME": "技能名称须为不超过 64 字符的小写字母、数字和单连字符，且不能以连字符开头或结尾。",
    "INVALID_SKILL_METADATA": "SKILL.md 元数据无效或缺失，请使用 skill-creator 指导修复；name 必须与技能目录名一致。",
    "INVALID_SKILL_FILE": "技能只能包含普通目录和文件，不允许符号链接、硬链接或特殊文件。",
    "SKILL_TOO_LARGE": "技能超过文件数量、大小或目录深度限制，请缩减内容后重试。",
    "SKILL_NOT_FOUND": "指定技能不存在，请核对名称。",
    "SKILL_NOT_LOADED": "执行脚本前须在当前 run 中先调用 load_skill。",
    "SKILL_CHANGED": "技能内容已变化，本次未执行脚本；请重新 load_skill 后再决定下一步。",
    "INVALID_SKILL_SCRIPT": "script 必须是 load_skill 返回的完整相对文件路径。",
    "SKILL_ENTRY_REQUIRED": "不能删除 SKILL.md，请通过 write_file 或 edit_file 修改。",
    "TOOL_STATE_UNCERTAIN": "异常中断，工具执行状态未知，请先检查工作区状态，不要盲目重试",
    "MEMORY_UNAVAILABLE": "目标记忆不可用，可能已被替换；本批未执行，请重新查询相关分类的摘要。",
    "MEMORY_STORAGE_FAILED": "记忆存储操作未能确认成功；请先查询核实状态，不要盲目重试写入。",
    "SANDBOX_UNAVAILABLE": "Bash 沙箱不可用，需要 Linux 和可执行的 /usr/bin/bwrap；不会退回宿主执行。",
    "SANDBOX_INVALID_WORKSPACE": "Bash workspace 无效，请检查项目工作目录及其路径边界。",
    "SANDBOX_START_FAILED": "Bash 沙箱启动失败，请检查系统依赖、user namespace 和 AppArmor 配置。",
    "BASH_TIMEOUT": "命令已超时并清理沙箱进程；已产生的 workspace 修改不会回滚，请先核实结果再继续。",
    "UNKNOWN_TOOL": "请使用当前提供的工具。",
    "INVALID_ARGUMENTS": "请按工具参数定义传入 JSON 对象，检查必填字段、字段类型和取值范围。",
    "INVALID_PATH": "请使用所选 workspace 或 skill 内的相对路径，不允许 .. 或符号链接。",
    "FILE_NOT_FOUND": "文件不存在，请核对相对路径。",
    "ACCESS_DENIED": "无法访问文件或目录，请核对相应的读写权限。",
    "NOT_A_FILE": "只能读写普通文件，请提供文件路径。",
    "INVALID_POSITION": "起点超出文件或不在 UTF-8 字符边界，请使用上次返回的游标；首次读取从第 1 行第 1 字节开始。",
    "NOT_UTF8_TEXT": "只支持 UTF-8 文本，不支持二进制或其他编码文件。",
    "READ_FAILED": "读取失败，请检查文件是否仍然可访问。",
    "WRITE_FAILED": "写入失败，请检查目标目录是否仍可访问及磁盘空间是否充足。",
    "EDIT_NOT_FOUND": "未找到待替换文本；本批未写入，请重新读取文件。仅完整行匹配容忍行首行尾空格、Tab 和 LF/CRLF 差异。",
    "EDIT_AMBIGUOUS": "待替换文本匹配到多处；本批未写入，请增加上下文以唯一定位。",
    "EDIT_OVERLAP": "多组编辑的实际匹配区间重叠；本批未写入，请合并重叠编辑或选择互不重叠的区间。",
    "EDIT_POSITION_CHANGED": "替换前原始位置的文本校验失败；本批未写入，请重新读取文件后提交编辑。",
}


def format_result(result: dict) -> str:
    guidance = ERROR_GUIDANCE.get(result["error_code"], "")
    return (guidance + "\n" if guidance else "") + json.dumps(result, ensure_ascii=False)
