import pytz
import time
import requests
import threading
from lxml import etree
from datetime import datetime, timedelta
from typing import Any, List, Dict, Tuple, Optional
from urllib.parse import urljoin
from urllib3.util.retry import Retry
from requests.adapters import HTTPAdapter

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from ruamel.yaml import CommentedMap

from app.chain.site import SiteChain
from app.core.config import settings
from app.core.event import eventmanager
from app.db.site_oper import SiteOper
from app.helper.sites import SitesHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType
from app.utils.timer import TimerUtils

class _RequestHelper:
    """自定义请求工具类"""
    
    def __init__(self, plugin):
        self.plugin = plugin
        self.logger = plugin.logger
        self.retries = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[403, 404, 500, 502, 503, 504],
            allowed_methods=["GET", "POST"]
        )
        self.adapter = HTTPAdapter(max_retries=self.retries)

    def request(self, method: str, url: str, **kwargs) -> requests.Response:
        """统一请求方法"""
        # 合并代理配置
        proxies = kwargs.pop('proxies', None) or settings.PROXY
        
        # 配置默认超时
        timeout = kwargs.pop('timeout', (3.05, 10))
        
        # 创建会话
        with requests.Session() as session:
            session.mount('https://', self.adapter)
            session.proxies = proxies
            
            try:
                response = session.request(
                    method=method.upper(),
                    url=url,
                    timeout=timeout,
                    **kwargs
                )
                response.raise_for_status()
                self.logger.debug(f"请求成功: {method} {url}")
                return response
            except Exception as e:
                self.logger.error(f"请求失败: {method} {url} - {str(e)}")
                raise

