"""Paper and live session configuration (YAML) and secrets (environment)."""

import contextlib
import difflib
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Literal, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from tbot.core.config import TradingConfig, load_yaml
from tbot.risk.guard import GuardConfig

ONE_QUOTE = "a book keeps one cash, so every symbol of a session needs the same quote asset"
FRACTION = "a fraction (0.002 means 0.2%)"
MAX_TOLERANCE = 0.05  # a reconciliation mismatch beyond 5% is never noise
MAX_BUY_LIMIT = 0.5  # a buy limit beyond 50% above the close limits nothing
OTHER_VARIABLES = {"TBOT_SUPERVISED", "TBOT_DEBUG"}  # read from the environment, not a setting
ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


class SessionConfig(TradingConfig):
    ledger: Path
    guard: GuardConfig = GuardConfig()
    heartbeat_seconds: int = Field(default=300, ge=30, le=86400)
    stale_after_seconds: int = Field(default=600, ge=60, le=7 * 86400)  # bar overdue: alert
    # Wait for streams closing together.
    batch_wait_seconds: float = Field(default=5.0, ge=0, le=300, allow_inf_nan=False)
    summary_hour_utc: int = Field(default=0, ge=0, le=23)
    backup_days: int = Field(default=14, ge=0)  # daily ledger copies kept; 0 turns them off
    # Answer /status, /fills, and /halt from the Telegram chat. The sessions of a clone
    # take turns reading the bot's messages; a real-money one goes first.
    telegram_commands: bool = True
    # A buy pays at most this much above the close it was decided on; when the price is
    # already beyond, the buy waits for the next close. Sales always go out. 0: no limit.
    buy_limit_pct: float = Field(default=0.02, ge=0, allow_inf_nan=False)

    @field_validator("buy_limit_pct")
    @classmethod
    def limit_fraction(cls, value: float) -> float:
        if value > MAX_BUY_LIMIT:  # 2 meant as 2% would let a buy pay three times the close
            raise ValueError(f"must be at most {MAX_BUY_LIMIT:g}: a fraction (0.02 means 2%)")
        return value


class PaperConfig(SessionConfig):
    ledger: Path = Path("data/paper.sqlite")


class LiveConfig(SessionConfig):
    mode: Literal["testnet", "live"]
    ledger: Path = Path("data/live.sqlite")  # data/testnet.sqlite in testnet mode
    # budget: the bot owns initial_cash and what it buys; other balances are left alone.
    # account: the bot owns the whole spot account (use only for a dedicated account).
    ownership: Literal["budget", "account"] = "budget"
    protective_stop_pct: float = Field(default=0.2, ge=0, lt=1)  # 0 disables exchange stops
    reconcile_seconds: int = Field(default=300, ge=30, le=86400)
    # Relative mismatch that is noise; a percent typed as such (0.2 for 0.2%) is refused.
    reconcile_tolerance: float = Field(default=0.002, ge=0, allow_inf_nan=False)
    recv_window: int = Field(default=5000, ge=1000, le=60000)

    @field_validator("reconcile_tolerance")
    @classmethod
    def tolerance_fraction(cls, value: float) -> float:
        if value > MAX_TOLERANCE:  # 0.2 meant as 0.2% would let a 20% mismatch pass
            raise ValueError(f"must be at most {MAX_TOLERANCE:g}: {FRACTION}")
        return value

    @model_validator(mode="before")
    @classmethod
    def ledger_per_mode(cls, data: Any) -> Any:
        if isinstance(data, dict) and "ledger" not in data and data.get("mode") == "testnet":
            return {**data, "ledger": Path("data/testnet.sqlite")}
        return data

    @model_validator(mode="after")
    def budget_for_real_money(self) -> Self:
        """Real money never trades a default amount."""
        if self.mode == "live" and "initial_cash" not in self.model_fields_set:
            raise ValueError("set initial_cash: the real-money budget, the most the bot can lose")
        return self


def looks_like_session(raw: dict[str, Any]) -> bool:
    """A paper, testnet, or live config: `mode` says so (no backtest or validation config
    takes it), else no backtest `start` and no validation `backtest` or `grid`."""
    return "mode" in raw or not ({"start", "backtest", "grid"} & raw.keys())


def named_ledger(raw: dict[str, Any]) -> Path | None:
    """The ledger a session config names, defaults included, without checking the rest:
    a session still runs on it after an edit breaks the file. None if it names none."""
    named = raw.get("ledger")
    if isinstance(named, str):
        return Path(named)
    mode = raw.get("mode")
    if named is not None or mode not in (None, "testnet", "live"):
        return None
    return Path(f"data/{mode or 'paper'}.sqlite")


def load_paper_config(path: Path) -> PaperConfig:
    return PaperConfig.model_validate(load_yaml(path.read_text(encoding="utf-8")))


def load_live_config(path: Path) -> LiveConfig:
    return LiveConfig.model_validate(load_yaml(path.read_text(encoding="utf-8")))


class EnvUnreadable(ValueError):
    """.env is there but cannot be used as written: every start would fail the same way."""


