"""Configuration, .env profile management, model registry, and Resolve path discovery."""

from .env import Config, key_var_for, load_config, resolve_davai_home, resolve_home
from .envfile import edit_env_file, env_file_path, parse_dotenv, read_env, set_env_vars
from .models import MODELS, ModelSpec, resolve_model
from .profiles import block_state, block_values, enable_block, select_block_var, set_block_var
from .resolve_paths import ResolveInstallation, discover_resolve
from .settings import load_settings, save_settings, settings_to_defaults

__all__ = [
    "MODELS",
    "Config",
    "ModelSpec",
    "ResolveInstallation",
    "block_state",
    "block_values",
    "discover_resolve",
    "edit_env_file",
    "enable_block",
    "env_file_path",
    "key_var_for",
    "load_config",
    "load_settings",
    "parse_dotenv",
    "read_env",
    "resolve_davai_home",
    "resolve_home",
    "resolve_model",
    "save_settings",
    "select_block_var",
    "set_block_var",
    "set_env_vars",
    "settings_to_defaults",
]