class NexusPHPHelper:
    """NexusPHP站点操作增强工具类"""
    
    def __init__(self, site_info: dict, request_helper: '_RequestHelper'):
        """
        :param site_info: 站点信息字典，包含url/cookie/ua等
        :param request_helper: 请求工具类实例
        """
        # 初始化基础配置
        self.url = site_info.get('url', '').rstrip('/')
        self.cookie = site_info.get('cookie', '')
        self.ua = site_info.get('ua', 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 Edg/132.0.0.0')
        self.base_headers = {
            "Cookie": self.cookie,
            "Referer": self.url,
            "User-Agent": self.ua
        }
        
        # 初始化API端点
        self.endpoints = {
            'shoutbox': f"{self.url}/shoutbox.php",
            'messages': f"{self.url}/messages.php",
        }
        
        # 请求工具类
        self.request_helper = request_helper

    def send_message(self, message: str) -> str:
        """
        发送群聊消息
        :param message: 要发送的消息内容
        :return: 操作结果描述
        """
        params = {
            "shbox_text": message,
            "shout": "我喊",
            "sent": "yes",
            "type": "shoutbox"
        }
        
        try:
            response = self.request_helper.request(
                method="GET",
                url=self.endpoints['shoutbox'],
                params=params,
                headers=self.base_headers,
                timeout=15
            )
            
            # 解析响应结果
            return self._parse_response(response, lambda r: " ".join(
                etree.HTML(r.text).xpath("//tr[1]/td//text()"))
            )
        except Exception as e:
            logger.error(f"消息发送失败: {str(e)}")
            return f"失败: {str(e)}"

    def get_messages(self, count: int = 10) -> list:
        """
        获取最新群聊消息
        :param count: 获取消息条数
        :return: 消息列表
        """
        try:
            response = self.request_helper.request(
                method="GET",
                url=self.endpoints['shoutbox'],
                headers=self.base_headers,
                timeout=10
            )
            
            return self._parse_response(response, lambda r: [
                "".join(item.xpath(".//text()")) 
                for item in etree.HTML(r.text).xpath("//tr/td")[:count]
            ])
        except Exception as e:
            logger.error(f"获取消息失败: {str(e)}")
            return []

    def get_message_list(self, rt_method: callable = None) -> list:
        """获取站内信列表"""
        try:
            response = self.request_helper.request(
                method="GET",
                url=self.endpoints['messages'],
                headers=self.base_headers
            )
            
            # 默认解析逻辑
            if not rt_method:
                rt_method = lambda res: [
                    {
                        "status": "".join(item.xpath("./td[1]/img/@title")),
                        "topic": "".join(item.xpath("./td[2]//text()")),
                        "from": "".join(item.xpath("./td[3]/text()")),
                        "time": "".join(item.xpath("./td[4]//text()")),
                        "id": "".join(item.xpath("./td[5]/input/@value"))
                    }
                    for item in etree.HTML(response.text).xpath("//form/table//tr")
                ]
                
            return rt_method(response)
        except Exception as e:
            logger.error(f"获取站内信失败: {str(e)}")
            return []

    def set_message_read(self, message_id: str, rt_method: callable = None) -> bool:
        """标记站内信为已读"""
        try:
            data = {
                "action": "moveordel",
                "messages[]": message_id,
                "markread": "设为已读",
                "box": "1"
            }
            
            response = self.request_helper.request(
                method="POST",
                url=self.endpoints['messages'],
                headers=self.base_headers,
                data=data
            )
            
            # 默认成功判断
            if not rt_method:
                rt_method = lambda res: res.status_code == 200
                
            return rt_method(response)
        except Exception as e:
            logger.error(f"标记已读失败: {str(e)}")
            return False

    def _parse_response(self, response, parser: callable):
        """统一响应解析方法"""
        try:
            return parser(response)
        except Exception as e:
            logger.error(f"响应解析失败: {str(e)}")
            return "响应解析失败"

class GroupChatZone(_PluginBase):
    # 插件名称
    plugin_name = "群聊区"
    # 插件描述
    plugin_desc = "定时向多个站点发送预设消息(特定站点可获得奖励)。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/KoWming/MoviePilot-Plugins/main/icons/GroupChat.png"
    # 插件版本
    plugin_version = "1.2.6"
    # 插件作者
    plugin_author = "KoWming"
    # 作者主页
    author_url = "https://github.com/KoWming"
    # 插件配置项ID前缀
    plugin_config_prefix = "groupchatzone_"
    # 加载顺序
    plugin_order = 0
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    sites: SitesHelper = None
    siteoper: SiteOper = None
    sitechain: SiteChain = None
    
    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    # 配置属性
    _enabled: bool = False
    _cron: str = ""
    _onlyonce: bool = False
    _notify: bool = False
    _interval_cnt: int = 2
    _chat_sites: list = []
    _sites_messages: list = []
    _start_time: int = None
    _end_time: int = None
    _lock = None
    _running = False

    def __init__(self):
        super().__init__()
        self.logger = logger 

    def init_plugin(self, config: dict = None):
        self._lock = threading.Lock()
        self.sites = SitesHelper()
        self.siteoper = SiteOper()
        self.sitechain = SiteChain()

        # 停止现有任务
        self.stop_service()

        # 配置
        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._onlyonce = config.get("onlyonce")
            self._notify = config.get("notify")
            self._interval_cnt = int(config.get("interval_cnt", 2))
            self._chat_sites = config.get("chat_sites", [])
            self._sites_messages = config.get("sites_messages", "")


            # 过滤掉已删除的站点
            valid_site_ids = [str(site.get("id")) for site in self.get_all_sites()]
            self._chat_sites = [
                site_id for site_id in self._chat_sites 
                if str(site_id) in valid_site_ids
            ]

            # 保存配置
            self.__update_config()

        # 加载模块
        if self._enabled or self._onlyonce:

            # 立即运行一次
            if self._onlyonce:
                # 定时服务
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                logger.info("站点喊话服务启动，立即运行一次")
                self._scheduler.add_job(func=self.send_site_messages, trigger='date',
                                        run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                        name="站点喊话服务")

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

    def __custom_sites(self) -> List[Any]:
        """获取自定义站点"""
        custom_sites = []
        custom_sites_config = self.get_config("CustomSites")
        if custom_sites_config and custom_sites_config.get("enabled"):
            custom_sites = custom_sites_config.get("sites")
        return custom_sites

    def get_all_sites(self) -> List[dict]:
        """获取所有站点（内置+自定义）"""
        builtin_sites = [
            {
                "id": site.id,
                "name": site.name,
                "url": site.url,
                "cookie": site.cookie,
                "ua": site.ua,
                "proxy": site.proxy
            } 
            for site in self.siteoper.list_order_by_pri()
        ]
        return builtin_sites + self.__custom_sites()

    def get_selected_sites(self) -> List[dict]:
        """获取已选中的有效站点"""
        all_sites = self.get_all_sites()
        return [
            site for site in all_sites
            if str(site.get("id")) in map(str, self._chat_sites)
        ]

    def parse_site_messages(self, site_messages: str) -> Dict[str, List[str]]:
        """解析输入的站点消息"""
        result = {}
        try:
            # 获取已选站点的名称集合
            selected_sites = self.get_selected_sites()
            valid_site_names = {site.get("name").strip() for site in selected_sites}
            
            logger.debug(f"有效站点名称列表: {valid_site_names}")

            # 按行解析配置
            for line_num, line in enumerate(site_messages.strip().splitlines(), 1):
                line = line.strip()
                if not line:
                    continue  # 跳过空行

                # 分割配置项
                parts = line.split("|")
                if len(parts) < 2:
                    logger.warning(f"第{line_num}行格式错误，缺少分隔符: {line}")
                    continue

                # 解析站点名称和消息
                site_name = parts[0].strip()
                messages = [msg.strip() for msg in parts[1:] if msg.strip()]
                
                if not messages:
                    logger.warning(f"第{line_num}行 [{site_name}] 没有有效消息内容")
                    continue

                # 验证站点有效性
                if site_name not in valid_site_names:
                    logger.warning(f"第{line_num}行 [{site_name}] 不在选中站点列表中")
                    continue

                # 合并相同站点的消息
                if site_name in result:
                    result[site_name].extend(messages)
                    logger.debug(f"合并重复站点 [{site_name}] 的消息，当前数量：{len(result[site_name])}")
                else:
                    result[site_name] = messages

        except Exception as e:
            logger.error(f"解析站点消息时出现异常: {str(e)}", exc_info=True)
        finally:
            logger.info(f"解析完成，共配置 {len(result)} 个有效站点的消息")
            return result

    def send_site_messages(self):
        """
        发送站点消息
        """
        try:
            # 获取选中的站点信息
            selected_sites = self.get_selected_sites()
            
            # 解析站点消息
            site_messages = self.parse_site_messages(self._sites_messages)
            
            # 初始化请求工具类
            request_helper = _RequestHelper(self)
            
            for site in selected_sites:
                try:
                    site_name = site.get("name")
                    messages = site_messages.get(site_name)
                    
                    if not messages:
                        logger.warning(f"站点 {site_name} 没有配置消息，跳过发送")
                        continue
                    
                    # 初始化NexusPHPHelper
                    nexus_helper = NexusPHPHelper(site_info=site, request_helper=request_helper)
                    
                    for message in messages:
                        try:
                            result = nexus_helper.send_message(message)
                            logger.info(f"向站点 {site_name} 发送消息 '{message}' 结果: {result}")
                        except Exception as e:
                            logger.error(f"向站点 {site_name} 发送消息 '{message}' 失败: {str(e)}")
                        finally:
                            # 等待间隔时间
                            time.sleep(self._interval_cnt)
                except Exception as e:
                    logger.error(f"处理站点 {site.get('name')} 时发生错误: {str(e)}")
        except Exception as e:
            logger.error(f"发送站点消息时发生全局错误: {str(e)}")

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
                        "id": "GroupChatZone",
                        "name": "站点喊话服务",
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
                                "id": "GroupChatZone",
                                "name": "站点喊话服务",
                                "trigger": "interval",
                                "func": self.send_site_messages,
                                "kwargs": {
                                    "hours": float(str(cron).strip()),
                                }
                            }]
                        else:
                            logger.error("站点喊话服务启动失败，周期格式错误")
                    else:
                        # 默认0-24 按照周期运行
                        return [{
                            "id": "GroupChatZone",
                            "name": "站点喊话服务",
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
            triggers = TimerUtils.random_scheduler(num_executions=1,
                                                   begin_hour=9,
                                                   end_hour=23,
                                                   max_interval=6 * 60,
                                                   min_interval=2 * 60)
            ret_jobs = []
            for trigger in triggers:
                ret_jobs.append({
                    "id": f"GroupChatZone|{trigger.hour}:{trigger.minute}",
                    "name": "站点喊话服务",
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
                                            'rows': 8,
                                            'placeholder': '每一行一个配置，配置方式：\n'
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
                                            'text': '配置注意事项：'
                                                    '1、注意定时任务设置，避免每分钟执行一次导致频繁请求；'
                                                    '2、消息发送执行间隔(秒)不能小于0，也不建议设置过大。1~5秒即可，设置过大可能导致线程运行时间过长；'
                                                    '3、如配置有全局代理，会默认调用全局代理执行。'
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
                                                    '3、周期不填默认9-23点随机执行1次。'
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
            "notify": False,
            "cron": "",
            "onlyonce": False,
            "interval_cnt": 2,
            "chat_sites": [],
            "sites_messages": ""
        }

    def get_page(self) -> List[dict]:
        pass

    def stop_service(self):
        """退出插件"""
        try:
            if self._scheduler:
                if self._lock.locked():
                    logger.info("等待当前任务执行完成...")
                    self._lock.acquire()
                    self._lock.release()
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error(f"退出插件失败：{str(e)}")
