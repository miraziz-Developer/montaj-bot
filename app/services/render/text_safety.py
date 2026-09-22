import re

# C0/C1 control characters and the Unicode line/paragraph separators (they can end an ASS event line).
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")


# SECURITY: every piece of user-influenced text (STT words, overlay text, watermark, title) MUST pass through
# this function before it is written into an ASS file (EDIT_PLAN_SCHEMA.md section 8). The result can never
# contain an override block or a line break: braces are dropped, backslashes become "/", control characters
# become spaces. Line breaks in captions are added by OUR code as a literal "\N", never taken from input.
def escape_ass(text: str) -> str:
    text = _CONTROL.sub(" ", text)  # 1. control characters (a newline must not end the event line)
    text = text.replace("\\", "/")  # 2. no backslash -> no \N, \h, \pos ... from the input
    text = text.replace("{", "").replace(
        "}", ""
    )  # 3. no override blocks; also covers rule 5 (cannot start one)
    return " ".join(text.split())  # 4. collapse whitespace
