# -*- coding: utf-8 -*-
"""数据模型：游戏实例、存档、本地同步状态"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from core.protocol import STATE_DIR, STATE_FILE, human_size, normalize_save_name


@dataclass
class Instance:
    name: str
    root: str                 # 实例根目录（外层目录）
    game_dir: str             # .minecraft 实际目录
    source: str = "未知"       # 启动器来源
    game_version: str = ""    # 主要游戏版本（推测）
    modded: bool = False

    @property
    def saves_dir(self) -> str:
        return os.path.join(self.game_dir, "saves")

    @property
    def config_dir(self) -> str:
        """整合包配置目录。

        里面有服务端/模组配置，多人玩同一个整合包时只有它也一起同步，
        各人的模组设置才不会跑偏。客户端自身配置不属于实例，不在这里。
        """
        return os.path.join(self.game_dir, "config")

    @property
    def key(self) -> str:
        return os.path.normcase(os.path.abspath(self.root))

    def to_dict(self) -> dict:
        return {
            "name": self.name, "root": self.root, "game_dir": self.game_dir,
            "source": self.source, "game_version": self.game_version, "modded": self.modded,
        }

    @staticmethod
    def from_dict(d: dict) -> "Instance":
        return Instance(
            name=d.get("name", ""), root=d.get("root", ""), game_dir=d.get("game_dir", ""),
            source=d.get("source", "未知"), game_version=d.get("game_version", ""),
            modded=bool(d.get("modded", False)),
        )


@dataclass
class SaveInfo:
    name: str                       # 文件夹名
    path: str
    world_name: str = ""
    game_version: str = ""
    gametype: str = ""
    difficulty: str = ""
    hardcore: bool = False
    last_played: float = 0
    seed: Optional[int] = None
    size: int = -1                  # -1 = 未统计
    file_count: int = -1
    icon: str = ""
    state: Optional["SaveState"] = None
    instance: Optional[Instance] = None

    @property
    def display_name(self) -> str:
        return self.world_name or self.name

    @property
    def cloud_name(self) -> str:
        return normalize_save_name(self.name)

    @property
    def size_text(self) -> str:
        return "计算中…" if self.size < 0 else human_size(self.size)

    @property
    def last_played_text(self) -> str:
        if not self.last_played:
            return "未知"
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(self.last_played))

    @property
    def synced(self) -> bool:
        return bool(self.state and self.state.last_version)

    def badge(self) -> str:
        if self.synced:
            return f"☁ v{self.state.last_version}"
        return "本地"


class SaveState:
    """存档本地同步状态，写入 <存档>/.mcsync/state.json（默认被同步忽略）"""

    def __init__(self, save_path: str, data: Optional[dict] = None):
        self.save_path = save_path
        self.data: Dict[str, Any] = data or {}
        self.data.setdefault("version", 2)
        self.data.setdefault("server", "")
        self.data.setdefault("cloud_name", "")
        self.data.setdefault("last_version", 0)
        self.data.setdefault("last_sync", 0)
        self.data.setdefault("files", {})

    # ------------------------------------------------------------ IO
    @property
    def state_dir(self) -> str:
        return os.path.join(self.save_path, STATE_DIR)

    @property
    def state_path(self) -> str:
        return os.path.join(self.state_dir, STATE_FILE)

    @classmethod
    def load(cls, save_path: str) -> "SaveState":
        p = os.path.join(save_path, STATE_DIR, STATE_FILE)
        data = {}
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        return cls(save_path, data)

    def save(self) -> None:
        try:
            os.makedirs(self.state_dir, exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
            os.replace(tmp, self.state_path)
        except Exception:
            pass

    # ------------------------------------------------------------ 字段
    @property
    def last_version(self) -> int:
        return int(self.data.get("last_version", 0) or 0)

    @property
    def cloud_name(self) -> str:
        return str(self.data.get("cloud_name", "") or "")

    @property
    def files(self) -> Dict[str, dict]:
        return self.data.get("files", {}) or {}

    def update(self, files: Dict[str, dict], version: int, server: str, cloud_name: str) -> None:
        self.data["files"] = files
        self.data["last_version"] = int(version)
        self.data["last_sync"] = time.time()
        self.data["server"] = server
        self.data["cloud_name"] = cloud_name
        self.save()

    def to_meta_list(self) -> List[dict]:
        return [
            {"path": p, "size": v.get("size", 0), "mtime": v.get("mtime", 0), "hash": v.get("hash", "")}
            for p, v in self.files.items()
        ]
