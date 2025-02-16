import time
import jwt
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger
from app.schemas import NotificationType
from app.utils.http import RequestUtils


class StationCall(_PluginBase):
    # 插件名称
    plugin_name = "站点喊话"
    # 插件描述
    plugin_desc = "特定站点喊话领取奖励。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/KoWming/MoviePilot-Plugins/main/icons/Lucky_B.png"
    # 插件版本
    plugin_version = "0.5.6"
    # 插件作者
    plugin_author = "KoWming"
    # 作者主页
    author_url = "https://github.com/KoWming"
    # 插件配置项ID前缀
    plugin_config_prefix = "stationcall_"
    # 加载顺序
    plugin_order = 15
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _cron = None
    _notify = False
    _sites = None

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()

        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._notify = config.get("notify")
            self._sites = config.get("sites")

            # 加载模块
        if self._enabled:
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info(f"站点喊话服务启动，定时任务设置为 {self._cron}")
            self._scheduler.add_job(func=self.__execute_requests, trigger=CronTrigger.from_crontab(self._cron),
                                    name="站点喊话")
            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def get_jwt(self) -> str:
        # 减少接口请求直接使用jwt
        payload = {
            "exp": int(time.time()) + 28 * 24 * 60 * 60,
            "iat": int(time.time())
        }
        encoded_jwt = jwt.encode(payload, self._openToken, algorithm="HS256")
        logger.debug(f"LuckyHelper get jwt---》{encoded_jwt}")
        return "Bearer "+encoded_jwt

    def __execute_requests(self):
        """
        执行喊话请求的主函数
        """
        message = ''

        if not self._sites:
            logger.info("没有配置站点信息，跳过发送消息")
            return

        for site_name, site_info in self._sites.items():
            url = site_info.get("url")
            cookie = site_info.get("cookie")
            messages = site_info.get("messages", [])

            for msg in messages:
                response = self.fetch_with_delay(url, self.create_params(msg), cookie)
                if response.status_code == 200:
                    message += f"{site_name}box: {msg} 成功\n\n"
                    logger.info(f"消息发送成功: {msg} 到 {url}")
                else:
                    message += f"{site_name}box: {msg} 失败\n\n"
                    logger.error(f"消息发送失败: {msg} 到 {url}, 状态码: {response.status_code}")

        # 发送通知
        if self._notify:
            self.post_message(
                mtype=NotificationType.SiteMessage,
                title="【站点喊话完成】:",
                text=message
            )

    def fetch_with_delay(self, url: str, params: str, cookie: str):
        """
        延迟发送请求
        """
        time.sleep(1)  # 延时 1 秒
        headers = {
            'Content-Type': 'application/x-www-form-urlencoded',
            'Cookie': cookie
        }
        return RequestUtils(headers=headers).get_res(url + '?' + params)

    def create_params(self, text: str) -> str:
        """
        创建请求参数
        """
        return "shbox_text=" + text + "&shout=我喊&sent=yes&type=shoutbox"

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        [({
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron/interval/date/CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数
        })]
        """
        if self._enabled and self._cron:
            return [{
                "id": "StationCall",
                "name": "站点喊话定时服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.__execute_requests,
                "kwargs": {}
            }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 4
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '开启通知',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'sites',
                                            'label': '站点信息',
                                            'rows': 10,
                                            'placeholder': '配置站点信息，格式为JSON数组，例如：[{"name": "qingwa", "url": "https://www.***.com/shoutbox.php", "cookie": "", "messages": ["蛙总 求上传", "蛙总 求下载"]}]'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 12
                                },
                                'content': [
                                    {
                                        'component': 'VCronField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '0 8 * * *',
                                            'hint': '输入5位cron表达式，默认每天8点运行。',
                                            'persistent-hint': True
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '站点列表: \n qingwa: \'https://www.***.com/shoutbox.php\'     \n'
                                                    '站点Cookie: \n qingwa: \'cookie\'     \n'
                                                    '多行使用,号连接。\n'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                ]
            },
        ], {
            "enabled": False,
            "notify": False,
            "cron": "0 8 * * *",
            "sites": []
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))