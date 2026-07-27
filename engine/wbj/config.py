"""Configuration loader for wbj compute engine."""

import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from dotenv import dotenv_values

# Servicio bajo el que viven los secretos en el Llavero de macOS. Una entrada por
# variable: el "account" es el nombre de la variable (p.ej. RH_PASSWORD).
KEYCHAIN_SERVICE = "warren-buffett-jr"


def _keychain_get(name: str) -> str | None:
    """Lee un secreto del Llavero de macOS, o None si no está.

    Nunca lanza: si no hay `security` (no es macOS), si la entrada no existe o si
    el Llavero está bloqueado, se devuelve None y el llamador cae al .env.
    """
    security = shutil.which("security")
    if not security:
        return None
    try:
        out = subprocess.run(
            [security, "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", name, "-w"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _secret(name: str, env_vars: dict) -> str | None:
    """Resuelve un secreto: primero el .env, y si no está, el Llavero.

    Este orden es deliberado: mantiene el .env como fuente ganadora mientras exista
    la línea, así que la migración es "guardar en el Llavero y BORRAR la línea".
    Mientras la línea siga ahí, el Llavero no se consulta — si la dejas, no estás
    migrado, aunque la entrada del Llavero exista.
    """
    return (env_vars.get(name) or None) or _keychain_get(name)


def _find_repo_root() -> Path:
    """Derive repo_root: two parents up from wbj/ directory."""
    # wbj package is at engine/wbj/
    wbj_dir = Path(__file__).parent  # engine/wbj/
    engine_dir = wbj_dir.parent  # engine/
    repo_root = engine_dir.parent  # repo_root
    return repo_root


@dataclass(repr=False)
class Settings:
    """Warren Buffett Jr settings, never repr keys."""

    fmp_api_key: str | None = None
    finnhub_api_key: str | None = None
    fred_api_key: str | None = None
    anthropic_api_key: str | None = None
    # Model for the qualitative judgment agent; override to cut cost (e.g.
    # "claude-haiku-4-5"). Default per the Anthropic SDK guidance.
    judge_model: str = "claude-opus-4-8"
    # Robinhood order entry (API no oficial, robin_stocks). Credenciales que TÚ
    # pones en API/.env; el motor solo las lee. Sin ellas el proveedor queda
    # deshabilitado. El MFA es el secreto TOTP de tu 2FA por app (base32), del
    # que robin_stocks genera el código en cada login. Ver providers/robinhood.py.
    rh_username: str | None = None
    rh_password: str | None = None
    rh_mfa_totp: str | None = None
    repo_root: Path = field(default_factory=_find_repo_root)
    cache_dir: Path = field(default_factory=lambda: _find_repo_root() / "engine" / "cache")
    reports_dir: Path = field(default_factory=lambda: _find_repo_root() / "Reportes")

    def __repr__(self) -> str:
        """Custom repr that never includes secret keys."""
        return (
            f"Settings(fmp_api_key={'*' * 8 if self.fmp_api_key else None}, "
            f"finnhub_api_key={'*' * 8 if self.finnhub_api_key else None}, "
            f"fred_api_key={'*' * 8 if self.fred_api_key else None}, "
            f"repo_root={self.repo_root}, "
            f"cache_dir={self.cache_dir}, "
            f"reports_dir={self.reports_dir})"
        )


def load_settings(env_file: Path | None = None) -> Settings:
    """Load settings from env file, with defaults.

    Args:
        env_file: Path to .env file. Defaults to <repo_root>/API/.env.

    Returns:
        Settings instance with keys from env file (or None if missing/empty).
    """
    repo_root = _find_repo_root()

    if env_file is None:
        env_file = repo_root / "API" / ".env"

    # Load env file if it exists; otherwise return defaults
    env_vars = {}
    if env_file.exists():
        env_vars = dotenv_values(env_file)

    # Map empty strings to None
    fmp_api_key = env_vars.get("FMP_API_KEY") or None
    finnhub_api_key = env_vars.get("FINNHUB_API_KEY") or None
    fred_api_key = env_vars.get("FRED_API_KEY") or None
    # ANTHROPIC_API_KEY may also come from the real environment (SDK convention).
    import os

    anthropic_api_key = (
        env_vars.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY") or None
    )
    judge_model = env_vars.get("JUDGE_MODEL") or "claude-opus-4-8"
    rh_username = env_vars.get("RH_USERNAME") or None
    # Contraseña y semilla TOTP: pueden vivir en el Llavero en vez del .env.
    rh_password = _secret("RH_PASSWORD", env_vars)
    rh_mfa_totp = _secret("RH_MFA_TOTP", env_vars)

    return Settings(
        fmp_api_key=fmp_api_key,
        finnhub_api_key=finnhub_api_key,
        fred_api_key=fred_api_key,
        anthropic_api_key=anthropic_api_key,
        judge_model=judge_model,
        rh_username=rh_username,
        rh_password=rh_password,
        rh_mfa_totp=rh_mfa_totp,
        repo_root=repo_root,
        cache_dir=repo_root / "engine" / "cache",
        reports_dir=repo_root / "Reportes",
    )
