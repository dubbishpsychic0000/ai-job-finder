"""Tavily API key rotation manager to distribute load across multiple keys."""

import os
from pathlib import Path
from typing import Optional


def _load_env_var(var_name: str) -> str:
    """Load env var from os.getenv or from .env file as fallback."""
    value = os.getenv(var_name, '')
    if value:
        return value
    
    # Fallback: try to load from .env file
    env_file = Path('.env')
    if env_file.exists():
        try:
            for line in env_file.read_text().split('\n'):
                line = line.strip()
                if line.startswith(f'{var_name}='):
                    return line.split('=', 1)[1]
        except Exception:
            pass
    
    return ''


class TavilyKeyRotator:
    """Manages rotation of Tavily API keys to handle rate limits."""

    def __init__(self):
        """Initialize with keys from environment or .env file."""
        self.keys_str = _load_env_var('TAVILY_API_KEYS')
        self.keys = [k.strip() for k in self.keys_str.split(',') if k.strip()]
        self.index_file = Path('.tavily_key_index')
        self.current_index = self._load_index()

    def _load_index(self) -> int:
        """Load the last used key index from disk."""
        if self.index_file.exists():
            try:
                return int(self.index_file.read_text().strip())
            except (ValueError, OSError):
                pass
        return 0

    def _save_index(self, index: int) -> None:
        """Save the current key index to disk."""
        try:
            self.index_file.write_text(str(index))
        except OSError as e:
            print(f"Warning: Could not save key index: {e}")

    def get_key(self) -> Optional[str]:
        """Get the next key in rotation."""
        if not self.keys:
            return None
        
        key = self.keys[self.current_index]
        self.current_index = (self.current_index + 1) % len(self.keys)
        self._save_index(self.current_index)
        return key

    def get_current_key(self) -> Optional[str]:
        """Get the current key without rotating."""
        return self.keys[self.current_index] if self.keys else None

    def get_all_keys(self) -> list:
        """Get all configured keys."""
        return self.keys.copy()

    def key_count(self) -> int:
        """Get the number of configured keys."""
        return len(self.keys)


# Singleton instance
_rotator: Optional[TavilyKeyRotator] = None


def get_tavily_key() -> Optional[str]:
    """Get the next Tavily API key with rotation."""
    global _rotator
    if _rotator is None:
        _rotator = TavilyKeyRotator()
    return _rotator.get_key()


def get_tavily_rotator() -> TavilyKeyRotator:
    """Get the TavilyKeyRotator instance."""
    global _rotator
    if _rotator is None:
        _rotator = TavilyKeyRotator()
    return _rotator
