"""One-line explanations of config mistakes, with the name probably meant."""

import difflib
from typing import Any

from pydantic import BaseModel, ValidationError
from pydantic_settings import BaseSettings


def known_keys() -> list[str]:
    """Every setting name of tbot's config models, for "did you mean" hints; not the .env
    settings, which no config file takes."""
    names: set[str] = set()
    models: list[type[BaseModel]] = [BaseModel]
    while models:
        model = models.pop()
        models.extend(model.__subclasses__())
        if model.__module__.startswith("tbot.") and not issubclass(model, BaseSettings):
            names.update(model.model_fields)
    return sorted(names)


def problem(error: Any, prefix: str) -> str:
    """One pydantic error: where, and what is wrong."""
    where = prefix + ".".join(str(part) for part in error["loc"]) or "config"
    if error["type"] == "extra_forbidden":
        key = str(error["loc"][-1])
        close = [k for k in difflib.get_close_matches(key, known_keys(), n=2) if k != key]
        return f"{where}: unknown key" + (f" (did you mean {close[0]}?)" if close else "")
    message = str(error["msg"])
    limit = (error.get("ctx") or {}).get("lt", (error.get("ctx") or {}).get("le"))
    if limit == 1:
        message += " (a fraction: 0.45 means 45%)"
    return f"{where}: {message}"


def validation_text(exc: ValidationError, prefix: str = "") -> str:
    return "; ".join(problem(error, prefix) for error in exc.errors())
