"""飞牛 fnOS 集成常量。"""
DOMAIN = "fnos"

PLATFORMS = [
    "sensor",
    "binary_sensor",
    "switch",
    "button",
]

CONF_SSL = "ssl"
CONF_UPDATE_INTERVAL = "update_interval"
CONF_WEB_BASE_URL = "web_base_url"

DEFAULT_PORT = 5666
DEFAULT_UPDATE_INTERVAL = 30
MIN_UPDATE_INTERVAL = 10
MAX_UPDATE_INTERVAL = 3600
DEFAULT_NAME = "飞牛 fnOS"

# 悬浮窗 / 远程地址：HA 侧签名页面路径（每个设备一个 target）
WEB_PAGE_PREFIX = "/fnos/web"
# fnOS Web 同源反向代理（HTTP + WS），path 前缀 = /fnos/proxy/<entry_id>/
PROXY_PREFIX = "/fnos/proxy"
# 前端心跳：上报浏览器地址栏 origin（设备链接自动跟随）
ORIGIN_PREFIX = "/fnos/api/origin"
# 注入 HA 前端的悬浮窗模块
EMBED_JS_URL = "/fnos/proxy_static/fnos-embed.js"

# 存储学习到的访问基地址（重启不丢）
BASE_STORE_KEY = f"{DOMAIN}.web_base_urls"
BASE_STORE_VERSION = 1

# 每台设备的访问 target（悬浮窗窗口类型 / 配置写入 configuration_url 的参数）
# host → HA 生成的应用启动器（系统设置 / 应用市场 / 各应用入口）
TARGET_HOST = "host"
TARGET_FILE = "file"
TARGET_PHOTO = "photo"
TARGET_VIDEO = "video"
TARGET_SETTING = "setting"
TARGET_DOCKER = "docker"
TARGET_VM = "vm"

TARGET_TITLES = {
    TARGET_HOST: "系统总览",
    TARGET_FILE: "文件管理",
    TARGET_PHOTO: "飞牛相册",
    TARGET_VIDEO: "飞牛影视",
    TARGET_SETTING: "系统设置",
    TARGET_DOCKER: "Docker 管理",
    TARGET_VM: "虚拟机",
}

# 设备 identifier 约定：(DOMAIN, f"{entry_id}:{kind}")，kind ∈：
#   host / file / photo / video / docker:<name> / vm:<name>
# 注：不再有独立的「设置」设备——系统设置相关实体全部挂在主机设备上。
KIND_HOST = "host"
KIND_FILE = "file"
KIND_PHOTO = "photo"
KIND_VIDEO = "video"
KIND_SETTING = "setting"  # 仅用于启动时清理历史遗留设备
KIND_DOCKER = "docker"
KIND_VM = "vm"

STATIC_KINDS = (KIND_HOST, KIND_FILE, KIND_PHOTO, KIND_VIDEO)

# target（悬浮窗打开的页面）与设备 kind 的映射
KIND_TO_TARGET = {
    KIND_HOST: TARGET_HOST,
    KIND_FILE: TARGET_FILE,
    KIND_PHOTO: TARGET_PHOTO,
    KIND_VIDEO: TARGET_VIDEO,
    KIND_DOCKER: TARGET_DOCKER,
    KIND_VM: TARGET_VM,
}

# 文件共享服务开关：snapshot 键 -> (中文名, fnOS opt/set 前缀)
SERVICE_SWITCHES = {
    "ssh": ("SSH", "ssh"),
    "smb": ("SMB", "smb"),
    "nfs": ("NFS", "nfs"),
    "ftp": ("FTP", "ftp"),
    "webdav": ("WebDAV", "webdav"),
    "dlna": ("DLNA", "dlna"),
}

DATA_KEY = f"{DOMAIN}_data"
TASK_KEY = f"{DOMAIN}_tasks"
