import pytz
import time
import requests
import threading
import re
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

# 自定义异常类
class GroupChatZoneError(Exception):
    """GroupChatZone插件的基础异常类"""
    pass

class MessageSendError(GroupChatZoneError):
    """消息发送失败异常"""
    pass

class GroupChatZone(_PluginBase):
    # 插件名称
    plugin_name = "群聊区"
    # 插件描述
    plugin_desc = "定时向多个站点发送预设消息(特定站点可获得奖励)。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/KoWming/MoviePilot-Plugins/main/icons/GroupChat.png"
    # 插件版本
    plugin_version = "1.2.5"  # 版本号更新
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
    _sites_messages: str = ""
    _start_time: Optional[int] = None
    _end_time: Optional[int] = None
    _lock: Optional[threading.Lock] = None
    _running: bool = False
    
    # 缓存解析后的站点消息
    _parsed_messages_cache: Dict[str, List[str]] = {}
    _last_parse_time: float = 0

    def init_plugin(self, config: Optional[dict] = None):
        """
        初始化插件
        :param config: 插件配置
        """
        # 初始化锁和服务
        if self._lock is None:
            self._lock = threading.Lock()
            
        # 初始化站点相关服务
        self.sites = SitesHelper()
        self.siteoper = SiteOper()
        self.sitechain = SiteChain()

        # 停止现有任务
        self.stop_service()

        # 初始化缓存
        self._parsed_messages_cache = {}
        self._last_parse_time = 0

        # 配置
        if config:
            # 基本配置
            self._enabled = bool(config.get("enabled", False))
            self._cron = str(config.get("cron", ""))
            self._onlyonce = bool(config.get("onlyonce", False))
            self._notify = bool(config.get("notify", False))
            
            # 间隔配置 - 确保在合理范围内
            interval_cnt = int(config.get("interval_cnt", 2))
            self._interval_cnt = max(1, min(interval_cnt, 10))  # 限制在1-10秒之间
            
            # 站点和消息配置
            self._chat_sites = config.get("chat_sites", [])
            self._sites_messages = str(config.get("sites_messages", ""))

            # 过滤掉已删除的站点
            self._validate_and_filter_sites()

            # 保存配置
            self.__update_config()
            
            # 预解析消息配置
            if self._enabled or self._onlyonce:
                self._parsed_messages_cache = self.parse_site_messages(self._sites_messages)
                self._last_parse_time = time.time()

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
                if self._scheduler and self._scheduler.get_jobs():
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
        """
        注册命令
        """
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        """
        注册API
        """
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
        if not self._enabled:
            logger.debug("插件未启用，不注册服务")
            return []
            
        try:
            # 有配置cron表达式
            if self._cron:
                return self._configure_cron_service()
            
            # 默认配置: 每天随机执行一次
            logger.info("使用默认配置: 9-23点之间随机执行一次")
            return self._configure_random_service()
            
        except Exception as e:
            logger.error(f"配置定时任务失败: {str(e)}")
            import traceback
            logger.debug(f"异常详情: {traceback.format_exc()}")
            return []
            
    def _configure_cron_service(self) -> List[Dict[str, Any]]:
        """
        配置基于cron表达式的服务
        :return: 服务配置列表
        """
        # 标准cron表达式 (5个空格分隔的字段)
        if str(self._cron).strip().count(" ") == 4:
            logger.info(f"使用cron表达式配置定时任务: {self._cron}")
            return [{
                "id": "GroupChatZone",
                "name": "站点喊话服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.send_site_messages,
                "kwargs": {}
            }]
        
        # 自定义格式: 间隔/时间范围 (例如: 2.3/9-23)
        crons = str(self._cron).strip().split("/")
        if len(crons) == 2:
            try:
                # 解析间隔时间
                interval_hours = float(crons[0].strip())
                
                # 解析时间范围
                times = crons[1].split("-")
                if len(times) == 2:
                    self._start_time = int(times[0].strip())
                    self._end_time = int(times[1].strip())
                    
                    # 验证时间范围
                    if not (0 <= self._start_time <= 23 and 0 <= self._end_time <= 23):
                        logger.warning(f"时间范围无效: {self._start_time}-{self._end_time}，使用默认值 9-23")
                        self._start_time = 9
                        self._end_time = 23
                    
                    logger.info(f"使用自定义间隔配置定时任务: 每{interval_hours}小时执行一次，在{self._start_time}-{self._end_time}点之间")
                    return [{
                        "id": "GroupChatZone",
                        "name": "站点喊话服务",
                        "trigger": "interval",
                        "func": self.send_site_messages,
                        "kwargs": {
                            "hours": interval_hours,
                        }
                    }]
            except (ValueError, TypeError) as e:
                logger.error(f"解析自定义间隔配置失败: {str(e)}")
        
        # 简单间隔 (例如: 2.5)
        try:
            interval_hours = float(str(self._cron).strip())
            if interval_hours <= 0:
                logger.warning(f"间隔时间 {interval_hours} 无效，使用默认值 2 小时")
                interval_hours = 2
            elif interval_hours < 0.1:
                logger.warning(f"间隔时间 {interval_hours} 过小，已调整为 0.1 小时")
                interval_hours = 0.1
                
            logger.info(f"使用简单间隔配置定时任务: 每{interval_hours}小时执行一次")
            return [{
                "id": "GroupChatZone",
                "name": "站点喊话服务",
                "trigger": "interval",
                "func": self.send_site_messages,
                "kwargs": {
                    "hours": interval_hours,
                }
            }]
        except (ValueError, TypeError) as e:
            logger.error(f"解析简单间隔配置失败: {str(e)}")
            
        # 如果所有解析都失败，返回空列表，将使用默认配置
        logger.warning("无法解析cron表达式，将使用默认配置")
        return []
        
    def _configure_random_service(self) -> List[Dict[str, Any]]:
        """
        配置随机执行的服务
        :return: 服务配置列表
        """
        try:
            triggers = TimerUtils.random_scheduler(
                num_executions=1,
                begin_hour=9,
                end_hour=23,
                max_interval=6 * 60,
                min_interval=2 * 60
            )
            
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
        except Exception as e:
            logger.error(f"配置随机定时任务失败: {str(e)}")
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

    def __custom_sites(self) -> List[Any]:
        custom_sites = []
        custom_sites_config = self.get_config("CustomSites")
        if custom_sites_config and custom_sites_config.get("enabled"):
            custom_sites = custom_sites_config.get("sites")
        return custom_sites

    def get_page(self) -> List[dict]:
        """
        注册页面
        """
        pass

    def send_site_messages(self) -> None:
        """
        自动向站点发送消息
        """
        # 检查是否在允许的时间范围内
        if self._start_time is not None and self._end_time is not None:
            current_hour = datetime.now(tz=pytz.timezone(settings.TZ)).hour
            if not (self._start_time <= current_hour <= self._end_time):
                logger.info(f"当前时间 {current_hour}点 不在配置的执行时间范围 {self._start_time}-{self._end_time}点 内，跳过执行")
                return
                
        # 使用上下文管理器处理锁
        if not self._lock.acquire(blocking=False):
            logger.warning("已有任务正在执行，本次调度跳过！")
            return
            
        try:
            self._running = True
            if self._chat_sites:
                # 确保 _sites_messages 是字符串类型
                site_messages = self._sites_messages
                if not isinstance(site_messages, str):
                    site_messages = str(site_messages) if site_messages else ""
                
                # 检查缓存是否有效
                current_time = time.time()
                if not self._parsed_messages_cache or (current_time - self._last_parse_time > 300):  # 5分钟缓存
                    self._parsed_messages_cache = self.parse_site_messages(site_messages)
                    self._last_parse_time = current_time
                    logger.debug("重新解析站点消息配置")
                else:
                    logger.debug("使用缓存的站点消息配置")
                
                self.__send_msgs(do_sites=self._chat_sites, site_msgs=self._parsed_messages_cache)
        except Exception as e:
            logger.error(f"发送站点消息时发生异常: {str(e)}")
            import traceback
            logger.debug(f"异常详情: {traceback.format_exc()}")
        finally:
            self._running = False
            self._lock.release()
            logger.debug("任务执行完成，锁已释放")

    def parse_site_messages(self, site_messages: str) -> Dict[str, List[str]]:
        """
        解析输入的站点消息
        :param site_messages: 多行文本输入
        :return: 字典，键为站点名称，值为该站点的消息列表
        """
        result = {}
        
        if not site_messages:
            logger.warning("站点消息配置为空")
            return result
            
        try:
            # 获取所有选中的站点名称
            all_sites = [site for site in self.sites.get_indexers() if not site.get("public")] + self.__custom_sites()
            
            # 创建站点ID到站点名称的映射
            site_id_to_name = {site.get("id"): site.get("name") for site in all_sites}
            
            # 获取选中的站点名称集合
            selected_site_names = {site_id_to_name.get(site_id) for site_id in self._chat_sites if site_id in site_id_to_name}
            
            if not selected_site_names:
                logger.warning("没有选中的站点")
                return result
                
            logger.debug(f"获取到的选中站点名称列表: {selected_site_names}")

            # 按行分割配置并处理
            for line_num, line in enumerate(site_messages.strip().splitlines(), 1):
                if not line.strip():
                    continue
                    
                try:
                    parts = line.split("|")
                    if len(parts) > 1:
                        site_name = parts[0].strip()
                        if site_name in selected_site_names:
                            messages = [msg.strip() for msg in parts[1:] if msg.strip()]
                            if messages:
                                result[site_name] = messages
                                logger.debug(f"站点 {site_name} 配置了 {len(messages)} 条消息")
                            else:
                                logger.warning(f"站点 {site_name} 没有有效的消息内容 (行 {line_num})")
                        else:
                            # 使用debug级别，因为这可能是正常情况（用户配置了多个站点但只选择了部分）
                            logger.debug(f"站点 {site_name} 不在选中列表中 (行 {line_num})")
                    else:
                        logger.warning(f"配置行格式错误，缺少分隔符: {line} (行 {line_num})")
                except Exception as e:
                    logger.error(f"解析配置行 {line_num} 时出错: {str(e)}")
            
            # 检查是否有选中的站点没有配置消息
            missing_sites = selected_site_names - set(result.keys())
            if missing_sites:
                logger.warning(f"以下选中的站点没有配置消息: {', '.join(missing_sites)}")
                
        except Exception as e:
            logger.error(f"解析站点消息时出现异常: {str(e)}")
            import traceback
            logger.debug(f"异常详情: {traceback.format_exc()}")
        
        if not result:
            logger.warning("没有解析到任何有效的站点消息配置")
        else:
            logger.info(f"站点消息解析完成，共解析到 {len(result)} 个站点的消息配置")
            
        return result

    def __send_msgs(self, do_sites: list, site_msgs: Dict[str, List[str]]) -> None:
        """
        发送消息逻辑
        :param do_sites: 要处理的站点ID列表
        :param site_msgs: 站点消息字典，键为站点名称，值为消息列表
        """
        # 查询所有站点
        all_sites = [site for site in self.sites.get_indexers() if not site.get("public")] + self.__custom_sites()
        
        # 创建站点ID到站点信息的映射，提高查找效率
        site_id_map = {site.get("id"): site for site in all_sites}
        
        # 过滤出需要处理的站点
        sites_to_process = []
        for site_id in do_sites:
            if site_id in site_id_map:
                sites_to_process.append(site_id_map[site_id])
            else:
                logger.warning(f"站点ID {site_id} 不存在或已被删除")
        
        if not sites_to_process:
            logger.info("没有需要发送消息的站点！")
            return

        # 执行站点发送消息
        site_results = {}
        total_success = 0
        total_failure = 0
        
        for site in sites_to_process:
            site_name = site.get("name")
            logger.info(f"开始处理站点: {site_name}")
            messages = site_msgs.get(site_name, [])

            # 添加消息列表空值检查
            if not messages:
                logger.warning(f"站点 {site_name} 没有需要发送的消息！")
                continue

            success_count = 0
            failure_count = 0
            failed_messages = []
            chat_records = []  # 存储聊天记录

            for i, message in enumerate(messages):
                try:
                    success, msg, chat_msgs = self.send_message_to_site(site, message)
                    if success:
                        success_count += 1
                        total_success += 1
                        logger.debug(f"站点 {site_name} 消息 {i+1}/{len(messages)} 发送成功")
                        
                        # 保存聊天记录
                        if chat_msgs:
                            chat_records.extend(chat_msgs)
                            logger.debug(f"获取到 {len(chat_msgs)} 条聊天记录")
                    else:
                        raise MessageSendError(msg)
                        
                except MessageSendError as e:
                    logger.error(f"向站点 {site_name} 发送消息失败: {str(e)}")
                    failure_count += 1
                    total_failure += 1
                    failed_messages.append(message)
                except Exception as e:
                    logger.error(f"向站点 {site_name} 发送消息时发生未预期异常: {str(e)}")
                    import traceback
                    logger.debug(f"异常详情: {traceback.format_exc()}")
                    failure_count += 1
                    total_failure += 1
                    failed_messages.append(message)
                
                # 修改间隔判断逻辑
                if i < len(messages) - 1:
                    interval = max(1, min(self._interval_cnt, 10))  # 限制间隔在1-10秒之间
                    logger.debug(f"等待 {interval} 秒后继续发送下一条消息...")
                    time.sleep(interval)
            
            site_results[site_name] = {
                "success_count": success_count,
                "failure_count": failure_count,
                "failed_messages": failed_messages,
                "chat_records": chat_records  # 添加聊天记录
            }
            
            logger.info(f"站点 {site_name} 处理完成: 成功 {success_count} 条, 失败 {failure_count} 条, 获取聊天记录 {len(chat_records)} 条")

        # 发送通知
        if self._notify:
            self._send_notification(site_results, len(sites_to_process), total_success, total_failure)

        # 检查是否所有消息都发送成功
        all_successful = total_failure == 0
        if all_successful:
            logger.info("所有站点的消息发送成功。")
        else:
            logger.warning(f"部分消息发送失败！成功: {total_success}, 失败: {total_failure}")

        self.__update_config()

    def _send_notification(self, site_results: Dict[str, Dict], total_sites: int, 
                          total_success: int, total_failure: int):
        """
        发送通知
        :param site_results: 站点结果字典
        :param total_sites: 总站点数
        :param total_success: 总成功数
        :param total_failure: 总失败数
        """
        notification_text = f"全部站点数量: {total_sites}\n"
        notification_text += f"总成功: {total_success}, 总失败: {total_failure}\n\n"
        
        for site_name, result in site_results.items():
            success_count = result["success_count"]
            failure_count = result["failure_count"]
            failed_messages = result["failed_messages"]
            chat_records = result.get("chat_records", [])
            
            notification_text += f"【{site_name}】成功发送{success_count}条信息，失败{failure_count}条\n"
            
            if failed_messages:
                notification_text += f"失败的消息: {', '.join(failed_messages)}\n"
            
            # 添加聊天记录到通知
            if chat_records:
                notification_text += f"\n最近聊天记录 ({len(chat_records)}条):\n"
                # 最多显示5条最新的聊天记录
                for record in chat_records[-5:]:
                    time_str = record.get("time", "")
                    username = record.get("username", "")
                    message = record.get("message", "")
                    notification_text += f"[{time_str}] {username}: {message}\n"
                notification_text += "\n"
        
        notification_text += f"\n{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(time.time()))}"

        self.post_message(
            mtype=NotificationType.SiteMessage,
            title="【执行喊话任务完成】:",
            text=notification_text
        )

    def send_message_to_site(self, site_info: CommentedMap, message: str) -> Tuple[bool, str, List[Dict]]:
        """
        向站点发送消息
        :param site_info: 站点信息
        :param message: 要发送的消息
        :raises MessageSendError: 当消息发送失败时
        :return: (成功状态, 消息, 聊天记录列表)
        """
        if not site_info:
            raise MessageSendError("无效的站点信息！")

        # 站点信息
        site_name = site_info.get("name", "").strip()
        site_url = site_info.get("url", "").strip()
        site_cookie = site_info.get("cookie", "").strip()
        ua = site_info.get("ua", "").strip()
        proxies = settings.PROXY if site_info.get("proxy") else None

        if not all([site_name, site_url, site_cookie, ua]):
            raise MessageSendError(f"站点 {site_name} 缺少必要信息，无法发送消息！")

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

        # 配置重试策略
        retries = Retry(
            total=3,  # 总重试次数
            backoff_factor=1,  # 重试间隔时间因子
            status_forcelist=[403, 404, 500, 502, 503, 504],  # 需要重试的状态码
            allowed_methods=["GET"],  # 需要重试的HTTP方法
            raise_on_status=False  # 不在重试时抛出异常，手动处理
        )
        adapter = HTTPAdapter(max_retries=retries)

        # 使用 Session 对象复用，创建会话对象
        with requests.Session() as session:
            session.headers.update(headers)
            if proxies:
                session.proxies = proxies
            session.mount('https://', adapter)
            
            attempt = 0
            max_attempts = 3  # 明确定义最大尝试次数
            while attempt < max_attempts:
                try:
                    response = session.get(send_url, params=params, timeout=(3.05, 10))
                    response.raise_for_status()  # 自动处理 4xx/5xx 状态码
                    
                    # 解析返回的聊天记录
                    chat_messages = self._parse_chat_response(response.text)
                    
                    logger.info(f"向 {site_name} 发送消息 '{message}' 成功")
                    return True, f"消息发送成功", chat_messages  # 成功发送后返回聊天记录
                except requests.exceptions.HTTPError as http_err:
                    logger.error(f"向 {site_name} 发送消息 '{message}' 失败，HTTP 错误: {http_err}")
                except requests.exceptions.ConnectionError as conn_err:
                    logger.error(f"向 {site_name} 发送消息 '{message}' 失败，连接错误: {conn_err}")
                except requests.exceptions.Timeout as timeout_err:
                    logger.error(f"向 {site_name} 发送消息 '{message}' 失败，请求超时: {timeout_err}")
                except requests.exceptions.RequestException as req_err:
                    logger.error(f"向 {site_name} 发送消息 '{message}' 失败，请求异常: {req_err}")
                
                attempt += 1
                if attempt < max_attempts:
                    backoff_time = 1 * (2 ** (attempt - 1))  # 简单的指数退避策略
                    logger.info(f"重试 {attempt}/{max_attempts}，将在 {backoff_time} 秒后重试...")
                    time.sleep(backoff_time)
                else:
                    logger.error(f"向 {site_name} 发送消息 '{message}' 失败，重试次数已达上限")
            
            # 如果所有尝试都失败，抛出异常
            raise MessageSendError(f"向站点 {site_name} 发送消息失败，已重试 {max_attempts} 次")
            
    def _parse_chat_response(self, html_content: str) -> List[Dict]:
        """
        解析站点返回的HTML内容，提取聊天记录
        :param html_content: HTML内容
        :return: 聊天记录列表
        """
        chat_messages = []
        try:
            # 首先尝试使用正则表达式提取聊天记录
            chat_pattern = r'\[\s*([^]]+)\s*\]\s*([^:]+):(.*?)(?=\[\s*[^]]+\s*\]|$)'
            matches = re.findall(chat_pattern, html_content, re.DOTALL)
            
            for match in matches:
                time_str = match[0].strip()
                username = match[1].strip()
                message = match[2].strip()
                
                chat_messages.append({
                    "time": time_str,
                    "username": username,
                    "message": message
                })
            
            # 如果正则表达式没有找到匹配，尝试使用BeautifulSoup解析HTML
            if not chat_messages:
                try:
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(html_content, 'html.parser')
                    
                    # 尝试查找聊天区域
                    # 这里的选择器需要根据实际站点HTML结构调整
                    chat_containers = soup.select('div.shoutbox, div.chat-container, table.shoutbox')
                    
                    if chat_containers:
                        for container in chat_containers:
                            # 查找聊天消息元素
                            chat_items = container.select('tr, div.chat-item, div.shout-item')
                            
                            for item in chat_items:
                                # 提取时间、用户名和消息
                                # 这里的选择器需要根据实际站点HTML结构调整
                                time_elem = item.select_one('span.time, td.time, div.time')
                                user_elem = item.select_one('span.user, td.user, div.user, a.user')
                                msg_elem = item.select_one('span.message, td.message, div.message')
                                
                                time_str = time_elem.text.strip() if time_elem else ""
                                username = user_elem.text.strip() if user_elem else ""
                                message = msg_elem.text.strip() if msg_elem else ""
                                
                                # 如果没有找到特定元素，尝试从整个item中提取文本
                                if not (time_str and username and message):
                                    text = item.text.strip()
                                    # 尝试解析文本格式，例如 "[时间] 用户名: 消息"
                                    match = re.search(r'\[(.*?)\](.*?):(.*)', text)
                                    if match:
                                        time_str = match.group(1).strip()
                                        username = match.group(2).strip()
                                        message = match.group(3).strip()
                                
                                if time_str or username or message:
                                    chat_messages.append({
                                        "time": time_str,
                                        "username": username,
                                        "message": message
                                    })
                except ImportError:
                    logger.warning("BeautifulSoup库未安装，无法使用高级HTML解析")
                except Exception as e:
                    logger.error(f"使用BeautifulSoup解析HTML时出错: {str(e)}")
            
            # 如果仍然没有找到聊天记录，尝试查找可能的聊天文本
            if not chat_messages:
                # 查找可能包含聊天记录的文本块
                text_blocks = re.findall(r'<div[^>]*>(.*?)</div>', html_content, re.DOTALL)
                for block in text_blocks:
                    # 移除HTML标签
                    clean_text = re.sub(r'<[^>]*>', ' ', block)
                    # 查找可能的聊天记录格式
                    chat_matches = re.findall(r'\[(.*?)\](.*?):(.*?)(?=\[|$)', clean_text)
                    for match in chat_matches:
                        time_str = match[0].strip()
                        username = match[1].strip()
                        message = match[2].strip()
                        
                        if time_str and username:  # 至少有时间和用户名
                            chat_messages.append({
                                "time": time_str,
                                "username": username,
                                "message": message
                            })
            
            # 去重并按时间排序
            if chat_messages:
                # 创建一个集合用于去重
                seen = set()
                unique_messages = []
                
                for msg in chat_messages:
                    # 创建一个唯一标识
                    msg_id = f"{msg['time']}|{msg['username']}|{msg['message']}"
                    if msg_id not in seen:
                        seen.add(msg_id)
                        unique_messages.append(msg)
                
                chat_messages = unique_messages
                
                # 记录解析结果
                logger.debug(f"成功解析到 {len(chat_messages)} 条聊天记录")
                if chat_messages:
                    logger.debug(f"示例记录: {chat_messages[0]}")
            else:
                logger.debug("未能解析到任何聊天记录")
                
        except Exception as e:
            logger.error(f"解析聊天记录时出错: {str(e)}")
            import traceback
            logger.debug(f"异常详情: {traceback.format_exc()}")
        
        return chat_messages

    def stop_service(self):
        """退出插件"""
        try:
            if self._scheduler:
                if self._lock and self._lock.locked():
                    logger.info("等待当前任务执行完成...")
                    try:
                        # 设置超时时间，避免无限等待
                        if self._lock.acquire(timeout=10):
                            self._lock.release()
                    except Exception as e:
                        logger.error(f"获取锁超时: {str(e)}")
                
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
                
            # 清除缓存
            self._parsed_messages_cache = {}
            self._last_parse_time = 0
        except Exception as e:
            logger.error(f"退出插件失败：{str(e)}")

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

    def _validate_and_filter_sites(self):
        """
        验证并过滤站点列表，移除不存在的站点
        """
        # 获取所有有效的站点ID
        all_sites = [site.id for site in self.siteoper.list_order_by_pri()] + [site.get("id") for site in self.__custom_sites()]
        
        # 过滤掉已删除的站点
        original_count = len(self._chat_sites)
        self._chat_sites = [site_id for site_id in self._chat_sites if site_id in all_sites]
        filtered_count = len(self._chat_sites)
        
        if original_count != filtered_count:
            logger.info(f"已从配置中移除 {original_count - filtered_count} 个不存在的站点")