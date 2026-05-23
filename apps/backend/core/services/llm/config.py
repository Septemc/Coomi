"""模型配置管理 - ~/.coomi/config/providers.json"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ProviderConfig:
    """单个 Provider 配置"""
    id: str
    type: str  # deepseek / openai / anthropic / generic
    display: str
    api_key: str
    model: str
    base_url: str = ""
    fast_model: str | None = None

    @classmethod
    def from_dict(cls, provider_id: str, data: dict) -> ProviderConfig:
        return cls(
            id=provider_id,
            type=data.get("type", "generic"),
            display=data.get("display", provider_id),
            api_key=data.get("api_key", ""),
            model=data.get("model", ""),
            base_url=data.get("base_url", ""),
            fast_model=data.get("fast_model"),
        )

    def to_dict(self) -> dict:
        d: dict = {
            "type": self.type,
            "display": self.display,
            "api_key": self.api_key,
            "model": self.model,
        }
        if self.base_url:
            d["base_url"] = self.base_url
        if self.fast_model:
            d["fast_model"] = self.fast_model
        return d


class ConfigManager:
    """统一模型配置管理器

    配置文件路径: ~/.coomi/config/providers.json
    首次启动自动从 .env 迁移。
    """

    def __init__(self):
        self.config_dir = Path.home() / ".coomi" / "config"
        self.config_path = self.config_dir / "providers.json"
        self.data = self._load()

    def _load(self) -> dict:
        """加载配置，不存在则自动创建"""
        if self.config_path.exists():
            try:
                return json.loads(self.config_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                # 损坏时备份后再迁移，避免静默覆盖用户手动编辑的内容
                import shutil
                bak_path = self.config_path.with_suffix(".json.bak")
                shutil.move(str(self.config_path), str(bak_path))

        # 不存在或损坏 → 从 .env 迁移或创建空模板
        return self._migrate_or_create()

    def _migrate_or_create(self) -> dict:
        """从 .env 迁移或创建空配置"""
        from dotenv import load_dotenv

        load_dotenv(Path.cwd() / ".env")  # 不加 override，避免覆盖系统环境变量
        provider_type = os.getenv("LLM_PROVIDER", "deepseek").lower()

        if provider_type == "deepseek":
            data = {
                "version": 1,
                "active": "default",
                "providers": {
                    "default": {
                        "type": "deepseek",
                        "display": os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro"),
                        "api_key": os.getenv("DEEPSEEK_API_KEY", ""),
                        "base_url": os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
                        "model": os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro"),
                        "fast_model": "deepseek-v4-flash" if "flash" not in os.getenv("DEEPSEEK_MODEL", "").lower() else None,
                    }
                },
            }
        elif provider_type == "openai":
            data = {
                "version": 1,
                "active": "default",
                "providers": {
                    "default": {
                        "type": "openai",
                        "display": os.getenv("OPENAI_MODEL", "gpt-4o"),
                        "api_key": os.getenv("OPENAI_API_KEY", ""),
                        "model": os.getenv("OPENAI_MODEL", "gpt-4o"),
                    }
                },
            }
        elif provider_type == "anthropic":
            data = {
                "version": 1,
                "active": "default",
                "providers": {
                    "default": {
                        "type": "anthropic",
                        "display": "Claude Sonnet 4",
                        "api_key": os.getenv("ANTHROPIC_API_KEY", ""),
                        "model": os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4-20250514"),
                        "fast_model": "claude-haiku-4-5-20251001",
                    }
                },
            }
        else:
            data = {"version": 1, "active": "", "providers": {}}

        self.save(data)
        return data

    def save(self, data: dict | None = None) -> None:
        """持久化到磁盘"""
        if data is not None:
            self.data = data
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    # ---- Provider 操作 ----

    def list_providers(self) -> list[ProviderConfig]:
        """列出所有已配置的提供方"""
        providers = self.data.get("providers", {})
        return [
            ProviderConfig.from_dict(pid, pdata)
            for pid, pdata in providers.items()
        ]

    def get_active(self) -> ProviderConfig | None:
        """获取当前激活的 Provider"""
        active_id = self.data.get("active", "")
        return self.get_provider(active_id)

    def set_active(self, provider_id: str) -> bool:
        """设置激活的 Provider"""
        if provider_id not in self.data.get("providers", {}):
            return False
        self.data["active"] = provider_id
        self.save()
        return True

    def get_provider(self, provider_id: str) -> ProviderConfig | None:
        """获取指定 Provider"""
        providers = self.data.get("providers", {})
        if provider_id in providers:
            return ProviderConfig.from_dict(provider_id, providers[provider_id])
        return None

    def add_provider(self, config: ProviderConfig) -> None:
        """新增 Provider"""
        if "providers" not in self.data:
            self.data["providers"] = {}
        self.data["providers"][config.id] = config.to_dict()
        self.save()

    def remove_provider(self, provider_id: str) -> bool:
        """删除 Provider"""
        if provider_id not in self.data.get("providers", {}):
            return False
        del self.data["providers"][provider_id]
        if self.data["active"] == provider_id:
            self.data["active"] = next(iter(self.data["providers"]), "")
        self.save()
        return True

    def get_config_path_str(self) -> str:
        """获取配置文件路径（用于提示用户编辑）"""
        return str(self.config_path)
