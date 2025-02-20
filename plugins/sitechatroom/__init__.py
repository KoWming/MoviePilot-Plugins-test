import re
import time
import traceback
from datetime import datetime, timedelta
from multiprocessing.dummy import Pool as ThreadPool
from multiprocessing.pool import ThreadPool
from typing import Any, List, Dict, Tuple, Optional
from urllib.parse import urljoin

from requests import Response
import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from ruamel.yaml import CommentedMap

from app import schemas
from app.chain.site import SiteChain
from app.core.config import settings
from app.core.event import EventManager, eventmanager, Event
from app.db.site_oper import SiteOper
from app.helper.browser import PlaywrightHelper
from app.helper.cloudflare import under_challenge
from app.helper.module import ModuleHelper
from app.helper.sites import SitesHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType
from app.utils.http import RequestUtils
from app.utils.site import SiteUtils
from app.utils.string import StringUtils
from app.utils.timer import TimerUtils
import os


class SiteChatRoom(_PluginBase):
    # 插件名称
    plugin_name = "站点聊天室"
    # 插件描述
    plugin_desc = "自动向多个站点发送预设消息。"
    # 插件图标
    plugin_icon = "signin.png"
    # 插件版本
    plugin_version = "2.5"
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
    _interval_cnt: int = 5
    _chat_sites: list = []
    _site_messages: dict = {}
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
            self._chat_sites = config.get("chat_sites") or []
            self._site_messages = config.get("site_messages") or {}

            # 过滤掉已删除的站点
            all_sites = [site.id for site in self.siteoper.list_order_by_pri()] + [site.get("id") for site in
                                                                                   self.__custom_sites()]
            self._chat_sites = [site_id for site_id in all_sites if site_id in self._chat_sites]
            # 保存配置
            self.__update_config()

        # 加载模块
        if self._enabled or self._onlyonce:
            # 这里暂时不需要加载模块，因为主要是发送消息
            pass

            # 立即运行一次
            if self._onlyonce:
                # 定时服务
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.info("站点聊天室消息发送服务启动，立即运行一次")
                self._scheduler.add_job(func=self.send_chat_messages, trigger='date',
                                        run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                        name="站点聊天室消息发送")

                # 关闭一次性开关
                self._onlyonce = False
                # 保存配置
                self.__update_config()

                # 启动任务
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

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
                "chat_sites": self._chat_sites,
                "site_messages": self._site_messages,
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
                        "name": "站点聊天室消息发送服务",
                        "trigger": CronTrigger.from_crontab(self._cron),
                        "func": self.send_chat_messages,
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
                                "name": "站点聊天室消息发送服务",
                                "trigger": "interval",
                                "func": self.send_chat_messages,
                                "kwargs": {
                                    "hours": float(str(cron).strip()),
                                }
                            }]
                        else:
                            logger.error("站点聊天室消息发送服务启动失败，周期格式错误")
                    else:
                        # 默认0-24 按照周期运行
                        return [{
                            "id": "SiteChatRoom",
                            "name": "站点聊天室消息发送服务",
                            "trigger": "interval",
                            "func": self.send_chat_messages,
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
                    "name": "站点聊天室消息发送服务",
                    "trigger": "cron",
                    "func": self.send_chat_messages,
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
                                            'model': 'chat_sites',
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
                                            'placeholder': '每行格式：站点ID|消息内容1|消息内容2|...'
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
            "chat_sites": [],
            "site_messages": {}
        }

    def __custom_sites(self) -> List[Any]:
        custom_sites = []
        custom_sites_config = self.get_config("CustomSites")
        if custom_sites_config and custom_sites_config.get("enabled"):
            custom_sites = custom_sites_config.get("sites")
        return custom_sites

    def get_page(self) -> List[dict]:
        pass

    def send_chat_messages(self, event: Event = None):
        """
        向选定站点发送聊天消息
        """
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "send_chat_messages":
                return

        # 日期
        today = datetime.today()
        if self._start_time and self._end_time:
            if int(datetime.today().hour) < self._start_time or int(datetime.today().hour) > self._end_time:
                logger.error(
                    f"当前时间 {int(datetime.today().hour)} 不在 {self._start_time}-{self._end_time} 范围内，暂不执行任务")
                return

        if event:
            logger.info("收到命令，开始向站点发送消息 ...")
            self.post_message(channel=event.event_data.get("channel"),
                              title="开始向站点发送消息 ...",
                              userid=event.event_data.get("user"))

        if self._chat_sites:
            for site_id in self._chat_sites:
                messages = self._site_messages.get(str(site_id))
                if messages:
                    site_info = self.siteoper.get(site_id)
                    if site_info:
                        self.__send_messages_to_site(site_info, messages)

    def __send_messages_to_site(self, site_info: CommentedMap, messages: List[str]):
        """
        向单个站点发送多条消息
        """
        site = site_info.get("name")
        site_url = site_info.get("url")
        site_cookie = site_info.get("cookie")
        ua = site_info.get("ua")
        render = site_info.get("render")
        proxies = settings.PROXY if site_info.get("proxy") else None
        proxy_server = settings.PROXY_SERVER if site_info.get("proxy") else None

        if not site_url or not site_cookie:
            logger.warn(f"未配置 {site} 的站点地址或Cookie，无法发送消息")
            return

        for message in messages:
            try:
                if render:
                    page = PlaywrightHelper().get_page(url=site_url,
                                                       cookies=site_cookie,
                                                       ua=ua,
                                                       proxies=proxy_server)
                    # 假设表单元素ID为 'chat_message' 和 'send_button'
                    page.fill('#chat_message', message)
                    page.click('#send_button')
                    logger.info(f"向 {site} 发送消息：{message} 成功")
                else:
                    message_url = urljoin(site_url, "/shoutbox.php")
                    headers = {
                        "User-Agent": ua,
                        "Cookie": site_cookie
                    }
                    data = {
                        "message": message
                    }
                    response = RequestUtils(headers=headers, proxies=proxies).post(url=message_url, data=data)
                    if response and response.status_code == 200:
                        logger.info(f"向 {site} 发送消息：{message} 成功")
                    else:
                        logger.error(f"向 {site} 发送消息：{message} 失败，状态码：{response.status_code if response else '无'}")

                # 发送间隔
                time.sleep(self._interval_cnt)

            except Exception as e:
                logger.error(f"向 {site} 发送消息：{message} 时出现错误：{str(e)}")

        if self._notify:
            self.post_message(channel=NotificationType.SiteChatRoom,
                              title=f"向 {site} 发送消息完成",
                              text="消息已全部发送")


    def post_message(self, channel: NotificationType, title: str, text: str = None, userid: str = None):
        """
        发送通知消息
        """
        try:
            self.eventmanager.send_event(EventType.Notify, {
                "channel": channel,
                "title": title,
                "text": text,
                "userid": userid
            })
        except Exception as e:
            logger.error(f"发送通知消息失败：{str(e)}")

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
        site_id = event.event_data.get("site_id")
        config = self.get_config()
        if config:
            self._sign_sites = self.__remove_site_id(config.get("sign_sites") or [], site_id)
            # 保存配置
            self.__update_config()

    def __remove_site_id(self, do_sites, site_id):
        if do_sites:
            if isinstance(do_sites, str):
                do_sites = [do_sites]

            # 删除对应站点
            if site_id:
                do_sites = [site for site in do_sites if int(site) != int(site_id)]
            else:
                # 清空
                do_sites = []

            # 若无站点，则停止
            if len(do_sites) == 0:
                self._enabled = False

        return do_sites