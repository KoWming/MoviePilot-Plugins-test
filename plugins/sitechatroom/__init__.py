import time
import os
import requests
from datetime import datetime, timedelta
from typing import Any, List, Dict, Tuple, Optional

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from requests import Response
from app.chain.site import SiteChain
from app.core.config import settings
from app.core.event import EventManager, eventmanager
from app.db.site_oper import SiteOper
from app.helper.module import ModuleHelper
from app.helper.sites import SitesHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType
from app.utils.timer import TimerUtils


class SiteChatRoom(_PluginBase):
    # 插件名称
    plugin_name = "站点聊天室"
    # 插件描述
    plugin_desc = "自动向多个站点发送预设消息。"
    # 插件图标
    plugin_icon = "signin.png"
    # 插件版本
    plugin_version = "2.3"
    # 插件作者
    plugin_author = "KoWming"
    # 作者主页
    author_url = "https://github.com/KoWming"
    # 插件配置项ID前缀
    plugin_config_prefix = "sitechatroom_"
    # 加载顺序
    plugin_order = 0
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    sites: SitesHelper = None
    siteoper: SiteOper = None
    sitechain: SiteChain = None
    # 事件管理器
    event: EventManager = None
    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None
    # 加载的模块
    _site_schema: list = []

    # 配置属性
    _enabled: bool = False
    _cron: str = ""
    _onlyonce: bool = False
    _notify: bool = False
    _interval_cnt: int = 5    # 消息间隔
    _sign_sites: list = []
    _site_messages: list = []  # 消息配置
    _clean: bool = False
    _start_time: int = None
    _end_time: int = None

    def init_plugin(self, config: dict = None):
        self.sites = SitesHelper()
        self.siteoper = SiteOper()
        self.event = EventManager()
        self.sitechain = SiteChain()

        # 停止现有任务
        self.stop_service()

        # 配置
        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._onlyonce = config.get("onlyonce")
            self._notify = config.get("notify")
            self._interval_cnt = config.get("interval_cnt") or 5
            self._sign_sites = config.get("sign_sites") or []
            self._site_messages = config.get("site_messages") or []

            # 过滤掉已删除的站点
            all_sites = [site.id for site in self.siteoper.list_order_by_pri()] + [site.get("id") for site in
                                                                                   self.__custom_sites()]
            self._sign_sites = [site_id for site_id in all_sites if site_id in self._sign_sites]
            # 保存配置
            self.__update_config()

        # 加载模块
        if self._enabled or self._onlyonce:

            self._site_schema = ModuleHelper.load('app.plugins.autosignin.sites',
                                                  filter_func=lambda _, obj: hasattr(obj, 'match'))

            # 立即运行一次
            if self._onlyonce:
                # 定时服务
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.info("站点预设消息发送服务启动，立即运行一次")
                self._scheduler.add_job(func=self.send_messages, trigger='date',
                                        run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                        name="预设消息发送")

                # 关闭一次性开关
                self._onlyonce = False
                # 保存配置
                self.__update_config()

                # 启动任务
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()
                    # 检查调度器状态
                    logger.info(f"调度器状态: {self._scheduler.running}")
                    logger.info(f"调度器任务: {self._scheduler.get_jobs()}")

    def get_state(self) -> bool:
        return self._enabled

    def __update_config(self):
        # 保存配置
        self.update_config(
            {
                "enabled": self._enabled,
                "notify": self._notify,
                "cron": self._cron,
                "onlyonce": self._onlyonce,
                "interval_cnt": self._interval_cnt,
                "sign_sites": self._sign_sites,
                "site_messages": self._site_messages,
                "clean": self._clean,
            }
        )

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron/interval/date/CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数
        }]
        """
        if self._enabled and self._cron:
            try:
                if str(self._cron).strip().count(" ") == 4:
                    return [{
                        "id": "SiteChatRoom",
                        "name": "预设消息发送服务",
                        "trigger": CronTrigger.from_crontab(self._cron),
                        "func": self.send_messages,
                        "kwargs": {}
                    }]
                else:
                    # 2.3/9-23
                    crons = str(self._cron).strip().split("/")
                    if len(crons) == 2:
                        # 2.3
                        cron = crons[0]
                        # 9-23
                        times = crons[1].split("-")
                        if len(times) == 2:
                            # 9
                            self._start_time = int(times[0])
                            # 23
                            self._end_time = int(times[1])
                        if self._start_time and self._end_time:
                            return [{
                                "id": "SiteChatRoom",
                                "name": "预设消息发送服务",
                                "trigger": "interval",
                                "func": self.send_messages,
                                "kwargs": {
                                    "hours": float(str(cron).strip()),
                                }
                            }]
                        else:
                            logger.error("站点自动签到服务启动失败，周期格式错误")
                    else:
                        # 默认0-24 按照周期运行
                        return [{
                            "id": "SiteChatRoom",
                            "name": "预设消息发送服务",
                            "trigger": "interval",
                            "func": self.send_messages,
                            "kwargs": {
                                "hours": float(str(self._cron).strip()),
                            }
                        }]
            except Exception as err:
                logger.error(f"定时任务配置错误：{str(err)}")
        elif self._enabled:
            # 随机时间
            triggers = TimerUtils.random_scheduler(num_executions=2,
                                                   begin_hour=9,
                                                   end_hour=23,
                                                   max_interval=6 * 60,
                                                   min_interval=2 * 60)
            ret_jobs = []
            for trigger in triggers:
                ret_jobs.append({
                    "id": f"SiteChatRoom|{trigger.hour}:{trigger.minute}",
                    "name": "预设消息发送服务",
                    "trigger": "cron",
                    "func": self.send_messages,
                    "kwargs": {
                        "hour": trigger.hour,
                        "minute": trigger.minute
                    }
                })
            return ret_jobs
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        # 站点的可选项（内置站点 + 自定义站点）
        customSites = self.__custom_sites()

        site_options = ([{"title": site.name, "value": site.id}
                         for site in self.siteoper.list_order_by_pri()]
                        + [{"title": site.get("name"), "value": site.get("id")}
                           for site in customSites])
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
                                            'label': '发送通知',
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
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
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
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VCronField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '执行周期',
                                            'placeholder': '5位cron表达式，留空自动'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'interval_cnt',
                                            'label': '执行间隔',
                                            'placeholder': '多消息自动发送间隔时间（秒）'
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
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'chips': True,
                                            'multiple': True,
                                            'model': 'sign_sites',
                                            'label': '选择站点',
                                            'items': site_options
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
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'site_messages',
                                            'label': '发送消息',
                                            'rows': 10,
                                            'placeholder': '每一行一个，格式如下：\n'
                                                           '站点名称|消息内容1|消息内容2|消息内容3|...\n'
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
                                            'text': '执行周期支持：'
                                                    '1、5位cron表达式；'
                                                    '2、配置间隔（小时），如2.3/9-23（9-23点之间每隔2.3小时执行一次）；'
                                                    '3、周期不填默认9-23点随机执行2次。'
                                                    '每天首次全量执行，其余执行命中重试关键词的站点。'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "notify": True,
            "cron": "",
            "onlyonce": False,
            "interval_cnt": 2,
            "sign_sites": [],
            "site_messages": []
        }

    def __custom_sites(self) -> List[Any]:
        custom_sites = []
        custom_sites_config = self.get_config("CustomSites")
        if custom_sites_config and custom_sites_config.get("enabled"):
            custom_sites = custom_sites_config.get("sites")
        return custom_sites

    def get_page(self) -> List[dict]:
        """
        拼装插件详情页面，需要返回页面配置，同时附带数据
        """
        pass

    def send_messages(self):
        try:
            logger.info(f"所选站点: {self._sign_sites}")
            logger.info(f"消息配置: {self._site_messages}")
            # 获取所有内置站点和自定义站点信息
            all_sites = {site.id: site for site in self.siteoper.list_order_by_pri()}
            custom_sites = self.__custom_sites()
            for site in custom_sites:
                all_sites[site.get("id")] = site

            # 过滤出所选站点信息
            selected_sites = {site_id: all_sites.get(site_id) for site_id in self._sign_sites if site_id in all_sites}

            # 添加日志记录，输出获取到的站点信息
            logger.info(f"获取到的站点信息: {selected_sites}")

            # 解析消息列表
            message_dict = {}
            for line in self._site_messages:
                parts = line.strip().split("|")
                if len(parts) > 1:
                    site_name = parts[0]
                    messages = parts[1:]
                    for site_id, site in selected_sites.items():
                        if site.get("name") == site_name:
                            message_dict[site_id] = messages

            # 遍历所选站点，发送消息
            for site_id, site in selected_sites.items():
                base_url = site.get("url")
                if base_url:
                    # 拼接 shoutbox.php
                    url = base_url.rstrip('/') + '/shoutbox.php'
                else:
                    url = None
                cookie = os.getenv(site.get("cookie_env", ""), "").strip()
                referer = site.get("referer", "")
                user_agent = site.get("user_agent", 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
                messages = message_dict.get(site_id, [])

                if not url or not cookie or not messages:
                    logger.warning(f"站点 {site.get('name')} 信息不完整，跳过发送消息")
                    continue

                for message in messages:
                    self._send_single_message(url, cookie, referer, user_agent, message)
                    time.sleep(self._interval_cnt)
        except Exception as e:
            logger.error(f"执行消息发送任务时出现异常: {e}")


    def _send_single_message(self, url, cookie, referer, user_agent, message):
        headers = {
            'User-Agent': user_agent,
            'Cookie': cookie,
            'Referer': referer,
        }
        data = {
            'shbox_text': message,
            'shout': '我喊',
            'sent': 'yes',
            'type': 'shoutbox'
        }

        try:
            response = requests.post(url, data=data, headers=headers)
            if response.status_code == 200:
                logger.info(f"成功向 {url} 发送消息: {message}")
            else:
                logger.error(f"向 {url} 发送消息失败: {response.status_code} - {message}")
        except Exception as e:
            logger.error(f"发送消息时出错: {e}")


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

    @eventmanager.register(EventType.SiteDeleted)
    def site_deleted(self, event):
        """
        删除对应站点选中
        """
        try:
            # 获取被删除站点的 ID
            site_id = event.event_data.get("site_id")
            if not site_id:
                logger.error("未获取到被删除站点的 ID")
                return

            # 获取插件配置
            config = self.get_config()
            if not config:
                logger.error("未获取到插件配置信息")
                return

            # 更新选中站点列表
            self._sign_sites = self.__remove_site_id(config.get("sign_sites") or [], site_id)

            # 保存配置
            self.__update_config()
            logger.info(f"成功移除站点 ID 为 {site_id} 的选中状态，并保存配置")
        except Exception as e:
            logger.error(f"处理站点删除事件时出错: {e}")


    def __remove_site_id(self, do_sites, site_id):
        try:
            # 处理 do_sites 为字符串的情况
            if isinstance(do_sites, str):
                do_sites = [do_sites]

            # 检查 site_id 是否为空
            if site_id:
                # 删除对应站点
                do_sites = [site for site in do_sites if int(site) != int(site_id)]
                logger.info(f"成功移除站点 ID 为 {site_id} 的站点")
            else:
                # 清空
                do_sites = []
                logger.info("清空站点列表")

            # 若无站点，则停止
            if len(do_sites) == 0:
                self._enabled = False
                logger.info("站点列表为空，禁用该功能")

            return do_sites
        except ValueError as e:
            logger.error(f"处理站点 ID 时出错：{e}")
            return do_sites
        except Exception as e:
            logger.error(f"移除站点 ID 时出错：{e}")
            return do_sites
