"""Platform-specific config and data directory paths."""

import os
import sys
from pathlib import Path


class ConfigPaths:
    """Resolves platform-specific paths for config, data, and cache."""

    APP_NAME = "openclose"

    @classmethod
    def config_dir(cls) -> Path:
        """User config directory (~/.config/openclose or platform equivalent)."""
        if sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support"
        elif sys.platform == "win32":
            base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        else:
            base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        return base / cls.APP_NAME

    @classmethod
    def data_dir(cls) -> Path:
        """User data directory (~/.local/share/openclose or platform equivalent)."""
        if sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support"
        elif sys.platform == "win32":
            base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        else:
            base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        return base / cls.APP_NAME

    @classmethod
    def cache_dir(cls) -> Path:
        """Cache directory."""
        if sys.platform == "darwin":
            base = Path.home() / "Library" / "Caches"
        elif sys.platform == "win32":
            base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        else:
            base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
        return base / cls.APP_NAME

    @classmethod
    def db_path(cls) -> Path:
        """SQLite database file path."""
        return cls.data_dir() / "openclose.db"

    @classmethod
    def user_config_path(cls) -> Path:
        """User-level config file."""
        return cls.config_dir() / "config.toml"

    @classmethod
    def project_config_path(cls, project_dir: Path) -> Path:
        """Project-level config file."""
        return project_dir / ".openclose" / "config.toml"

    @classmethod
    def project_runtime_dir(cls, project_dir: str | Path) -> Path:
        """Per-project runtime dir under user config, outside the project tree."""
        resolved = Path(project_dir).resolve()
        return cls.config_dir() / resolved.name

    @classmethod
    def ensure_dirs(cls) -> None:
        """Create all necessary directories if they don't exist."""
        cls.config_dir().mkdir(parents=True, exist_ok=True)
        cls.data_dir().mkdir(parents=True, exist_ok=True)
        cls.cache_dir().mkdir(parents=True, exist_ok=True)
