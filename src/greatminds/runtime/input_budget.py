"""Limits for client-submitted UTF-8 text, independent of provider token usage."""
from greatminds.core.errors import GreatMindsError


class InputBudgetExceeded(GreatMindsError):
    def __init__(self, kind, *, limit, used, requested):
        self.details = {"budget": kind, "limit_bytes": limit,
                        "used_bytes": used, "requested_bytes": requested}
        super().__init__(f"{kind} input budget exceeded", exit_code=2)


def check_prompt(prompt, binding):
    size = len(prompt.encode("utf-8"))
    if size > binding.max_prompt_bytes:
        raise InputBudgetExceeded("prompt", limit=binding.max_prompt_bytes,
                                  used=0, requested=size)
    return size
