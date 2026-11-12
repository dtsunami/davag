"""User interface layer: shared slash commands, TUI REPL, and Web GUI sidecar."""

from .commands import HELP_TEXT, command_list, handle_command

__all__ = ["HELP_TEXT", "command_list", "handle_command"]