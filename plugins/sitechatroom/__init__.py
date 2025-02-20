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
    plugin_version = "2.8.6"
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
            self._site_messages = config.get("site_messages") or []

            # 确保 _site_messages 是一个字符串列表
            if isinstance(self._site_messages, dict):
                self._site_messages = [f"{site_id}|{'|'.join(messages)}" for site_id, messages in self._site_messages.items()]

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
        """向选定站点发送聊天消息（完整实现）"""
        try:
            logger.info("开始执行send_chat_messages函数")
            if event:
                event_data = event.event_data
                if not event_data or event_data.get("action") != "send_chat_messages":
                    return
                logger.info("收到命令，开始向站点发送消息 ...")
                self.post_message(channel=event.event_data.get("channel"),
                                title="开始向站点发送消息 ...",
                                userid=event.event_data.get("user"))

            if self._chat_sites:
                # 获取所有可用站点（内置+自定义）
                all_sites = {
                    str(site.id): site
                    for site in self.siteoper.list_order_by_pri()
                }
                all_sites.update({
                    site.get("id"): site
                    for site in self.__custom_sites()
                })

                logger.debug(f"all_sites 类型: {type(all_sites)}")
                logger.debug(f"all_sites 内容: {all_sites}")

                for site_id in self._chat_sites:
                    str_site_id = str(site_id)
                    # 获取站点配置信息
                    site_info = all_sites.get(str_site_id)
                    if not site_info:
                        logger.warn(f"站点 {site_id} 配置不存在，跳过处理")
                        continue

                    # 解析消息列表
                    message_dict = {}
                    for line in self._site_messages:
                        parts = line.strip().split("|")
                        if len(parts) > 1:
                            site_name = parts[0]
                            messages = parts[1:]
                            for site_id, site in all_sites.items():
                                if site.get("name") == site_name:
                                    message_dict[site_id] = messages

                    # 获取消息列表
                    messages = message_dict.get(str_site_id)
                    if not messages:
                        logger.info(f"站点 {site_info.get('name')} 没有需要发送的消息")
                        continue
                    
                    # 执行消息发送
                    self.__send_messages_to_site(site_info, messages)
        except Exception as e:
            logger.error(f"send_chat_messages函数执行失败: {str(e)}")
            traceback.print_exc()

    def __send_messages_to_site(self, site_info: CommentedMap, messages: List[str]):
        """向单个站点发送消息完整实现"""
        site_name = site_info.get("name")
        site_url = site_info.get("url")
        site_cookie = site_info.get("cookie")
        ua = site_info.get("ua") or settings.USER_AGENT
        render = site_info.get("render")
        
        if not all([site_url, site_cookie]):
            logger.warn(f"站点 {site_name} 配置不完整，需要URL和Cookie")
            return

        success_count = 0
        for index, message in enumerate(messages, 1):
            try:
                # 渲染模式处理（Playwright）
                if render:
                    with ThreadPool(processes=1) as pool:
                        result = pool.apply_async(self._send_with_playwright,
                                                (site_url, site_cookie, ua, message))
                        result.get(timeout=120)
                # 普通模式处理
                else:
                    self._send_with_requests(site_url, site_cookie, ua, message)
                
                success_count += 1
                logger.info(f"[{site_name}] 第{index}条消息发送成功: {message}")
                
                # 执行间隔（最后一条不等待）
                if index < len(messages):
                    time.sleep(self._interval_cnt)

            except Exception as e:
                logger.error(f"[{site_name}] 消息发送失败: {str(e)}")
                traceback.print_exc()

        # 发送通知
        if self._notify:
            status = f"成功发送 {success_count}/{len(messages)} 条消息"
            self.post_message(channel=NotificationType.SiteChatRoom,
                            title=f"[{site_name}] 消息发送完成",
                            text=status)

    def _send_with_playwright(self, url: str, cookie: str, ua: str, message: str):
        """Playwright实现发送逻辑"""
        try:
            with PlaywrightHelper() as helper:
                page = helper.get_page(url=url, cookies=cookie, ua=ua)
                page.fill('#iframe-shout-box', message)
                page.click('#hbsubmit')
                helper.sleep(5)
        except Exception as e:
            raise Exception(f"浏览器模式发送失败: {str(e)}")

    def _send_with_requests(self, url: str, cookie: str, ua: str, message: str):
        """Requests实现发送逻辑"""
        message_url = urljoin(url, "/shoutbox.php")
        headers = {"User-Agent": ua, "Cookie": cookie}
        
        try:
            response = RequestUtils(headers=headers).post(
                url=message_url,
                data={"message": message},
                timeout=60
            )
            if not response or response.status_code != 200:
                raise Exception(f"HTTP状态码异常: {getattr(response, 'status_code', '无响应')}")
        except Exception as e:
            raise Exception(f"API模式发送失败: {str(e)}")

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
            self._chat_sites = self.__remove_site_id(config.get("chat_sites") or [], site_id)
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