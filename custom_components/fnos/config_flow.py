"""飞牛 fnOS 集成配置流程。"""
from __future__ import annotations

import logging

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .api import ERR_BAD_CREDENTIAL, FnOsApiError, FnOsClient
from .const import (
    CONF_UPDATE_INTERVAL,
    CONF_WEB_BASE_URL,
    CONF_SSL,
    DEFAULT_PORT,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
    MAX_UPDATE_INTERVAL,
    MIN_UPDATE_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
        vol.Required(CONF_SSL, default=False): bool,
        vol.Required(CONF_USERNAME, default="admin"): str,
        vol.Required(CONF_PASSWORD): str,
    }
)

OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_UPDATE_INTERVAL, default=DEFAULT_UPDATE_INTERVAL):
            vol.All(vol.Coerce(int), vol.Range(min=MIN_UPDATE_INTERVAL,
                                               max=MAX_UPDATE_INTERVAL)),
        vol.Optional(CONF_WEB_BASE_URL, default=""): str,
    }
)


def _validate(hass, data):
    """登录 + 拉一轮快照验证配置（executor 里跑同步客户端）。"""

    def _try():
        client = FnOsClient(
            host=data[CONF_HOST], port=data[CONF_PORT],
            username=data[CONF_USERNAME], password=data[CONF_PASSWORD],
            ssl=data.get(CONF_SSL, False),
        )
        try:
            client.must("appcgi.sysinfo.getHostName")
            return True
        finally:
            client.close()

    return hass.async_add_executor_job(_try)


class FnOsConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """处理配置流程。"""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            await self.async_set_unique_id(
                f"{user_input[CONF_HOST]}:{user_input[CONF_PORT]}")
            self._abort_if_unique_id_configured()
            try:
                await _validate(self.hass, user_input)
            except FnOsApiError as err:
                if err.errno == ERR_BAD_CREDENTIAL:
                    errors["base"] = "invalid_auth"
                else:
                    errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("fnOS 连接校验失败")
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=f"飞牛 fnOS · {user_input[CONF_HOST]}",
                    data=user_input,
                )
        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return FnOsOptionsFlow(config_entry)


class FnOsOptionsFlow(config_entries.OptionsFlow):
    """选项：数据刷新间隔 / 显式访问基地址。"""

    def __init__(self, config_entry):
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_UPDATE_INTERVAL,
                    default=self.config_entry.options.get(
                        CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL),
                ): vol.All(vol.Coerce(int),
                           vol.Range(min=MIN_UPDATE_INTERVAL,
                                     max=MAX_UPDATE_INTERVAL)),
                vol.Optional(
                    CONF_WEB_BASE_URL,
                    default=self.config_entry.options.get(
                        CONF_WEB_BASE_URL, ""),
                ): str,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
