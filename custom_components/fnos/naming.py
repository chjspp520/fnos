"""实体 ID 命名：统一 ``<domain>.fnos_<短英文>``（不使用汉语拼音）。

为什么需要它
------------
HA 默认按实体**显示名**生成 entity_id，本集成的显示名是中文，于是 ID 全变成
拼音（``sensor.fei_niu_zhu_ji_nucfnos_cpu_shi_yong_lu``）。这里改成：

    <domain>.fnos_[<条目>_]<设备>_<用途>

* 设备段：主机为空（``sensor.fnos_cpu``）、``file`` / ``photo`` / ``video``、
  ``docker_<容器名>``、``vm_<虚拟机标题>``；
* 条目段只在配了多个 fnOS 条目时出现，避免两条配置抢同一个 ID；
* 显示名（friendly_name）保持中文不变，只有 entity_id 变化。

实体注册表优先于代码里设置的 entity_id：首次创建直接生效；旧版本已经注册过的
拼音 ID 由 :meth:`ForcedEntityIdMixin.async_migrate_entity_id` 在实体加入后
改名（注册表改名，唯一 ID / 历史 / 统计都保留）。
"""
from __future__ import annotations

import hashlib
import logging
import re

from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er

from .const import (
    DOMAIN,
    KIND_DOCKER,
    KIND_FILE,
    KIND_HOST,
    KIND_PHOTO,
    KIND_VIDEO,
    KIND_VM,
)

_LOGGER = logging.getLogger(__name__)

ENTITY_ID_PREFIX = f"{DOMAIN}_"

#: 设备 kind → 实体 ID 里的英文段（主机不占段，保持最短）
_DEVICE_TOKENS = {
    KIND_HOST: "",
    KIND_FILE: "file",
    KIND_PHOTO: "photo",
    KIND_VIDEO: "video",
    KIND_DOCKER: "docker",
    KIND_VM: "vm",
}

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_MAX_SLUG = 28


def entity_id_slug(text, max_len: int = _MAX_SLUG) -> str:
    """ASCII 小写下划线 slug；中文等非 ASCII 字符会被丢弃。"""
    value = _SLUG_RE.sub("_", str(text or "").lower()).strip("_")
    if len(value) > max_len:
        value = value[:max_len].rstrip("_")
    return value


def short_hash(text, length: int = 6) -> str:
    """名称全是非 ASCII（如中文容器名）时的稳定兜底段。"""
    return hashlib.sha1(str(text or "").encode("utf-8")).hexdigest()[:length]


def object_id(*parts) -> str:
    """把若干段拼成 ``a_b_c`` 形式，自动跳过空段。"""
    tokens = [entity_id_slug(part) for part in parts]
    return "_".join(token for token in tokens if token)


def device_token(kind: str, key: str = "", title: str = "") -> str:
    """设备在实体 ID 里的英文段。

    ``host`` → 空串（``sensor.fnos_cpu``）；
    ``docker:nginx`` → ``docker_nginx``；
    ``vm:tf2vf3bx``（标题 Windows 7） → ``vm_windows_7``。
    """
    base = _DEVICE_TOKENS.get(kind, entity_id_slug(kind))
    sub = ""
    if kind == KIND_VM and title:
        sub = entity_id_slug(title)
    if not sub:
        sub = entity_id_slug(key)
    if not sub and key:
        sub = short_hash(key)
    return "_".join(part for part in (base, sub) if part)


def entry_token(hass, entry) -> str:
    """多配置条目时的区分段；单条目返回空串（ID 保持最短）。"""
    try:
        entries = hass.config_entries.async_entries(DOMAIN)
    except Exception:  # pylint: disable=broad-except
        return ""
    if len(entries) <= 1:
        return ""
    return (entity_id_slug(entry.data.get("host"))
            or short_hash(entry.entry_id, 4))


class ForcedEntityIdMixin:
    """把实体 ID 固定成 ``<domain>.fnos_<短英文>``（显示名不动）。"""

    _forced_entity_id: str = ""

    def _setup_forced_entity_id(self, platform_domain: str, *parts) -> None:
        """设置固定实体 ID（首次创建生效；旧 ID 靠迁移改名）。"""
        if not platform_domain:
            return
        suffix = object_id(*parts)
        if not suffix:
            return
        self._forced_entity_id = f"{platform_domain}.{ENTITY_ID_PREFIX}{suffix}"
        self.entity_id = self._forced_entity_id

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.async_migrate_entity_id()

    @callback
    def async_migrate_entity_id(self) -> None:
        """把注册表里的旧 ID（拼音）迁移到固定 ID，幂等。

        目标 ID 被别的实体占用时退到 ``<目标>_2``、``_3`` …，绝不抢别人的号。
        """
        target = getattr(self, "_forced_entity_id", "")
        current = self.entity_id
        hass = getattr(self, "hass", None)
        if not target or not current or current == target or hass is None:
            return
        registry = er.async_get(hass)
        occupant = registry.async_get(target)
        if occupant is None:
            new_id = target
        elif current.startswith(f"{target}_"):
            # 已经是 <目标>_N 的兜底形态，别来回改名
            return
        else:
            new_id = ""
            for index in range(2, 100):
                candidate = f"{target}_{index}"
                if registry.async_get(candidate) is None:
                    new_id = candidate
                    break
            if not new_id:
                _LOGGER.warning(
                    "fnOS 实体 %s 的目标 ID %s 已被占用且无可用兜底，保留原样",
                    current, target)
                return
            _LOGGER.info("fnOS 实体 %s 的目标 ID 被 %s 占用，暂用 %s",
                         current, occupant.entity_id, new_id)
        try:
            registry.async_update_entity(current, new_entity_id=new_id)
        except (ValueError, KeyError) as err:
            _LOGGER.warning("fnOS 实体 %s 改名为 %s 失败：%s",
                            current, new_id, err)
            return
        # 显式回写：同一生命周期内的后续代码（聚合快照排除自身等）要拿到新 ID
        self.entity_id = new_id
        _LOGGER.info("fnOS 实体 ID 已迁移：%s → %s", current, new_id)
