import re
import time
import requests
import traceback
from datetime import datetime, timedelta
from multiprocessing.dummy import Pool as ThreadPool
from multiprocessing.pool import ThreadPool
from typing import Any, List, Dict, Tuple, Optional
from urllib.parse import urljoin

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


class SiteChatRoom(_PluginBase):
    # 插件名称
    plugin_name = "站点聊天室"
    # 插件描述
    plugin_desc = "自动向多个站点发送预设消息。"
    # 插件图标
    plugin_icon = "signin.png"
    # 插件版本
    plugin_version = "2.0.3"
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

    # 配置属性
    _enabled: bool = False
    _cron: str = ""
    _onlyonce: bool = False
    _notify: bool = False
    _interval_cnt: int = 5
    _chat_sites: list = []
    _sites_messages: list = []
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
            self._interval_cnt = config.get("interval_cnt") or 2
            self._chat_sites = config.get("chat_sites") or []
            self._sites_messages = config.get("sites_messages")


            # 过滤掉已删除的站点
            all_sites = [site.id for site in self.siteoper.list_order_by_pri()] + [site.get("id") for site in self.__custom_sites()]
            self._chat_sites = [site_id for site_id in all_sites if site_id in self._chat_sites]

            # 保存配置
            self.__update_config()

        # 加载模块
        if self._enabled or self._onlyonce:

            # 立即运行一次
            if self._onlyonce:
                # 定时服务
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.info("站点自动签到服务启动，立即运行一次")
                self._scheduler.add_job(func=self.send_site_messages, trigger='date',
                                        run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                        name="站点自动签到")

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
                "sites_messages": self._sites_messages
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
                        "id": "AutoSignIn",
                        "name": "站点自动签到服务",
                        "trigger": CronTrigger.from_crontab(self._cron),
                        "func": self.send_site_messages,
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
                                "id": "AutoSignIn",
                                "name": "站点自动签到服务",
                                "trigger": "interval",
                                "func": self.send_site_messages,
                                "kwargs": {
                                    "hours": float(str(cron).strip()),
                                }
                            }]
                        else:
                            logger.error("站点自动签到服务启动失败，周期格式错误")
                    else:
                        # 默认0-24 按照周期运行
                        return [{
                            "id": "AutoSignIn",
                            "name": "站点自动签到服务",
                            "trigger": "interval",
                            "func": self.send_site_messages,
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
                    "id": f"AutoSignIn|{trigger.hour}:{trigger.minute}",
                    "name": "站点自动签到服务",
                    "trigger": "cron",
                    "func": self.send_site_messages,
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
                                            'model': 'sites_messages',
                                            'label': '发送消息',
                                            'rows': 10,
                                            'placeholder': '每行格式：站点名称|消息内容1|消息内容2|...'
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
            "sites_messages": ""
        }

    def __custom_sites(self) -> List[Any]:
        custom_sites = []
        custom_sites_config = self.get_config("CustomSites")
        if custom_sites_config and custom_sites_config.get("enabled"):
            custom_sites = custom_sites_config.get("sites")
        return custom_sites

    def get_page(self) -> List[dict]:
        pass

    @eventmanager.register(EventType.PluginAction)
    def send_site_messages(self, event: Event = None):
        """
        自动向站点发送消息
        """
        try:
            logger.info("进入 send_site_messages 函数")
            if event:
                event_data = event.event_data
                if not event_data or event_data.get("action") != "site_send_messages":
                    return
            # 日期
            today = datetime.today()
            if self._start_time and self._end_time:
                current_hour = int(datetime.today().hour)
                if current_hour < self._start_time or current_hour > self._end_time:
                    logger.error(
                        f"当前时间 {current_hour} 不在 {self._start_time}-{self._end_time} 范围内，暂不执行任务")
                    return
            if event:
                logger.info("收到命令，开始向站点发送消息 ...")
                self.post_message(channel=event.event_data.get("channel"),
                                  title="开始向站点发送消息 ...",
                                  userid=event.event_data.get("user"))

            if self._chat_sites:
                site_msgs = self.parse_site_messages("\n".join(self._sites_messages))
                self.__send_msgs(today=today, do_sites=self._chat_sites, site_msgs=site_msgs, event=event)
            logger.info("send_site_messages 函数执行成功")
        except Exception as e:
            logger.error(f"send_site_messages 函数执行失败: {str(e)}")

    def __send_msgs(self, today: datetime, do_sites: list, site_msgs: Dict[str, List[str]], event: Event = None):
        """
        发送消息逻辑
        """
        try:
            logger.info("进入 __send_msgs 函数")
            # 查询所有站点
            all_sites = [site for site in self.sites.get_indexers() if not site.get("public")] + self.__custom_sites()
            # 过滤掉没有选中的站点
            if do_sites:
                do_sites = [site for site in all_sites if site.get("id") in do_sites]
            else:
                do_sites = all_sites

            if not do_sites:
                logger.info("没有需要发送消息的站点")
                return

            # 执行发送消息
            logger.info("开始执行发送消息任务 ...")
            for site in do_sites:
                site_name = site.get("name")
                logger.info(f"开始处理站点: {site_name}")
                messages = site_msgs.get(site_name, [])
                for i, message in enumerate(messages):
                    self.send_msg_to_site(site, message)
                    if i < len(messages) - 1:
                        logger.info(f"等待 {self._interval_cnt} 秒...")
                        time.sleep(self._interval_cnt)

            # 保存配置
            self.__update_config()
        except Exception as e:
            logger.error(f"发送消息过程中出现异常: {str(e)}")




    def send_msg_to_site(self, site_info: CommentedMap, message: str):
        """
        向一个站点发送消息
        """
        logger.info(f"进入 send_msg_to_site 函数，准备向 {site_info.get('name')} 发送消息")
        # 站点信息
        site_name = site_info.get("name") 
        site_url = site_info.get("url")
        site_cookie = site_info.get("cookie")
        ua = site_info.get("ua")
        proxies = settings.PROXY if site_info.get("proxy") else None
        if not site_name or not site_url or not site_cookie or not ua:
            logger.error(f"站点 {site_name} 缺少必要信息，无法发送消息")
            return

        try:
            send_url = urljoin(site_url, "/shoutbox.php")
            headers = {
                'User-Agent': ua,
                'Cookie': site_cookie,
                'Referer': site_url
            }
            params = {
                'shbox_text': message,
                'shout': '我喊',
                'sent': 'yes',
                'type': 'shoutbox'
            }
            try:
                response = requests.get(send_url, params=params, headers=headers, proxies=proxies)
            except Exception as req_err:
                logger.warn(f"发送请求失败：{str(req_err)}")
                return

            if response and response.status_code == 200:
                logger.info(f"向 {site_info.get('name')} 发送消息 '{message}' 成功")
            else:
                logger.warn(f"向 {site_info.get('name')} 发送消息 '{message}' 失败，状态码：{response.status_code if response else '无响应'}")
        except Exception as e:
            logger.warn(f"向 {site_info.get('name')} 发送消息 '{message}' 失败：{str(e)}")


    def parse_site_messages(self, site_messages: str) -> Dict[str, List[str]]:
        """
        解析输入的站点消息
        :param site_messages: 多行文本输入
        :return: 字典，键为站点名称，值为该站点的消息
        """
        logger.info("开始解析输入的站点消息")
        result = {}
        try:
            # 获取所有选中的站点名称
            all_sites = [site for site in self.sites.get_indexers() if not site.get("public")] + self.__custom_sites()
            selected_site_names = [site.get("name") for site in all_sites if site.get("id") in self._chat_sites]
            logger.info(f"获取到的选中站点名称列表: {selected_site_names}")

            # 按"|"分割配置
            parts = str(site_messages).split("|")
            if len(parts) > 1:
                site_name = parts[0].strip()
                logger.debug(f"解析出的站点名称: {site_name}")
                # 检查站点是否在选中列表中
                if site_name in selected_site_names:
                    # 获取消息内容并去除前后空格
                    messages = [msg.strip() for msg in parts[1:] if msg.strip()]
                    if messages:  # 如果有有效消息才添加
                        result[site_name] = messages
                        logger.info(f"成功解析站点 {site_name} 的消息: {messages}")
                    else:
                        logger.warn(f"站点 {site_name} 没有有效的消息内容")
                else:
                    logger.warn(f"配置行格式错误，缺少分隔符")
            else:
                logger.warn(f"配置行格式错误，缺少分隔符")
        except Exception as e:
            logger.error(f"解析站点消息时出现异常: {str(e)}")
        logger.info(f"站点消息解析完成，解析结果: {result}")
        return result


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
    