def env_problem(path: Path) -> str:
    """Why this .env cannot be read as written, or ""."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return ""
    except OSError as exc:  # owned by another account, such as an elevated prompt's
        return f"cannot read {path} ({exc.strerror}): give your account access to it"
    if b"\x00" in data:  # PowerShell 5.1 `>>` appends UTF-16 to a UTF-8 file
        return (
            f"{path} holds UTF-16 text, which is not read (PowerShell's `>`, `>>`, and "
            "Out-File write it): open it in Notepad and save it as UTF-8"
        )
    return ""


def env_notes(text: str, names: set[str]) -> list[str]:
    """Lines of a .env that set nothing: an unknown TBOT_ name (a typo), or a line in
    another shell's syntax (`set X=`, `$env:X=`)."""
    notes = []
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = ENV_LINE.match(line)
        if match is None:
            notes.append(f"line {number} is not NAME=value, so it sets nothing")
            continue
        name = match.group(1).upper()
        if name.startswith("TBOT_") and name not in names | OTHER_VARIABLES:
            close = difflib.get_close_matches(name, sorted(names), n=1)
            hint = f" (did you mean {close[0]}?)" if close else ""
            notes.append(f"line {number}: {name} is no tbot setting{hint}")
    return notes


class Settings(BaseSettings):
    """Secrets and endpoints from the environment or a .env file."""

    model_config = SettingsConfigDict(env_prefix="TBOT_", env_file=".env", extra="ignore")

    @classmethod
    def names(cls) -> set[str]:
        """The .env names of the settings."""
        return {f"TBOT_{name.upper()}" for name in cls.model_fields}

    telegram_token: str | None = None
    telegram_chat_id: str | None = None
    heartbeat_url: str | None = None
    binance_api_key: str | None = None
    binance_api_secret: str | None = None
    # Testnet keys of their own, so a testnet session keeps running once the main pair is
    # a live key; without them testnet uses the main pair.
    binance_testnet_api_key: str | None = None
    binance_testnet_api_secret: str | None = None

    def api_keys(self, mode: str) -> tuple[str, str] | None:
        """The key and secret for a testnet or live session, or None when not set."""
        if mode == "testnet" and self.binance_testnet_api_key and self.binance_testnet_api_secret:
            return self.binance_testnet_api_key, self.binance_testnet_api_secret
        if self.binance_api_key and self.binance_api_secret:
            return self.binance_api_key, self.binance_api_secret
        return None

    def key_names(self, mode: str) -> str:
        """The .env lines of the pair api_keys picks, for messages about a refused key."""
        if mode == "testnet" and self.binance_testnet_api_key and self.binance_testnet_api_secret:
            return "TBOT_BINANCE_TESTNET_API_KEY and TBOT_BINANCE_TESTNET_API_SECRET"
        return "TBOT_BINANCE_API_KEY and TBOT_BINANCE_API_SECRET"

    @field_validator("*", mode="before")
    @classmethod
    def strip(cls, value: Any) -> Any:
        """A quoted value keeps its spaces, and a key with one fails in an HTTP header
        whose error text would carry the key into logs."""
        return value.strip() if isinstance(value, str) else value


def save_settings(path: Path, values: dict[str, str]) -> None:
    """Set KEY=value lines in a .env file and keep everything else.

    A missing file starts from .env.example next to it. The file is replaced in one step.
    """
    template = path.with_name(".env.example")
    source = path if path.exists() else template if template.exists() else None
    text = source.read_bytes().decode("utf-8-sig") if source else ""  # as is: CRLF stays
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.splitlines()
    for key, value in values.items():
        line = f"{key}={value}"
        found = [i for i, old in enumerate(lines) if old.split("=", 1)[0].strip() == key]
        for i in found:  # every copy: the last one is the one that counts
            lines[i] = line
        if not found:
            lines.append(line)
    partial = path.with_name(path.name + ".partial")
    try:
        partial.write_bytes((newline.join(lines) + newline).encode("utf-8"))
        replace_keeping_access(partial, path)
    except OSError as exc:  # the secret must not stay behind in a file anyone may read
        partial.unlink(missing_ok=True)
        hint = "; if it is read-only, clear that in its Properties" if path.exists() else ""
        raise EnvUnreadable(f"cannot save {path} ({exc.strerror}){hint}") from None


def replace_keeping_access(source: Path, target: Path) -> None:
    """Put source in place of target in one step. Target's access rights stay (an
    owner-only .env stays so); a new file is readable by its owner only."""
    if sys.platform == "win32":
        import ctypes

        if target.exists():
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            if kernel32.ReplaceFileW(str(target), str(source), None, 0, None, None):
                return  # keeps the target's security settings
            source.replace(target)
            return
        source.replace(target)
        user = os.environ.get("USERNAME", "")
        if user:  # inherited rights of the folder: often every account on the PC
            domain = os.environ.get("USERDOMAIN", "")
            owner = f"{domain}\\{user}" if domain else user
            with contextlib.suppress(OSError):
                subprocess.run(
                    ["icacls", str(target), "/inheritance:r", "/grant:r", f"{owner}:F"],
                    capture_output=True,
                    check=False,
                )
        return
    if target.exists():
        shutil.copymode(target, source)
    else:
        os.chmod(source, 0o600)
    source.replace(target)


BROAD_SIDS = {"S-1-1-0": "Everyone", "S-1-5-11": "Authenticated Users", "S-1-5-32-545": "Users"}


def env_readers(path: Path) -> list[str]:
    """Groups of other accounts that can read this file, or [] (also when unknown)."""
    if sys.platform != "win32":
        return ["other accounts"] if path.stat().st_mode & 0o077 else []
    literal = str(path.resolve()).replace("'", "''")
    script = (
        f"(Get-Acl -LiteralPath '{literal}').Access | "
        "Where-Object { $_.AccessControlType -eq 'Allow' } | ForEach-Object { try { "
        "$_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value "
        "} catch { } }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted({BROAD_SIDS[sid] for sid in result.stdout.split() if sid in BROAD_SIDS})
